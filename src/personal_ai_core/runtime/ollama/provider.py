"""OllamaProvider — a ModelProvider backed by a local Ollama server.

Transport is injected. That keeps the provider testable without a live model,
a GPU or a network, and keeps the HTTP detail in one place rather than
duplicated across modules -- the failure found in the audited source, where
embedding calls were hard-wired in three files.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ...core.domain import Message, ModelResponse, NativeToolCall, ToolDeclaration
from ...core.errors import ProviderError
from ..failures import transport_failure, with_one_retry

# A transport takes (url, payload, timeout) and returns a decoded JSON object.
Transport = Callable[[str, Mapping[str, Any], int], Mapping[str, Any]]


def http_transport(url: str, payload: Mapping[str, Any], timeout: int) -> Mapping[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        kind, status = transport_failure(exc)
        raise ProviderError(f"ollama request failed: {exc}", kind=kind, status=status) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"ollama returned invalid JSON: {exc}",
                            kind="invalid_response") from exc


class OllamaProvider:
    """Implements `core.contracts.ModelProvider` and `ToolCallingProvider` (ADR-025)."""

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: int = 120,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout_seconds
        self._transport: Transport = transport or http_transport
        self._sleep = sleep

    @property
    def name(self) -> str:
        return "ollama"

    def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message | Mapping[str, Any]],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        if not messages:
            raise ProviderError("generate() requires at least one message", kind="invalid_request")
        raw, message = self._chat(self._payload(model, messages, options))
        if "content" not in message:
            raise ProviderError(
                "ollama response missing message.content; "
                f"got keys: {sorted(raw)}",
                kind="invalid_response",
            )
        return self._response(raw, message, model, message["content"], ())

    def generate_with_tools(
        self,
        *,
        model: str,
        messages: Sequence[Message | Mapping[str, Any]],
        tools: Sequence[ToolDeclaration],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        """`generate` with `tools` declared natively (ADR-025 §4.3).

        Each declaration becomes one entry of the request's `tools`, in the
        form the model's template renders. Every call in `message.tool_calls`
        is returned as it came: nothing is coerced, and an entry of the wrong
        shape is returned with no name, so the caller refuses it.
        """
        if not messages:
            raise ProviderError("generate_with_tools() requires at least one message",
                                kind="invalid_request")
        payload = self._payload(model, messages, options)
        payload["tools"] = [
            {"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": dict(t.parameters)}}
            for t in tools
        ]
        raw, message = self._chat(payload)
        calls = _native_calls(message.get("tool_calls"))
        content = message.get("content")
        if content is None and not calls:
            raise ProviderError(
                "ollama response has neither message.content nor message.tool_calls; "
                f"got keys: {sorted(raw)}",
                kind="invalid_response",
            )
        return self._response(raw, message, model, content if isinstance(content, str) else "", calls)

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _payload(
        model: str,
        messages: Sequence[Message | Mapping[str, Any]],
        options: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_payload(message) for message in messages],
            "stream": False,
        }
        if options:
            payload["options"] = dict(options)
        return payload

    def _chat(self, payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        raw = with_one_retry(lambda: self._transport(f"{self._host}/api/chat", payload, self._timeout),
                             sleep=self._sleep)
        message = raw.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError(
                "ollama response missing message.content; "
                f"got keys: {sorted(raw)}",
                kind="invalid_response",
            )
        return raw, message

    @staticmethod
    def _response(
        raw: Mapping[str, Any],
        message: Mapping[str, Any],
        model: str,
        text: str,
        calls: tuple[NativeToolCall, ...],
    ) -> ModelResponse:
        return ModelResponse(
            text=text,
            model=raw.get("model", model),
            prompt_tokens=raw.get("prompt_eval_count"),
            completion_tokens=raw.get("eval_count"),
            finish_reason=raw.get("done_reason"),
            raw=raw,
            tool_calls=calls,
        )


def _message_payload(message: Message | Mapping[str, Any]) -> dict[str, Any]:
    """Keep native tool history at the transport boundary without changing Core roles.

    Ollama 0.35 accepts call IDs, object arguments and tool result linkage in
    ``/api/chat`` messages. OpenCode executes the tools; this adapter only sends
    their transcript to the model. Unsupported fields fail before transport.
    """
    if isinstance(message, Message):
        return {"role": message.role.value, "content": message.content}
    if not isinstance(message, Mapping):
        raise ProviderError("ollama message must be a Message or mapping", kind="invalid_request")
    allowed = {"role", "content", "tool_calls", "tool_call_id", "tool_name"}
    if set(message) - allowed:
        raise ProviderError("ollama message has unsupported fields", kind="invalid_request")
    role = message.get("role")
    if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
        raise ProviderError("ollama message has an unsupported role", kind="invalid_request")
    if not isinstance(message.get("content"), str):
        raise ProviderError("ollama message.content must be a string", kind="invalid_request")
    if "tool_calls" in message:
        calls = message["tool_calls"]
        if role != "assistant" or not isinstance(calls, list):
            raise ProviderError("ollama tool_calls require an assistant message and a list",
                                kind="invalid_request")
        for call in calls:
            _validate_tool_call(call)
    linkage = {"tool_call_id", "tool_name"}.intersection(message)
    if linkage and role != "tool":
        raise ProviderError("ollama tool result linkage requires role=tool", kind="invalid_request")
    if role == "tool" and not linkage:
        raise ProviderError("ollama tool results require a call ID or tool name", kind="invalid_request")
    for key in linkage:
        if not isinstance(message[key], str) or not message[key]:
            raise ProviderError("ollama tool result linkage must be nonempty strings",
                                kind="invalid_request")
    try:
        return _json_copy(message)
    except RecursionError as exc:
        raise ProviderError("ollama message contains cyclic or excessively nested JSON",
                            kind="invalid_request") from exc


def _validate_tool_call(call: Any) -> None:
    if not isinstance(call, Mapping) or set(call) - {"id", "type", "function"}:
        raise ProviderError("ollama tool call has an unsupported shape", kind="invalid_request")
    if "id" in call and (not isinstance(call["id"], str) or not call["id"]):
        raise ProviderError("ollama tool call ID must be a nonempty string", kind="invalid_request")
    if "type" in call and call["type"] != "function":
        raise ProviderError("ollama tool call type must be function", kind="invalid_request")
    function = call.get("function")
    if not isinstance(function, Mapping) or set(function) - {"name", "arguments", "index"}:
        raise ProviderError("ollama tool call function has an unsupported shape", kind="invalid_request")
    if not isinstance(function.get("name"), str) or not function["name"]:
        raise ProviderError("ollama tool call function.name must be nonempty", kind="invalid_request")
    if not isinstance(function.get("arguments"), Mapping):
        raise ProviderError("ollama tool call arguments must be an object", kind="invalid_request")
    if "index" in function and (
        type(function["index"]) is not int or function["index"] < 0
    ):
        raise ProviderError("ollama tool call index must be a nonnegative integer",
                            kind="invalid_request")


def _json_copy(value: Any) -> Any:
    """Copy supported JSON values without coercing arguments or sharing mutable state."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    raise ProviderError("ollama message values must be valid JSON", kind="invalid_request")


def _native_calls(value: Any) -> tuple[NativeToolCall, ...]:
    """Ollama's `message.tool_calls`, raw. A missing field is no call; any
    entry that is not `{"function": {...}}` is kept, with no name."""
    if value is None:
        return ()
    if not isinstance(value, list):
        return (NativeToolCall(name=None, arguments=value),)
    calls = []
    for entry in value:
        function = entry.get("function") if isinstance(entry, Mapping) else None
        if isinstance(function, Mapping):
            calls.append(NativeToolCall(name=function.get("name"),
                                        arguments=function.get("arguments")))
        else:
            calls.append(NativeToolCall(name=None, arguments=entry))
    return tuple(calls)
