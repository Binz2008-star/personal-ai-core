"""OllamaProvider — a ModelProvider backed by a local Ollama server.

Transport is injected. That keeps the provider testable without a live model,
a GPU or a network, and keeps the HTTP detail in one place rather than
duplicated across modules -- the failure found in the audited source, where
embedding calls were hard-wired in three files.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from ...core.domain import Message, ModelResponse, NativeToolCall, ToolDeclaration
from ...core.errors import ProviderError

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
        raise ProviderError(f"ollama request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"ollama returned invalid JSON: {exc}") from exc


class OllamaProvider:
    """Implements `core.contracts.ModelProvider` and `ToolCallingProvider` (ADR-025)."""

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: int = 120,
        transport: Transport | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout_seconds
        self._transport: Transport = transport or http_transport

    @property
    def name(self) -> str:
        return "ollama"

    def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        if not messages:
            raise ProviderError("generate() requires at least one message")
        raw, message = self._chat(self._payload(model, messages, options))
        if "content" not in message:
            raise ProviderError(
                "ollama response missing message.content; "
                f"got keys: {sorted(raw)}"
            )
        return self._response(raw, message, model, message["content"], ())

    def generate_with_tools(
        self,
        *,
        model: str,
        messages: Sequence[Message],
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
            raise ProviderError("generate_with_tools() requires at least one message")
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
                f"got keys: {sorted(raw)}"
            )
        return self._response(raw, message, model, content if isinstance(content, str) else "", calls)

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _payload(
        model: str, messages: Sequence[Message], options: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
        }
        if options:
            payload["options"] = dict(options)
        return payload

    def _chat(self, payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        raw = self._transport(f"{self._host}/api/chat", payload, self._timeout)
        message = raw.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError(
                "ollama response missing message.content; "
                f"got keys: {sorted(raw)}"
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
