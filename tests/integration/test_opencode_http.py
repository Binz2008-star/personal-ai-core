"""HTTP wire checks: structured calls survive without HTTP executing tools."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from personal_ai_core.integrations.opencode.http import MAX_REQUEST_BYTES, create_server
from personal_ai_core.integrations.opencode.service import IntegrationError
from personal_ai_core.integrations.opencode.store import IntegrationStoreError


@dataclass
class Completion:
    body: dict
    session_id: str = "pac-session"
    budget: dict | None = None


class Service:
    context_window = 8192
    output_limit = 1024

    def __init__(self):
        self.requests = []
        self.selections = []
        self.error: Exception | None = None

    def complete(self, request, *, conversation_id=None, request_kind="primary", selection_diagnostics=None):
        self.requests.append((request, conversation_id, request_kind))
        self.selections.append(selection_diagnostics)
        if self.error:
            raise self.error
        if len(self.requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_git", "type": "function", "function": {
                    "name": "bash", "arguments": '{"command":"git status"}'}}
            ]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "The working tree is clean."}
            reason = "stop"
        return Completion({"id": "chatcmpl-pac", "object": "chat.completion", "created": 1,
                           "model": "pac-local", "choices": [{"index": 0, "message": message,
                                                                 "finish_reason": reason}]})


@contextmanager
def _server(service=None, *, log=None):
    fake = service or Service()
    server = create_server(fake, port=0, log=log)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _send(base, path="/v1/chat/completions", body=None, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    request = Request(base + path, data=data,
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, json.loads(response.read())


def _request():
    return {"model": "pac-local", "messages": [{"role": "user", "content": "Inspect the repo"}],
            "tools": [{"type": "function", "function": {"name": "bash", "description": "Run command",
                      "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                                     "required": ["command"]}}}], "stream": False}


def test_native_selection_manifest_is_metadata_only_and_passes_to_service():
    manifest = {"incoming_names": ["bash", "read"], "selected_names": ["bash"],
                "omitted": [{"name": "read", "reason": "upstream_selection"}],
                "incoming_schema_tokens": 300, "selected_schema_tokens": 200, "profile": "repo"}
    with _server() as (service, base):
        status, _, _ = _send(base, body=_request(),
                            headers={"X-PAC-Tool-Selection": json.dumps(manifest)})
        assert status == 200 and service.selections == [manifest]
        assert service.requests[0][0].tools[0].name == "bash"


@pytest.mark.parametrize("metadata", ["not-json", "[]", "x" * 16_385])
def test_invalid_selection_header_refused_without_leaking_it(metadata):
    with _server() as (service, base):
        status, _, body = _send(base, body=_request(), headers={"X-PAC-Tool-Selection": metadata})
        assert status == 400 and body["error"]["code"] == "invalid_selection_metadata"
        assert not service.requests
        assert metadata not in body["error"]["message"]


def test_models_has_exact_local_model_and_configured_limits():
    with _server() as (_, base), urlopen(base + "/v1/models", timeout=3) as response:
        model = json.loads(response.read())["data"][0]
        assert response.status == 200
        assert model["id"] == "pac-local" and model["name"] == "PAC (local)"
        assert model["context_window"] == 8192 and model["output_limit"] == 1024


def test_two_json_requests_preserve_tool_call_result_and_session_header():
    with _server() as (service, base):
        first = _request()
        code, headers, completion = _send(base, body=first, headers={"X-OpenCode-Session-ID": "ses_external"})
        assert code == 200 and headers["X-PAC-Session-ID"] == "pac-session"
        call = completion["choices"][0]["message"]
        assert call["tool_calls"][0]["function"]["arguments"] == '{"command":"git status"}'
        second = _request()
        second["messages"] += [call, {"role": "tool", "tool_call_id": "call_git",
                                       "content": "On branch main\nnothing to commit, working tree clean"}]
        code, _, completion = _send(base, body=second, headers={"X-OpenCode-Session-ID": "ses_external"})
        assert code == 200 and completion["choices"][0]["finish_reason"] == "stop"
        assert len(service.requests) == 2
        request, external, kind = service.requests[1]
        assert external == "ses_external" and kind == "primary"
        message = request.messages[-1]
        assert message.role == "tool" and message.tool_call_id == "call_git"
        assert "working tree clean" in message.content
        assert request.messages[-2].tool_calls[0].id == "call_git"


def test_fallback_header_is_used_without_guessing_session_from_content():
    with _server() as (service, base):
        assert _send(base, body=_request(), headers={"X-PAC-Session-ID": "fallback"})[0] == 200
        assert _send(base, body=_request())[0] == 200
        assert [item[1] for item in service.requests] == ["fallback", None]


def _send_stream(base, body):
    req = Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", "X-OpenCode-Session-ID": "ses_stream"})
    with urlopen(req, timeout=3) as response:
        headers = response.headers
        data = response.read().decode("utf-8")
    frames = [item[6:] for item in data.split("\n\n") if item.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    return headers, [json.loads(frame) for frame in frames[:-1]]


def test_sse_tool_call_then_native_result_continuation_uses_one_generation_each():
    with _server() as (service, base):
        body = _request()
        body["stream"] = True
        headers, frames = _send_stream(base, body)
        assert headers.get_content_type() == "text/event-stream"
        assert headers["X-PAC-Session-ID"] == "pac-session"
        calls = {}
        for frame in frames:
            for delta in frame["choices"][0]["delta"].get("tool_calls", []):
                call = calls.setdefault(delta["index"], {"id": "", "name": "", "arguments": ""})
                call["id"] += delta.get("id", "")
                call["name"] += delta.get("function", {}).get("name", "")
                call["arguments"] += delta.get("function", {}).get("arguments", "")
        assert calls == {0: {"id": "call_git", "name": "bash", "arguments": '{"command":"git status"}'}}
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
        body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{
            "id": calls[0]["id"], "type": "function", "function": {
                "name": calls[0]["name"], "arguments": calls[0]["arguments"]}}]},
            {"role": "tool", "tool_call_id": "call_git", "content": "Working tree clean"}]
        _, frames = _send_stream(base, body)
        content = "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames)
        assert content == "The working tree is clean."
        assert frames[-1]["choices"][0]["finish_reason"] == "stop"
        assert len(service.requests) == 2
        assert service.requests[-1][0].messages[-1].role == "tool"


def test_sse_multiple_call_argument_fragments_reconstruct_unicode_and_usage():
    arguments = json.dumps({"command": "شيء" * 400}, ensure_ascii=False)

    class LongCalls(Service):
        def complete(self, request, **kwargs):
            self.requests.append((request, kwargs))
            return Completion({"id": "chat-long", "object": "chat.completion", "created": 1,
                "model": "pac-local", "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"call_{index}", "type": "function", "function": {
                    "name": "bash", "arguments": arguments}} for index in range(2)]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}})

    with _server(LongCalls()) as (service, base):
        body = {**_request(), "stream": True, "stream_options": {"include_usage": True}}
        _, frames = _send_stream(base, body)
        calls = {}
        fragment_count = 0
        for frame in frames:
            if not frame["choices"]:
                continue
            for delta in frame["choices"][0]["delta"].get("tool_calls", []):
                call = calls.setdefault(delta["index"], {"id": "", "name": "", "arguments": ""})
                call["id"] += delta.get("id", "")
                call["name"] += delta["function"].get("name", "")
                call["arguments"] += delta["function"].get("arguments", "")
                fragment_count += 1
        assert fragment_count > 4
        assert [calls[index]["id"] for index in range(2)] == ["call_0", "call_1"]
        assert all(call["arguments"] == arguments and call["name"] == "bash" for call in calls.values())
        assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"
        assert frames[-1]["choices"] == [] and frames[-1]["usage"]["total_tokens"] == 140
        assert len(service.requests) == 1


def test_sse_normal_text_fragments_preserve_the_complete_answer():
    answer = "English and العربية\n" * 80

    class Text(Service):
        def complete(self, request, **kwargs):
            self.requests.append((request, kwargs))
            return Completion({"id": "chat-text", "object": "chat.completion", "created": 1,
                "model": "pac-local", "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": answer}, "finish_reason": "length"}]})

    with _server(Text()) as (service, base):
        _, frames = _send_stream(base, {**_request(), "stream": True})
        assert "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames) == answer
        assert frames[-1]["choices"][0]["finish_reason"] == "length"
        assert len(service.requests) == 1


@pytest.mark.parametrize("raw", [b"not json", b"\xff"], ids=["malformed_json", "invalid_utf8"])
def test_bad_json_is_safe_and_does_not_call_service(raw):
    with _server() as (service, base):
        code, _, result = _send(base, raw=raw)
        assert code == 400 and result["error"]["code"] == "invalid_json"
        assert service.requests == []


def test_deeply_nested_non_request_json_is_refused_without_provider_call():
    with _server() as (service, base):
        code, _, result = _send(base, raw=b"[" * 2000 + b"]" * 2000)
        assert code == 400 and result["error"]["code"] in {"invalid_json", "invalid_request"}
        assert service.requests == []


def test_oversized_body_is_refused_before_reading_or_calling_provider():
    with _server() as (service, base):
        code, _, result = _send(base, raw=b"{}", headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)})
        assert code == 413 and result["error"]["code"] == "request_too_large"
        assert service.requests == []


def test_unknown_and_unexpected_errors_never_expose_exception_or_query_in_logs():
    secret = "sk-private-test-value"
    logged = []
    with _server(log=logged.append) as (service, base):
        service.error = RuntimeError(secret)
        code, _, result = _send(base, body=_request())
        assert code == 500 and secret not in json.dumps(result)
        assert _send(base, path="/unknown?key=" + secret, body={})[0] == 404
    assert logged == ["/v1/chat/completions 500", "/unknown 404"]


def test_typed_error_status_and_code_are_preserved():
    with _server() as (service, base):
        service.error = IntegrationError("context_overflow", "Request exceeds the context window", 400)
        code, _, result = _send(base, body=_request())
        assert code == 400 and result["error"]["code"] == "context_overflow"


def test_http_refuses_public_or_ambiguous_bindings():
    for host in ("0.0.0.0", "localhost", "::1", "192.168.1.1"):
        with pytest.raises(ValueError, match="127.0.0.1"):
            create_server(Service(), host=host, port=0)  # type: ignore[arg-type]


def test_cli_reports_invalid_integration_database_without_raw_exception(monkeypatch, capsys, tmp_path):
    from personal_ai_core.integrations.opencode import __main__ as entry

    def fail(**kwargs):
        raise sqlite3.DatabaseError("private startup data")

    monkeypatch.setattr(entry, "build_service", fail)
    assert entry.main(["--database", str(tmp_path / "opencode.db"),
                       "--profile", str(tmp_path / "profile.md")]) == 2
    captured = capsys.readouterr()
    assert "separate integration database" in captured.err
    assert "private startup data" not in captured.err


def test_profile_and_integration_database_cli_paths_override_environment(tmp_path):
    from personal_ai_core.integrations.opencode.__main__ import parse_args

    args = parse_args(["--profile", str(tmp_path / "explicit.md"),
                       "--database", str(tmp_path / "explicit-opencode.db")], env={
        "PAC_PROFILE": str(tmp_path / "env-profile.md"),
        "PAC_DATABASE": str(tmp_path / "owner" / "core.db"),
        "PAC_OPENCODE_DATABASE": str(tmp_path / "env-opencode.db"),
    })
    assert args.profile == tmp_path / "explicit.md"
    assert args.database == tmp_path / "explicit-opencode.db"
    assert not any(tmp_path.iterdir())


def test_environment_profile_precedes_core_database_directory(tmp_path):
    from personal_ai_core.integrations.opencode.__main__ import parse_args

    args = parse_args([], env={
        "PAC_PROFILE": str(tmp_path / "profile-choice.md"),
        "PAC_DATABASE": str(tmp_path / "owner" / "core.db"),
        "PAC_OPENCODE_DATABASE": str(tmp_path / "opencode-choice.db"),
    })
    assert args.profile == tmp_path / "profile-choice.md"
    assert args.database == tmp_path / "opencode-choice.db"
    assert not any(tmp_path.iterdir())


def test_core_database_environment_only_locates_profile_and_never_sets_integration_database(tmp_path):
    from personal_ai_core.integrations.opencode.__main__ import parse_args

    args = parse_args([], env={"PAC_DATABASE": str(tmp_path / "owner" / "core.db")})
    assert args.profile == tmp_path / "owner" / "profile.md"
    assert args.database == Path.home() / ".personal-ai-core" / "opencode.db"
    assert not any(tmp_path.iterdir())


def test_empty_environment_selects_separate_default_database_without_opening_files():
    from personal_ai_core.integrations.opencode.__main__ import parse_args

    args = parse_args([], env={})
    assert args.profile == Path.home() / ".personal-ai-core" / "profile.md"
    assert args.database == Path.home() / ".personal-ai-core" / "opencode.db"


def test_selected_integration_database_locates_adjacent_profile_unless_explicitly_overridden(tmp_path):
    from personal_ai_core.integrations.opencode.__main__ import parse_args

    selected = tmp_path / "selected" / "opencode.db"
    args = parse_args(["--database", str(selected)], env={"PAC_DATABASE": str(tmp_path / "old" / "core.db")})
    assert args.profile == selected.parent / "profile.md"
    explicit = tmp_path / "owner" / "profile.md"
    args = parse_args(["--database", str(selected)], env={"PAC_PROFILE": str(explicit)})
    assert args.profile == explicit
    assert not any(tmp_path.iterdir())


def test_store_validation_error_is_safe_client_error():
    with _server() as (service, base):
        service.error = IntegrationStoreError("private internal store data")
        code, _, result = _send(base, body=_request())
        assert code == 400 and result["error"]["code"] == "invalid_session"
        assert "private internal store data" not in json.dumps(result)


def test_optional_request_kind_header_preserves_supported_kind_and_rejects_unknown():
    with _server() as (service, base):
        assert _send(base, body=_request(), headers={"X-PAC-Request-Kind": "title"})[0] == 200
        assert service.requests[0][2] == "title"
        code, _, result = _send(base, body=_request(), headers={"X-PAC-Request-Kind": "private-unknown"})
        assert code == 400 and result["error"]["code"] == "invalid_request_kind"
        assert len(service.requests) == 1
