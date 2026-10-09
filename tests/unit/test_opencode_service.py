"""PAC's provider boundary preserves native tools and never executes them."""
import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import fields
from typing import Any

import pytest

from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import (
    Message,
    ModelResponse,
    NativeToolCall,
    Role,
    ToolDeclaration,
)
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.core.redaction import Redaction, RedactionError
from personal_ai_core.integrations.opencode.factory import build_service
from personal_ai_core.integrations.opencode.protocol import OpenCodeChatRequest
from personal_ai_core.integrations.opencode.service import IntegrationError


class FakeProvider:
    name = "scripted"

    def __init__(self, *responses: ModelResponse | ProviderError) -> None:
        self.responses = list(responses) or [ModelResponse(text="Done.", model="test-backend")]
        self.seen: list[dict[str, Any]] = []

    def _respond(self, model: str, messages: Sequence[Mapping[str, Any]],
                 tools: Sequence[ToolDeclaration], options: Mapping[str, Any] | None) -> ModelResponse:
        self.seen.append({"model": model, "messages": messages,
                          "tools": tools, "options": dict(options or {})})
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response

    def generate(self, *, model: str, messages: Sequence[Mapping[str, Any]],
                 options: Mapping[str, Any] | None = None) -> ModelResponse:
        return self._respond(model, messages, (), options)

    def generate_with_tools(self, *, model: str, messages: Sequence[Mapping[str, Any]],
                            tools: Sequence[ToolDeclaration],
                            options: Mapping[str, Any] | None = None) -> ModelResponse:
        return self._respond(model, messages, tools, options)


def settings():
    return Settings(boss_model="test-backend", boss_context_window=8192)


def declaration(name="read_file"):
    return {"type": "function", "function": {
        "name": name, "description": "Read the requested file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    }}


def request(messages=None, **kwargs):
    return OpenCodeChatRequest.from_dict({
        "model": "pac-local", "messages": messages or [
            {"role": "user", "content": "Explain this repository."},
        ], **kwargs,
    })


def tool_exchange(output="# This project uses Python.", identifier="call_from_client"):
    return [
        {"role": "user", "content": "Read README.md."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        }]},
        {"role": "tool", "tool_call_id": identifier, "name": "read_file", "content": output},
    ]


def model_call(name="read_file", arguments=None):
    return NativeToolCall(name=name, arguments=arguments if arguments is not None else {"path": "README.md"})


def response(*calls, text=""):
    return ModelResponse(text=text, model="test-backend", tool_calls=tuple(calls),
                         prompt_tokens=200, completion_tokens=30)


@pytest.mark.parametrize("refusal", [False, True])
def test_one_final_content_free_audit_per_request_including_budget_refusal(refusal):
    records = []
    provider = FakeProvider(response(text="Completed."))
    service = build_service(settings=settings(), provider=provider, audit=records.append)
    content = "PRIVATE-PROMPT " * (2000 if refusal else 1)
    incoming = request([{"role": "user", "content": content}], tools=[declaration("read")])
    if refusal:
        with pytest.raises(IntegrationError, match="8192"):
            service.complete(incoming)
        assert not provider.seen
    else:
        service.complete(incoming)
    assert len(records) == (1 if refusal else 2)
    record = records[0]
    assert len(record["request_correlation_id"]) == 32
    assert record["event_type"] == "request_admission"
    assert record["outcome"] == ("refused_context_budget" if refusal else "admitted")
    if not refusal:
        assert records[1]["event_type"] == "request_completion"
        assert records[1]["outcome"] == "generated_text"
        assert records[1]["request_correlation_id"] == record["request_correlation_id"]
    encoded = json.dumps(record)
    assert "PRIVATE-PROMPT" not in encoded and "Read the requested file" not in encoded
    assert "README.md" not in encoded and "Non-negotiable" not in encoded


@pytest.mark.parametrize("required", [False, True])
def test_audit_write_error_is_sanitized_and_required_mode_withholds_response(required):
    warnings = []
    calls = []

    def unavailable(record):
        calls.append(record)
        raise OSError("SECRET-PATH /private/profile.md SECRET-PROMPT")

    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider, audit=unavailable,
                            audit_required=required, audit_warning=warnings.append)
    if required:
        with pytest.raises(IntegrationError) as caught:
            service.complete(request())
        assert caught.value.code == "audit_unavailable" and caught.value.status == 503
        assert not provider.seen
    else:
        assert service.complete(request()).body["choices"][0]["finish_reason"] == "stop"
    assert len(calls) == len(warnings) == (1 if required else 2)
    assert "SECRET" not in warnings[0] and "/private" not in warnings[0]


