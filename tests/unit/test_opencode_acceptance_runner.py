"""Executor evidence must distinguish actual calls from model claims and errors."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "opencode_provider_acceptance.py"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def _runner():
    spec = importlib.util.spec_from_file_location("opencode_provider_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(name="shell", *, command="git status; git rev-parse HEAD", output=HEAD,
          status="completed", metadata=None):
    return {"type": "tool_use", "part": {"id": "call-1", "tool": name,
            "state": {"status": status, "input": {"command": command}, "output": output,
                      "metadata": {"metadata": metadata or {"exit": 0}}}}}


def _text(text=HEAD):
    return {"type": "text", "part": {"text": text}}


def _admission(correlation="a", *, tool_results=0, kind="primary", outcome="admitted"):
    return {"event_type": "request_admission", "request_correlation_id": correlation,
            "request_kind": kind, "outcome": outcome,
            "authoritative_budget": {"context_window": 8192, "identity_tokens": 1321},
            "incoming_messages": {"role_counts": {"tool": tool_results}}}


def _completion(correlation="a", outcome="generated_tool_calls"):
    return {"event_type": "request_completion", "request_correlation_id": correlation,
            "outcome": outcome}


def test_real_executor_shape_and_later_answer_qualify():
    result = _runner().classify_events([_tool(), _text()], expected_head=HEAD, scenario="repository")
    assert result["pass"]
    assert result["actual_completed_tools"] == ["shell"]


@pytest.mark.parametrize("events", [
    [_text("I ran git status and git rev-parse HEAD: " + HEAD)],
    [_tool(command="echo 'git status'; echo 'git rev-parse HEAD'"), _text()],
    [_tool(status="error"), _text()],
    [_tool(metadata={"exit": 1}), _text()],
    [_tool(output="wrong commit"), _text()],
    [_text(), _tool()],
    [_tool(), {"type": "error", "error": {"message": "private failure"}}, _text()],
])
def test_claims_errors_echoes_and_ungrounded_answers_cannot_pass(events):
    assert not _runner().classify_events(events, expected_head=HEAD, scenario="repository")["pass"]


def test_mcp_requires_completed_inner_execution_not_javascript_claim():
    claimed = _tool("execute", command="return 'used GitHub'", output="used GitHub")
    assert not _runner().classify_events([claimed, _text("GitHub inspected")], expected_head=HEAD,
                                         scenario="github")["pass"]
    executed = _tool("execute", metadata={"toolCalls": [
        {"tool": "github.get_repository", "status": "completed", "input": {"private": "not recorded"}},
    ]})
    result = _runner().classify_events([executed, _text("GitHub inspected")], expected_head=HEAD,
                                      scenario="github")
    assert result["pass"]
    assert result["actual_completed_tools"] == ["github.get_repository"]
    assert "not recorded" not in json.dumps(result)


def test_mcp_inner_error_cannot_qualify():
    event = _tool("execute", metadata={"toolCalls": [
        {"tool": "github.get_repository", "status": "error"},
    ]})
    assert not _runner().classify_events([event, _text()], expected_head=HEAD, scenario="github")["pass"]


def test_pairing_and_tool_roles_prove_the_continuation():
    records = [_admission(), _completion(), _admission("b", tool_results=2),
               _completion("b", "generated_text")]
    result = _runner().classify_audit(records, require_tools=True, request_kind_trusted=True)
    assert result["pass"]
    assert result["conditions"]["role_tool_primary_continuation"]


@pytest.mark.parametrize("records", [
    [_admission(), _completion(), _admission("b"), _completion("b", "generated_text")],
    [_admission(), _completion(), _admission("b", tool_results=2, kind="compaction"),
     _completion("b", "generated_text")],
    [_admission(), _completion(), _admission("b", tool_results=2), _completion("b", "provider_error")],
    [_admission(), _completion(), _admission("b", tool_results=2)],
    [_completion(), _admission(), _admission("b", tool_results=2), _completion("b", "generated_text")],
    [_admission(), _completion(), _admission("b", tool_results=2, outcome="refused_context_budget")],
])
def test_missing_results_compaction_refusals_and_wrong_order_fail(records):
    assert not _runner().classify_audit(records, require_tools=True, request_kind_trusted=True)["pass"]


def test_request_kind_label_without_independent_hook_validation_is_not_proof():
    records = [_admission(), _completion(), _admission("b", tool_results=2),
               _completion("b", "generated_text")]
    assert not _runner().classify_audit(records, require_tools=True)["pass"]


def test_compaction_must_complete_and_be_followed_by_primary():
    records = [_admission(), _completion(), _admission("b", kind="compaction", tool_results=1),
               _completion("b", "generated_text"), _admission("c", tool_results=2),
               _completion("c", "generated_text")]
    runner = _runner()
    assert runner.classify_audit(records, require_tools=True, require_compaction=True,
                                request_kind_trusted=True)["pass"]
    assert not runner.classify_audit(records[:-2], require_tools=True, require_compaction=True,
                                    request_kind_trusted=True)["pass"]


def test_jsonl_parser_marks_plain_text_and_malformed_json_as_invalid():
    events, invalid = _runner().parse_events(json.dumps(_tool()) + "\nI executed a tool\n{broken\n")
    assert len(events) == 1 and invalid == 2


def test_repository_integrity_reports_counts_without_file_names_or_contents():
    before = {"files": {"private-profile.md": "one"}, "file_count": 1, "file_digest": "first",
              "head": HEAD, "status": b"", "index": b"unchanged"}
    after = {**before, "files": {"private-profile.md": "two"}, "file_digest": "second"}
    result = _runner().compare_snapshots(before, after)
    assert result["changed_file_count"] == 1
    assert not result["repository_unchanged"]
    assert "private-profile.md" not in json.dumps(result)


def test_disposable_file_restoration_requires_two_actual_edit_events():
    changed = _tool("edit")
    restored = _tool("edit")
    changed["part"]["state"]["input"] = {"newString": "PAC ACCEPTANCE CHANGED"}
    restored["part"]["state"]["input"] = {"newString": "PAC ACCEPTANCE ORIGINAL"}
    runner = _runner()
    assert runner.classify_events([changed, restored, _text("Restored")], expected_head=HEAD,
                                  scenario="temporary-edit")["pass"]
    assert not runner.classify_events([changed, _text("Restored")], expected_head=HEAD,
                                      scenario="temporary-edit")["pass"]
