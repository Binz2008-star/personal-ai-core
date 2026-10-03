"""ADR-025 §4.1-§4.3: the native tool-call capability of the Ollama adapter.

Fake transport, no live model. What is pinned: ordinary `generate` is unchanged
(no `tools`, no calls); `generate_with_tools` declares the tools in the form the
installed template renders (`{"type": "function", "function": {...}}`) and
returns every call exactly as the backend sent it, never coerced; and the
capability is structural, so a provider without it is not mistaken for one.
"""
from __future__ import annotations

import pytest

from personal_ai_core.core.contracts import ModelProvider, ToolCallingProvider
from personal_ai_core.core.domain import (
    Message,
    ModelResponse,
    NativeToolCall,
    Role,
    ToolDeclaration,
)
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.llamacpp.provider import LlamaCppProvider
from personal_ai_core.runtime.ollama.provider import OllamaProvider

READ_FILE = ToolDeclaration(
    name="read_file", description="read a file [low risk]",
    parameters={"type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"]})


def _messages():
    return [Message(session_id="s", role=Role.SYSTEM, content="rules"),
            Message(session_id="s", role=Role.USER, content="read a.py")]


def _provider(reply: dict, seen: dict) -> OllamaProvider:
    def transport(url, payload, timeout):
        seen["url"], seen["payload"] = url, payload
        return reply
    return OllamaProvider("http://h:11434", transport=transport)


def _ollama(message: dict, **extra) -> dict:
    return {"model": "boss", "message": {"role": "assistant", **message},
            "prompt_eval_count": 7, "eval_count": 5, "done_reason": "stop", **extra}


# --- ordinary generate is unchanged -------------------------------------------


def test_generate_sends_no_tools_and_returns_no_calls_even_if_the_backend_sends_some():
    seen: dict = {}
    reply = _ollama({"content": "hi", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]})
    response = _provider(reply, seen).generate(model="boss", messages=_messages())
    assert "tools" not in seen["payload"]
    assert response.text == "hi" and response.tool_calls == ()


def test_model_response_has_no_calls_by_default():
    assert ModelResponse(text="x", model="m").tool_calls == ()


# --- generate_with_tools -------------------------------------------------------


def test_tools_are_declared_in_the_form_the_template_renders():
    seen: dict = {}
    _provider(_ollama({"content": "done"}), seen).generate_with_tools(
        model="boss", messages=_messages(), tools=[READ_FILE], options={"num_predict": 64})
    assert seen["url"] == "http://h:11434/api/chat"
    assert seen["payload"]["tools"] == [{
        "type": "function",
        "function": {"name": "read_file", "description": "read a file [low risk]",
                     "parameters": READ_FILE.parameters}}]
    assert seen["payload"]["messages"] == [{"role": "system", "content": "rules"},
                                           {"role": "user", "content": "read a.py"}]
    assert seen["payload"]["options"] == {"num_predict": 64}
    assert seen["payload"]["stream"] is False


def test_a_call_is_returned_as_ollama_sent_it():
    reply = _ollama({"content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]})
    response = _provider(reply, {}).generate_with_tools(
        model="boss", messages=_messages(), tools=[READ_FILE])
    assert response.tool_calls == (NativeToolCall(name="read_file", arguments={"path": "a.py"}),)
    assert response.text == ""
    assert (response.prompt_tokens, response.completion_tokens, response.finish_reason) == (7, 5, "stop")


@pytest.mark.parametrize("tool_calls, expected", [
    # A JSON string is not parsed: the caller refuses it (ADR-025 §4.6).
    ([{"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
     (NativeToolCall(name="read_file", arguments='{"path": "a.py"}'),)),
    ([{"function": {"arguments": {"path": "a.py"}}}],
     (NativeToolCall(name=None, arguments={"path": "a.py"}),)),
    ([{"name": "read_file"}], (NativeToolCall(name=None, arguments={"name": "read_file"}),)),
    ("read_file", (NativeToolCall(name=None, arguments="read_file"),)),
    ([{"function": {"name": "a", "arguments": {}}}, {"function": {"name": "b", "arguments": {}}}],
     (NativeToolCall(name="a", arguments={}), NativeToolCall(name="b", arguments={}))),
])
def test_calls_are_never_coerced_or_dropped(tool_calls, expected):
    reply = _ollama({"content": "", "tool_calls": tool_calls})
    response = _provider(reply, {}).generate_with_tools(
        model="boss", messages=_messages(), tools=[READ_FILE])
    assert response.tool_calls == expected


def test_a_plain_answer_has_text_and_no_calls():
    response = _provider(_ollama({"content": "It says 42."}), {}).generate_with_tools(
        model="boss", messages=_messages(), tools=[READ_FILE])
    assert response.text == "It says 42." and response.tool_calls == ()


def test_calls_without_content_are_accepted():
    reply = _ollama({"tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]})
    response = _provider(reply, {}).generate_with_tools(
        model="boss", messages=_messages(), tools=[READ_FILE])
    assert response.text == "" and len(response.tool_calls) == 1


@pytest.mark.parametrize("reply", [
    {"model": "boss", "message": {"role": "assistant"}},
    {"model": "boss"},
])
def test_neither_content_nor_calls_is_a_provider_error(reply):
    with pytest.raises(ProviderError):
        _provider(reply, {}).generate_with_tools(
            model="boss", messages=_messages(), tools=[READ_FILE])


def test_no_messages_is_a_provider_error():
    with pytest.raises(ProviderError):
        _provider(_ollama({"content": "x"}), {}).generate_with_tools(
            model="boss", messages=[], tools=[READ_FILE])


# --- the capability is structural ----------------------------------------------


def test_ollama_has_the_capability_and_llamacpp_does_not():
    assert isinstance(OllamaProvider("http://x"), ToolCallingProvider)
    assert isinstance(OllamaProvider("http://x"), ModelProvider)
    llamacpp = LlamaCppProvider("http://x")
    assert isinstance(llamacpp, ModelProvider)
    assert not isinstance(llamacpp, ToolCallingProvider)


def test_a_provider_with_only_generate_is_not_a_tool_calling_provider():
    class TextOnly:
        name = "text"

        def generate(self, *, model, messages, options=None):
            return ModelResponse(text="", model=model)

    assert isinstance(TextOnly(), ModelProvider)
    assert not isinstance(TextOnly(), ToolCallingProvider)