def test_audit_is_written_before_model_call_and_is_not_rewritten_afterward():
    records = []
    provider = FakeProvider()
    previous = provider._respond

    def generating(model, messages, tools, options):
        assert len(records) == 1 and records[0]["outcome"] == "admitted"
        return previous(model, messages, tools, options)

    provider._respond = generating
    service = build_service(settings=settings(), provider=provider, audit=records.append)
    service.complete(request())
    assert len(records) == 2 and records[1]["outcome"] == "generated_text"


@pytest.mark.parametrize("failure", ["provider", "protocol", "tool_calls"])
def test_completion_audit_has_same_id_and_only_sanitized_finite_metadata(failure):
    records = []
    model_response = (ProviderError("PRIVATE-ERROR /private/profile.md", kind="unreachable") if failure == "provider" else
                      response(model_call(name="read", arguments={"path": "PRIVATE-ARGUMENT"})))
    provider = FakeProvider(model_response)
    service = build_service(settings=settings(), provider=provider, audit=records.append)
    incoming = request(tools=[declaration("read")], tool_choice="none" if failure == "protocol" else "auto")
    if failure in {"provider", "protocol"}:
        with pytest.raises(IntegrationError):
            service.complete(incoming)
    else:
        service.complete(incoming)
    assert len(records) == 2
    assert records[0]["outcome"] == "admitted"
    assert records[1]["event_type"] == "request_completion"
    assert records[1]["outcome"] == {"provider": "provider_error", "protocol": "protocol_error",
                                      "tool_calls": "generated_tool_calls"}[failure]
    assert records[0]["request_correlation_id"] == records[1]["request_correlation_id"]
    assert "PRIVATE" not in json.dumps(records) and "/private" not in json.dumps(records)


def test_completion_write_failure_is_nonfatal_after_successful_admission():
    records = []
    warnings = []

    def audit(record):
        records.append(record)
        if record["event_type"] == "request_completion":
            raise OSError("PRIVATE-ERROR /secret/path")

    service = build_service(settings=settings(), provider=FakeProvider(), audit=audit,
                            audit_warning=warnings.append)
    assert service.complete(request()).body["choices"][0]["finish_reason"] == "stop"
    assert len(records) == 2 and len(warnings) == 1
    assert "PRIVATE" not in warnings[0] and "/secret" not in warnings[0]


def test_regular_text_uses_production_identity_and_registered_model_with_bounded_output():
    provider = FakeProvider(response(text="The repository contains a local AI layer."))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(max_completion_tokens=256, temperature=0.5, seed=12))
    assert completed.body["object"] == "chat.completion"
    assert completed.body["model"] == "pac-local"
    assert completed.body["choices"][0]["message"]["content"] == "The repository contains a local AI layer."
    assert completed.body["choices"][0]["finish_reason"] == "stop"
    assert completed.body["usage"]["total_tokens"] == 230
    sent = provider.seen[0]
    assert sent["model"] == "test-backend"
    assert sent["messages"][0]["role"] == "system"
    assert "Non-negotiable rules" in sent["messages"][0]["content"]
    assert sent["options"]["num_ctx"] == 8192
    assert sent["options"]["num_predict"] == 256
    assert sent["options"]["temperature"] == 0.5
    assert sent["options"]["seed"] == 12
    assert sent["tools"] == ()


def test_native_round_trip_retains_ids_names_roles_and_declarations():
    provider = FakeProvider(response(text="README explains the project."))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(tool_exchange(), tools=[declaration()]))
    sent = provider.seen[0]
    history = sent["messages"][1:]
    assert [message["role"] for message in history] == ["user", "assistant", "tool"]
    native_call = history[1]["tool_calls"][0]
    assert native_call["id"] == "call_from_client"
    assert native_call["function"] == {"name": "read_file", "arguments": {"path": "README.md"}}
    assert history[2]["tool_call_id"] == "call_from_client"
    assert history[2]["tool_name"] == "read_file"
    assert history[2]["content"] == "# This project uses Python."
    assert sent["tools"][0].parameters == declaration()["function"]["parameters"]
    assert completed.budget["tool_schema_tokens"] > 0
    assert completed.body["choices"][0]["message"]["content"] == "README explains the project."


