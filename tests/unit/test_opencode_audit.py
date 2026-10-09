"""Audits describe exposure/cost without retaining private request contents."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from personal_ai_core.context.budget import ReserveBasedBudgetPolicy
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.core.redaction import RedactionError
from personal_ai_core.integrations.opencode.audit import (
    BUILTIN_BASELINE,
    JsonlAuditWriter,
    describe_request,
)
from personal_ai_core.integrations.opencode.budget import (
    OpenCodeContextBudget,
    canonical_json,
)
from personal_ai_core.integrations.opencode.protocol import (
    OpenCodeChatRequest,
    OpenCodeMessage,
)


class Spec:
    name = "test-backend"
    provider = "scripted"
    context_window = 8192


def tool(name, description="PRIVATE_SCHEMA_DESCRIPTION", parameters=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": parameters if parameters is not None else {"type": "object"},
    }}


def parsed(messages=None, tools=None, **kwargs):
    return OpenCodeChatRequest.from_dict({
        "model": "pac-local", "messages": messages if messages is not None else [
            {"role": "system", "content": "PRIVATE_SYSTEM_INSTRUCTIONS"},
            {"role": "user", "content": "PRIVATE_USER_REQUEST"},
        ], "tools": tools if tools is not None else [tool("read"), tool("glob")], **kwargs,
    })


def measured(request, messages=None):
    estimator = ScriptAwareTokenEstimator()
    meter = OpenCodeContextBudget(estimator, ReserveBasedBudgetPolicy(
        identity_reserve=1200, generation_reserve=1024, overhead=256, guard_reserve=100,
    ))
    return meter.measure(Spec(), request.messages if messages is None else messages, request.tools), estimator


def catalog(partial=True):
    inventory = ("The catalog is partial. Inside `execute`, use `search(...)` to find a tool, "
                 "then call it by the `path` in the result. `search` is synchronous. "
                 "Call it without `await`; it does not return a Promise. Do not guess tool names.\n\n"
                 "- search(input: { query: string }): SearchResult"
                 if partial else "The catalog is complete. Do not guess tool names.")
    return (
        "# Code Mode\n\nUse the `execute` tool to call the tools listed below. "
        "They cannot be called directly" + (", and neither can `search`. Both" if partial else ". They") +
        " only work inside code you pass to `execute`.\n\n" + inventory +
        "\n\n## Available tools\n\n"
        "- context7 (3 tools, 1 shown) // PRIVATE_NAMESPACE_DESCRIPTION\n"
        '  - tools.context7["query-docs"]({ query: string }): Promise<string> // PRIVATE_TOOL_DESCRIPTION\n'
        "- opencode (1 tool)\n"
        "  - tools.opencode.models(): Promise<Model[]> // PRIVATE_MODEL_CATALOG_TEXT"
    )


def test_received_native_set_is_exact_and_unreceived_baseline_is_not_a_denial():
    request = parsed(tools=[tool("read"), tool("glob"), tool("my_custom_lookup")])
    cost, _ = measured(request)
    record = describe_request(request, cost)
    assert record["incoming_native_tools"] == ["read", "glob", "my_custom_lookup"]
    assert record["selected_native_tools"] == record["incoming_native_tools"]
    assert record["omitted_native_tools"] == []
    assert record["saved_tool_schema_tokens"] == 0
    assert record["selection_owner"] == "opencode"
    assert record["builtin_baseline_not_received"] == sorted(BUILTIN_BASELINE - {"read", "glob"})
    assert record["unreceived_builtin_semantics"] == "absence_from_request_not_a_permission_denial"
    assert record["mcp_omitted_inventory"] == "unknown_without_upstream_manifest"


def test_private_transcript_schema_arguments_and_outputs_never_appear_in_record():
    request = parsed(messages=[
        {"role": "system", "content": "PRIVATE_SYSTEM_INSTRUCTIONS"},
        {"role": "user", "content": "PRIVATE_USER_REQUEST"},
        {"role": "assistant", "content": "PRIVATE_ASSISTANT_TEXT", "tool_calls": [{
            "id": "PRIVATE_CALL_ID", "type": "function", "function": {
                "name": "read", "arguments": '{"path":"PRIVATE_PATH","value":"PRIVATE_ARGUMENT"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "PRIVATE_CALL_ID", "content": "PRIVATE_TOOL_RESULT"},
    ], tools=[tool("read", parameters={"type": "object", "default": "PRIVATE_SCHEMA_DEFAULT"})])
    cost, _ = measured(request)
    serialized = json.dumps(describe_request(request, cost))
    assert "PRIVATE_" not in serialized


def test_role_decomposition_uses_production_estimator_without_changing_authoritative_total():
    request = parsed(messages=[
        {"role": "system", "content": "إرشادات باللغة العربية"},
        {"role": "user", "content": "Explain the file."},
        {"role": "assistant", "content": "An earlier answer."},
    ])
    cost, estimator = measured(request)
    before = cost.to_dict()
    record = describe_request(request, cost)
    messages = record["incoming_messages"]
    expected = estimator.estimate(canonical_json([message.to_dict() for message in request.messages]))
    assert messages["canonical_transcript_tokens"] == expected
    assert messages["system_canonical_tokens"] + messages["non_system_increment_tokens"] == expected
    assert messages["role_counts"] == {"system": 1, "user": 1, "assistant": 1, "tool": 0}
    assert messages["per_message"][0] == {
        "index": 0, "role": "system",
        "estimated_tokens": estimator.estimate(canonical_json(request.messages[0].to_dict())),
    }
    assert messages["standalone_estimates_are_additive"] is False
    assert record["authoritative_budget"] == before == cost.to_dict()


def test_added_choice_instruction_is_a_separate_increment_not_incoming_user_tokens():
    request = parsed(tool_choice="required")
    base, _ = measured(request)
    added = (OpenCodeMessage(role="system", content="Use a declared tool for this generation."),
             *request.messages)
    cost, _ = measured(request, added)
    record = describe_request(request, cost)
    assert record["incoming_messages"]["canonical_transcript_tokens"] == base.transcript_tokens
    assert record["integration_instruction_increment_tokens"] == cost.transcript_tokens - base.transcript_tokens
    assert record["integration_instruction_increment_tokens"] > 0


def test_redaction_savings_are_visible_as_signed_delta_without_relabeling_incoming_messages():
    request = parsed(messages=[{"role": "user", "content": "LONG_PRIVATE_TEXT" * 100}])
    sent = (OpenCodeMessage(role="user", content="[withheld: secret]"),)
    cost, _ = measured(request, sent)
    record = describe_request(request, cost)
    assert record["integration_instruction_increment_tokens"] < 0
    assert record["transcript_delta_scope"] == "integration_instructions_and_redaction"


def test_generated_codemode_catalog_exposes_paths_inventory_and_lazy_counts_without_text():
    request = parsed(messages=[{"role": "system", "content": catalog()},
                               {"role": "user", "content": "PRIVATE_USER_REQUEST"}], tools=[tool("execute")])
    cost, estimator = measured(request)
    record = describe_request(request, cost)
    mode = record["codemode"]
    assert mode["present"] is True
    assert mode["instruction_tokens"] == estimator.estimate(catalog())
    groups = mode["sections"][0]["namespaces"]
    assert groups[0]["name"] == "context7"
    assert groups[0]["inventory_count"] == 3
    assert groups[0]["inline_count"] == 1
    assert groups[0]["lazy_not_inline_count"] == 2
    assert groups[0]["inline_paths"] == ['tools.context7["query-docs"]']
    assert groups[1]["inline_paths"] == ["tools.opencode.models"]
    assert groups[0]["instruction_tokens"] > 0
    assert "PRIVATE_" not in json.dumps(record)
    assert mode["inventory_semantics"] == "observed_inline_snapshots_not_permission_decisions"
    assert "denied" not in json.dumps(mode)


def test_codemode_lookalikes_in_user_text_or_without_generated_preamble_are_not_catalogs():
    request = parsed(messages=[
        {"role": "user", "content": catalog()},
        {"role": "system", "content": "# Code Mode\n\n## Available tools\n- private (100 tools)"},
    ])
    cost, _ = measured(request)
    assert describe_request(request, cost)["codemode"]["present"] is False


def test_codemode_catalog_ends_at_next_top_level_heading_and_preserves_snapshot_indices():
    text = catalog() + "\n# Private project\n- private_project (50 tools) // PRIVATE_PROJECT_TEXT"
    request = parsed(messages=[{"role": "system", "content": text},
                               {"role": "system", "content": catalog(False)}])
    cost, estimator = measured(request)
    mode = describe_request(request, cost)["codemode"]
    assert mode["section_count"] == 2
    assert mode["sections"][0]["instruction_tokens"] == estimator.estimate(catalog())
    assert mode["sections"][1]["message_index"] == 1
    assert mode["sections"][1]["catalog_complete"] is True
    assert "private_project" not in json.dumps(mode)


@pytest.mark.parametrize("boundary", [
    "\n\n<mcp_instructions>",
    "\n\nSkills provide specialized instructions and workflows for specific tasks.",
    "\n\nProject references provide additional directories that can be accessed when relevant.",
])
def test_codemode_cost_excludes_later_generated_mcp_skills_and_reference_blocks(boundary):
    text = catalog() + boundary + "PRIVATE_SOURCE_TEXT " * 10_000 + "\n" + catalog()
    request = parsed(messages=[{"role": "system", "content": text}])
    cost, estimator = measured(request)
    record = describe_request(request, cost)
    assert record["codemode"]["section_count"] == 1
    assert record["codemode"]["instruction_tokens"] == estimator.estimate(catalog())
    assert record["incoming_messages"]["system_canonical_tokens"] > 80_000
    assert "PRIVATE_" not in json.dumps(record)


def test_multiline_namespace_descriptions_are_fully_costed_without_being_logged():
    text = catalog().replace("PRIVATE_NAMESPACE_DESCRIPTION", "PRIVATE_DESCRIPTION_LINE\n" * 1000)
    request = parsed(messages=[{"role": "system", "content": text}])
    cost, estimator = measured(request)
    record = describe_request(request, cost)
    group = record["codemode"]["sections"][0]["namespaces"][0]
    assert group["instruction_tokens"] > estimator.estimate("PRIVATE_DESCRIPTION_LINE\n" * 1000)
    assert "PRIVATE_" not in json.dumps(record)


@pytest.mark.parametrize("name", ["sk-" + "A" * 40, "PRIVATE_NAME\nPRIVATE_VALUE", "x" * 100])
def test_suspect_native_tool_names_are_hashed_with_sanitized_warning(name):
    request = parsed(tools=[tool(name)])
    cost, _ = measured(request)
    record = describe_request(request, cost)
    hashed = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
    assert record["selected_native_tools"] == [hashed]
    assert name not in json.dumps(record)
    assert record["warnings"] == ["suspect_or_invalid_name_withheld"]


def test_codemode_suspect_namespace_and_path_are_withheld_without_logging_descriptions():
    secret = "sk-" + "B" * 40
    text = catalog().replace("context7", secret)
    request = parsed(messages=[{"role": "system", "content": text}])
    cost, _ = measured(request)
    record = describe_request(request, cost)
    group = record["codemode"]["sections"][0]["namespaces"][0]
    assert group["name"].startswith("sha256:")
    assert "PRIVATE_" not in json.dumps(record)
    assert secret not in json.dumps(record)


def test_failed_name_redactor_withholds_names_and_reports_only_fixed_warning():
    class Broken:
        def redact(self, text):
            raise RedactionError(RedactionError.INTERNAL)

    request = parsed(tools=[tool("custom_lookup")])
    cost, _ = measured(request)
    record = describe_request(request, cost, redactor=Broken())
    assert record["selected_native_tools"][0].startswith("sha256:")
    assert "custom_lookup" not in json.dumps(record)
    assert record["warnings"] == ["name_check_failed", "suspect_or_invalid_name_withheld"]


def test_trusted_upstream_manifest_records_actual_omissions_and_reasons():
    request = parsed(tools=[tool("read")])
    cost, _ = measured(request)
    manifest = {
        "incoming_names": ["read", "shell"], "selected_names": ["read"],
        "incoming_schema_tokens": cost.tool_schema_tokens + 300,
        "selected_schema_tokens": cost.tool_schema_tokens,
        "omitted": [{"name": "shell", "reason": "agent_preset"}],
    }
    record = describe_request(request, cost, selection_diagnostics=manifest)
    assert record["incoming_native_tools"] == ["read", "shell"]
    assert record["selected_native_tools"] == ["read"]
    assert record["omitted_native_tools"] == [{"name": "shell", "reason": "agent_preset"}]
    assert record["saved_tool_schema_tokens"] == 300
    assert record["selection_visibility"] == "trusted_upstream_selection_manifest"


def test_manifest_cannot_invent_selected_tools_or_log_arbitrary_reason_text():
    request = parsed(tools=[tool("read")])
    cost, _ = measured(request)
    manifest = {
        "incoming_names": ["read", "shell"], "selected_names": ["read"],
        "incoming_schema_tokens": cost.tool_schema_tokens + 300,
        "selected_schema_tokens": cost.tool_schema_tokens,
        "omitted": [{"name": "shell", "reason": "PRIVATE_REASON_TEXT"}],
    }
    with pytest.raises(ValueError, match="supported reason"):
        describe_request(request, cost, selection_diagnostics=manifest)
    manifest["omitted"][0]["reason"] = "agent_preset"
    manifest["selected_names"] = ["shell"]
    with pytest.raises(ValueError, match="received tools"):
        describe_request(request, cost, selection_diagnostics=manifest)


def test_manually_created_budget_without_diagnostic_estimator_fails_explicitly():
    request = parsed()
    cost, _ = measured(request)
    with pytest.raises(ValueError, match="diagnostic estimator"):
        describe_request(request, replace(cost, _diagnostic_estimator=None))


def audit_record(index=1, outcome="admitted"):
    request = parsed()
    cost, _ = measured(request)
    record = describe_request(request, cost)
    record.update(request_correlation_id=f"{index:032x}",
                  selection_policy_version="opencode-native-pass-through-v1",
                  request_kind="primary", outcome=outcome, event_type="request_admission")
    return record


def completion_record(index=1, outcome="generated_text", **kwargs):
    return {
        "event_type": "request_completion", "request_correlation_id": f"{index:032x}",
        "selection_policy_version": "opencode-native-pass-through-v1",
        "request_kind": "primary", "outcome": outcome, **kwargs,
    }


def test_jsonl_writer_appends_immutable_single_lines_and_creates_parent(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    writer = JsonlAuditWriter(path)
    first = audit_record()
    writer(first)
    before = path.read_bytes()
    first["outcome"] = "changed_after_write"
    writer(completion_record())
    after = path.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 2
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["outcome"] == "admitted"
    assert rows[1]["outcome"] == "generated_text"
    assert rows[0]["request_correlation_id"] == rows[1]["request_correlation_id"]


def test_jsonl_writer_rejects_nan_before_creating_or_modifying_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    writer = JsonlAuditWriter(path)
    invalid = audit_record()
    invalid["authoritative_budget"]["transcript_tokens"] = float("nan")
    with pytest.raises(ValueError):
        writer(invalid)
    assert not path.exists()
    writer(audit_record())
    before = path.read_bytes()
    invalid["authoritative_budget"]["transcript_tokens"] = float("inf")
    with pytest.raises(ValueError):
        writer(invalid)
    assert path.read_bytes() == before


def test_jsonl_writer_errors_propagate_to_the_callers_error_policy(tmp_path):
    directory = tmp_path / "destination"
    directory.mkdir()
    writer = JsonlAuditWriter(directory)
    with pytest.raises(OSError):
        writer(audit_record())


def test_jsonl_writer_lock_keeps_concurrent_records_intact(tmp_path):
    path = tmp_path / "audit.jsonl"
    writer = JsonlAuditWriter(path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: writer(audit_record(index)), range(30)))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 30
    assert {row["request_correlation_id"] for row in rows} == {f"{index:032x}" for index in range(30)}


@pytest.mark.parametrize("location", ["top", "budget", "messages", "codemode", "namespace", "omission"])
def test_writer_refuses_unexpected_private_fields_at_every_nested_boundary(tmp_path, location):
    request = parsed(messages=[{"role": "system", "content": catalog()}])
    cost, _ = measured(request)
    record = describe_request(request, cost)
    record.update(request_correlation_id=f"{1:032x}", selection_policy_version="test-v1",
                  request_kind="primary", outcome="admitted", event_type="request_admission")
    if location == "top":
        record["profile"] = "PRIVATE_PROFILE"
    elif location == "budget":
        record["authoritative_budget"]["prompt"] = "PRIVATE_PROMPT"
    elif location == "messages":
        record["incoming_messages"]["per_message"][0]["content"] = "PRIVATE_MESSAGE"
    elif location == "codemode":
        record["codemode"]["source"] = "PRIVATE_SYSTEM"
    elif location == "namespace":
        record["codemode"]["sections"][0]["namespaces"][0]["description"] = "PRIVATE_SCHEMA"
    else:
        record["omitted_native_tools"] = [{"name": "shell", "reason": "agent_preset", "arguments": "PRIVATE_ARGS"}]
    path = tmp_path / "audit.jsonl"
    with pytest.raises(ValueError, match="metadata"):
        JsonlAuditWriter(path)(record)
    assert not path.exists()


def test_writer_accepts_full_safe_codemode_metadata_and_rejects_content_disguised_as_path(tmp_path):
    request = parsed(messages=[{"role": "system", "content": catalog()}])
    cost, _ = measured(request)
    record = describe_request(request, cost)
    record.update(request_correlation_id=f"{1:032x}", selection_policy_version="test-v1",
                  request_kind="primary", outcome="admitted", event_type="request_admission")
    path = tmp_path / "audit.jsonl"
    writer = JsonlAuditWriter(path)
    writer(record)
    before = path.read_bytes()
    record["codemode"]["sections"][0]["namespaces"][0]["inline_paths"] = ["PRIVATE_USER_REQUEST"]
    with pytest.raises(ValueError, match="callable identifier"):
        writer(record)
    assert path.read_bytes() == before


@pytest.mark.parametrize("outcome", [
    "refused_context_budget", "refused_required_tool_unavailable", "refused_protocol",
])
def test_refused_admission_is_one_append_only_event_without_completion(tmp_path, outcome):
    path = tmp_path / "audit.jsonl"
    JsonlAuditWriter(path)(audit_record(outcome=outcome))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event_type"] == "request_admission"
    assert rows[0]["outcome"] == outcome


@pytest.mark.parametrize("outcome, optional", [
    ("generated_text", {"tool_call_count": 0, "finish_reason": "stop", "prompt_tokens": 200, "completion_tokens": 20}),
    ("generated_text", {"finish_reason": "length"}),
    ("generated_tool_calls", {"tool_call_count": 2, "finish_reason": "tool_calls"}),
    ("provider_error", {}),
    ("protocol_error", {}),
])
def test_completion_events_accept_only_finite_generation_outcomes_and_optional_counts(tmp_path, outcome, optional):
    path = tmp_path / "audit.jsonl"
    record = completion_record(outcome=outcome, **optional)
    JsonlAuditWriter(path)(record)
    assert json.loads(path.read_text(encoding="utf-8")) == record


@pytest.mark.parametrize("record", [
    completion_record(outcome="completed"),
    completion_record(content="PRIVATE_REPLY"),
    completion_record(arguments={"path": "PRIVATE_PATH"}),
    completion_record(outcome="provider_error", finish_reason="stop"),
    completion_record(outcome="protocol_error", prompt_tokens=12),
    completion_record(prompt_tokens=float("nan")),
    completion_record(completion_tokens=-1),
    completion_record(tool_call_count=True),
    completion_record(finish_reason="PRIVATE_ERROR_TEXT"),
    completion_record(tool_call_count=1),
    completion_record(outcome="generated_tool_calls", tool_call_count=0),
    completion_record(outcome="generated_tool_calls", finish_reason="stop"),
])
def test_completion_metadata_rejects_private_fields_legacy_outcomes_and_invalid_counts(tmp_path, record):
    path = tmp_path / "audit.jsonl"
    with pytest.raises((ValueError, TypeError)):
        JsonlAuditWriter(path)(record)
    assert not path.exists()


def test_admission_does_not_accept_completion_or_legacy_outcomes(tmp_path):
    writer = JsonlAuditWriter(tmp_path / "audit.jsonl")
    for outcome in ("completed", "generation_admitted", "budget_refused", "generated_text", "provider_error"):
        with pytest.raises(ValueError):
            writer(audit_record(outcome=outcome))
    assert not writer.path.exists()
