"""External tool transcripts retain their native roles and call/result linkage."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from personal_ai_core.core.domain import Message, Role, ToolDeclaration
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.ollama.provider import OllamaProvider

EXECUTE = ToolDeclaration(
    name="execute",
    description="Run OpenCode tools",
    parameters={"type": "object", "properties": {"code": {"type": "string"}}},
)


def _call(call_id: str, code: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function",
            "function": {"name": "execute", "arguments": {"code": code}}}


def test_tool_continuation_preserves_native_messages_through_the_existing_provider():
    call = _call("call_git", "return await tools.bash({command: 'git status'})")
    transcript = [
        Message(session_id="s", role=Role.SYSTEM, content="PAC identity and OpenCode catalog"),
        Message(session_id="s", role=Role.USER, content="Inspect this repository"),
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_git", "tool_name": "execute",
         "content": "On branch main\nHEAD=fd02696\n"},
    ]
    seen: dict[str, Any] = {}

    def transport(url, payload, timeout):
        seen.update(json.loads(json.dumps(payload)))
        return {"message": {"content": "main is clean at fd02696"}}

    response = OllamaProvider("http://local", transport=transport).generate_with_tools(
        model="boss", messages=transcript, tools=[EXECUTE], options={"num_ctx": 8192},
    )

    assert seen["messages"] == [
        {"role": "system", "content": transcript[0].content},
        {"role": "user", "content": transcript[1].content},
        transcript[2], transcript[3],
    ]
    assert seen["messages"][2]["tool_calls"][0]["function"]["arguments"] == call["function"]["arguments"]
    assert seen["tools"][0]["function"]["name"] == "execute"
    assert seen["options"] == {"num_ctx": 8192}
    assert response.text == "main is clean at fd02696"


def test_parallel_results_keep_their_ids_even_when_returned_in_a_different_order():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("call_status", "git status"), _call("call_head", "git rev-parse HEAD")]},
        {"role": "tool", "content": "fd02696", "tool_call_id": "call_head", "tool_name": "execute"},
        {"role": "tool", "content": "clean", "tool_call_id": "call_status", "tool_name": "execute"},
    ]
    seen: dict[str, Any] = {}

    def transport(url, payload, timeout):
        seen.update(payload)
        return {"message": {"content": "done"}}

    OllamaProvider("http://local", transport=transport).generate_with_tools(
        model="boss", messages=messages, tools=[EXECUTE],
    )
    assert seen["messages"] == messages


def test_structured_messages_also_work_for_generate_without_tools():
    messages = [{"role": "system", "content": "identity"},
                {"role": "user", "content": "hello"}]
    seen: dict[str, Any] = {}

    def transport(url, payload, timeout):
        seen.update(payload)
        return {"message": {"content": "hi"}}

    response = OllamaProvider("http://local", transport=transport).generate(
        model="boss", messages=messages,
    )
    assert seen["messages"] == messages
    assert "tools" not in seen
    assert response.text == "hi"


def test_transport_cannot_mutate_the_callers_structured_history():
    call = _call("call_one", "return 42")
    call["function"]["arguments"]["nested"] = {"items": [False, None, 1.25, "عربي"]}
    call["function"]["index"] = 0
    messages = [{"role": "assistant", "content": "", "tool_calls": [call]}]
    original = copy.deepcopy(messages)

    def transport(url, payload, timeout):
        payload["messages"][0]["tool_calls"][0]["function"]["arguments"]["nested"]["items"].append("mutation")
        return {"message": {"content": "done"}}

    OllamaProvider("http://local", transport=transport).generate_with_tools(
        model="boss", messages=messages, tools=[EXECUTE],
    )
    assert messages == original


@pytest.mark.parametrize("message", [
    {"role": "developer", "content": "unsupported role"},
    {"role": "user", "content": [{"type": "text", "text": "not native content"}]},
    {"role": "user", "content": "hi", "unsupported": "must not disappear"},
    {"role": "assistant", "content": None},
    {"role": "assistant", "content": "", "tool_calls": "not a list"},
    {"role": "user", "content": "", "tool_calls": []},
    {"role": "assistant", "content": "", "tool_call_id": "call_one"},
    {"role": "tool", "content": "unlinked result"},
    {"role": "tool", "content": "result", "tool_call_id": ""},
    {"role": "tool", "content": "result", "tool_name": 7},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_one", "function": {"name": "execute", "arguments": '{"code":"42"}'}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_one", "type": "custom", "function": {"name": "execute", "arguments": {}}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "execute", "arguments": {"value": float("nan")}}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "execute", "arguments": {"value": (1, 2)}}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "execute", "arguments": {2: "not a JSON key"}}}]},
])
def test_unsupported_transcripts_fail_before_any_model_request(message):
    invoked = False

    def transport(url, payload, timeout):
        nonlocal invoked
        invoked = True
        return {"message": {"content": "unexpected"}}

    with pytest.raises(ProviderError) as caught:
        OllamaProvider("http://local", transport=transport).generate_with_tools(
            model="boss", messages=[message], tools=[EXECUTE],
        )
    assert caught.value.kind == "invalid_request"
    assert invoked is False