def test_model_tool_response_is_native_json_with_identifier_and_object_arguments():
    provider = FakeProvider(response(model_call(arguments={"path": "ملف.md"})))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(tools=[declaration()], tool_choice="auto"))
    choice = completed.body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert message["content"] is None
    call = message["tool_calls"][0]
    assert isinstance(call["id"], str) and call["id"]
    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "ملف.md"}
    continuation = request([
        {"role": "user", "content": "Read the file."}, message,
        {"role": "tool", "tool_call_id": call["id"], "content": "File content."},
    ], tools=[declaration()])
    assert continuation.messages[-1].tool_call_id == call["id"]
    assert len(provider.seen) == 1


@pytest.mark.parametrize("choice", ["auto", "none"])
def test_auto_or_none_can_return_assistant_text(choice):
    provider = FakeProvider(response(text="Here is the explanation."))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(tools=[declaration()], tool_choice=choice))
    assert completed.body["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("choice", ["required", {
    "type": "function", "function": {"name": "read_file"},
}])
def test_required_or_named_choice_accepts_the_requested_tool(choice):
    provider = FakeProvider(response(model_call()))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(tools=[declaration()], tool_choice=choice))
    assert completed.body["choices"][0]["finish_reason"] == "tool_calls"
    instruction = provider.seen[0]["messages"][1]
    assert instruction["role"] == "system"
    assert "call" in instruction["content"]


@pytest.mark.parametrize("choice, returned", [
    ("none", response(model_call())),
    ("required", response(text="I can read it later.")),
    ({"type": "function", "function": {"name": "read_file"}},
     response(model_call(name="list_files"))),
])
def test_tool_choice_violations_are_refused_before_open_code_receives_calls(choice, returned):
    provider = FakeProvider(returned)
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tools=[declaration(), declaration("list_files")], tool_choice=choice))
    assert failed.value.code == "tool_choice_violation"
    assert failed.value.status == 502


def test_disabling_parallel_calls_refuses_multiple_model_calls():
    provider = FakeProvider(response(model_call(), model_call(arguments={"path": "pyproject.toml"})))
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tools=[declaration()], parallel_tool_calls=False))
    assert failed.value.code == "parallel_tool_calls_violation"


@pytest.mark.parametrize("returned", [
    response(model_call(name="undeclared")),
    response(model_call(arguments='{"path":"README.md"}')),
    response(model_call(arguments={"x": float("nan")})),
])
def test_undeclared_or_malformed_model_calls_are_not_returned_to_the_executor(returned):
    provider = FakeProvider(returned)
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tools=[declaration()]))
    assert failed.value.code in {"invalid_tool_call", "invalid_tool_arguments"}


def test_unknown_client_model_is_refused_before_generation():
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    wrong = OpenCodeChatRequest.from_dict({"model": "unknown", "messages": [
        {"role": "user", "content": "Hello."},
    ]})
    with pytest.raises(IntegrationError) as failed:
        service.complete(wrong)
    assert failed.value.code == "model_not_found"
    assert failed.value.status == 404
    assert provider.seen == []


def test_oversized_native_tool_output_stops_before_provider_and_is_not_truncated():
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    output = "هذا هو ناتج قراءة ملف المشروع. " * 1200
    original = request(tool_exchange(output), tools=[declaration()])
    with pytest.raises(IntegrationError) as failed:
        service.complete(original)
    assert failed.value.code == "context_length_exceeded"
    assert "8192" in failed.value.message
    assert original.messages[-1].content == output
    assert provider.seen == []


def test_tool_output_secret_is_withheld_before_generation_without_losing_pairing():
    secret = "sk-" + "A" * 40
    provider = FakeProvider(response(text="The key was withheld."))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request(tool_exchange("API_KEY=" + secret), tools=[declaration()]))
    wire = provider.seen[0]["messages"]
    assert secret not in json.dumps(wire)
    result = wire[-1]
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_from_client"
    assert result["tool_name"] == "read_file"
    assert "[withheld: secret]" in result["content"]
    assert completed.body["choices"][0]["message"]["content"] == "The key was withheld."


def test_encoded_json_tool_result_secret_is_redacted_after_decoding():
    secret = "sk-" + "D" * 40
    encoded = "".join(f"\\u{ord(letter):04x}" for letter in secret)
    content = '{"token":"' + encoded + '"}'
    provider = FakeProvider(response(text="The token was withheld."))
    service = build_service(settings=settings(), provider=provider)
    service.complete(request(tool_exchange(content), tools=[declaration()]))
    result = provider.seen[0]["messages"][-1]
    assert result["tool_call_id"] == "call_from_client"
    assert secret not in json.dumps(result)
    assert json.loads(result["content"])["token"] == "[withheld: secret]"


def test_historical_tool_argument_secret_is_redacted_without_changing_call_identity():
    secret = "sk-" + "E" * 40
    messages = tool_exchange()
    messages[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "path": "README.md", "nested": {"items": [{"token": secret}]},
    })
    provider = FakeProvider(response(text="Done."))
    service = build_service(settings=settings(), provider=provider)
    service.complete(request(messages, tools=[declaration()]))
    sent = provider.seen[0]["messages"]
    assert secret not in json.dumps(sent)
    call = sent[-2]["tool_calls"][0]
    assert call["id"] == "call_from_client"
    assert call["function"]["name"] == "read_file"
    assert call["function"]["arguments"]["nested"]["items"][0]["token"] == "[withheld: secret]"


def test_generated_secret_tool_argument_is_withheld_instead_of_altered_and_executed():
    secret = "sk-" + "F" * 40
    provider = FakeProvider(response(model_call(arguments={"path": "README.md", "token": secret})))
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tools=[declaration()]))
    assert failed.value.code == "secret_tool_arguments"
    assert secret not in failed.value.message


@pytest.mark.parametrize("location", ["encoded_result_key", "historical_argument_key"])
def test_secret_shaped_object_keys_fail_closed_without_changing_key_semantics(location):
    secret = "sk-" + "G" * 40
    messages = tool_exchange()
    if location == "encoded_result_key":
        encoded = "".join(f"\\u{ord(letter):04x}" for letter in secret)
        messages[-1]["content"] = '{"' + encoded + '":"value"}'
    else:
        messages[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
            "path": "README.md", "nested": {secret: "value"},
        })
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(messages, tools=[declaration()]))
    assert failed.value.code == "redaction_failed"
    assert secret not in failed.value.message
    assert provider.seen == []


def test_generated_secret_shaped_argument_key_is_not_sent_to_tool_executor():
    secret = "sk-" + "H" * 40
    provider = FakeProvider(response(model_call(arguments={"path": "README.md", "nested": {secret: "x"}})))
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tools=[declaration()]))
    assert failed.value.code == "invalid_tool_arguments"
    assert failed.value.status == 502
    assert secret not in failed.value.message


def test_raw_native_calls_without_declared_tools_are_withheld_even_on_text_only_provider_path():
    provider = FakeProvider(ModelResponse(text="I read the repository.", model="test-backend", raw={
        "message": {"role": "assistant", "content": "I read the repository.", "tool_calls": [{
            "function": {"name": "read_file", "arguments": {"path": "README.md"}},
        }]},
    }))
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request())
    assert failed.value.code == "undeclared_tool_call"
    assert failed.value.status == 502
    assert len(provider.seen) == 1
    assert provider.seen[0]["tools"] == ()


def test_secret_bearing_tool_schema_is_refused_whole_without_rewriting_or_sending_it():
    secret = "sk-" + "I" * 40
    definition = declaration()
    definition["function"]["parameters"]["properties"]["token"] = {"type": "string", "default": secret}
    original = request(tools=[definition])
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(original)
    assert failed.value.code == "secret_tool_schema"
    assert secret not in failed.value.message
    assert original.tools[0].to_dict() == definition
    assert provider.seen == []


class FailingRedactor:
    def redact(self, text: str):
        raise RedactionError(RedactionError.INTERNAL)


def test_redactor_failure_refuses_transcript_before_provider(monkeypatch):
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    monkeypatch.setattr(service, "_redactor", FailingRedactor())
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(tool_exchange(), tools=[declaration()]))
    assert failed.value.code == "redaction_failed"
    assert provider.seen == []


def test_redactor_failure_withholds_generated_reply_even_after_provider_succeeded(monkeypatch):
    class FailOnReply:
        def __init__(self):
            self.calls = 0

        def redact(self, text: str):
            self.calls += 1
            if self.calls > 1:
                raise RedactionError(RedactionError.INTERNAL)
            return Redaction(text=text)

    provider = FakeProvider(response(text="Unchecked provider reply."))
    service = build_service(settings=settings(), provider=provider)
    monkeypatch.setattr(service, "_redactor", FailOnReply())
    with pytest.raises(IntegrationError) as failed:
        service.complete(request())
    assert failed.value.code == "redaction_failed"
    assert failed.value.status == 502
    assert "Unchecked provider reply" not in failed.value.message
    assert len(provider.seen) == 1


@pytest.mark.parametrize("options", [
    {"max_tokens": 1025}, {"temperature": 2.1}, {"top_p": 1.1},
    {"frequency_penalty": 2.1}, {"presence_penalty": -2.1},
])
def test_unbudgeted_output_or_invalid_sampling_options_stop_before_model(options):
    provider = FakeProvider()
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request(**options))
    assert failed.value.code in {"invalid_output_limit", "invalid_sampling_option"}
    assert provider.seen == []


def test_generated_reply_secrets_are_withheld():
    secret = "sk-" + "B" * 40
    provider = FakeProvider(response(text="Here is the token: " + secret))
    service = build_service(settings=settings(), provider=provider)
    completed = service.complete(request())
    shown = completed.body["choices"][0]["message"]["content"]
    assert secret not in shown
    assert "[withheld: secret]" in shown


def test_provider_failures_return_safe_error_without_raw_provider_text():
    secret = "sk-" + "C" * 40
    provider = FakeProvider(ProviderError("Transport failed while sending " + secret, kind="timeout"))
    service = build_service(settings=settings(), provider=provider)
    with pytest.raises(IntegrationError) as failed:
        service.complete(request())
    assert failed.value.code == "provider_failure"
    assert failed.value.status == 502
    assert secret not in failed.value.message


def test_arabic_profile_and_projects_are_composed_and_counted_by_factory(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("اسمي سامر، وأعمل في تطوير البرمجيات.", encoding="utf-8")
    (tmp_path / "projects.md").write_text("مشروعي: تطوير مساعد محلي لتحليل المستندات.", encoding="utf-8")
    provider = FakeProvider(response(text="يمكنني مساعدتك في مشروعك."))
    service = build_service(settings=settings(), profile_path=profile, provider=provider)
    completed = service.complete(request())
    identity = provider.seen[0]["messages"][0]["content"]
    assert "اسمي سامر" in identity
    assert "مشروعي: تطوير مساعد محلي" in identity
    baseline = build_service(settings=settings(), provider=FakeProvider()).complete(request())
    assert completed.budget["identity_tokens"] > baseline.budget["identity_tokens"]


def test_oversized_arabic_profile_is_refused_without_model_call_or_store(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("هذه معلومات المستخدم التي ترسل مع كل طلب. " * 1000, encoding="utf-8")
    provider = FakeProvider()
    store = tmp_path / "opencode.sqlite3"
    with pytest.raises(IntegrationError) as failed:
        build_service(settings=settings(), profile_path=profile, store_path=store, provider=provider)
    assert failed.value.code == "profile_too_large"
    assert provider.seen == []
    assert not store.exists()


def test_external_conversation_mapping_survives_rebuild_and_stores_metadata_only(tmp_path):
    store = tmp_path / "opencode.sqlite3"
    transcript_marker = "PRIVATE_TOOL_CONTENT_MUST_NOT_BE_STORED"
    provider = FakeProvider(response(text="PRIVATE_REPLY_MUST_NOT_BE_STORED"))
    first_service = build_service(settings=settings(), store_path=store, provider=provider)
    first = first_service.complete(request(tool_exchange(transcript_marker), tools=[declaration()]),
                                   conversation_id="opencode-test-123")
    second_service = build_service(settings=settings(), store_path=store, provider=FakeProvider())
    second = second_service.complete(request(), conversation_id="opencode-test-123", request_kind="compaction")
    assert second.session_id == first.session_id
    with closing(sqlite3.connect(store)) as database:
        rows = database.execute("SELECT * FROM opencode_sessions").fetchall()
        metadata = json.loads(database.execute("SELECT metadata_json FROM opencode_sessions").fetchone()[0])
        tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(rows) == 1
    assert tables == [("opencode_sessions",)]
    assert transcript_marker not in str(rows)
    assert "PRIVATE_REPLY_MUST_NOT_BE_STORED" not in str(rows)
    assert metadata["status"] == "completed"
    assert metadata["context_window"] == 8192
    assert all(isinstance(value, (int, str)) for value in metadata.values())


def test_integration_keeps_existing_core_message_role_and_schema_shape():
    assert {role.value for role in Role} == {"system", "user", "assistant"}
    assert {item.name for item in fields(Message)} == {
        "session_id", "role", "content", "language", "id", "created_at",
    }
