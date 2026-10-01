"""LlamaCppProvider -- a ModelProvider backed by a local llama-server.

Speaks llama-server's OpenAI-compatible chat endpoint, which also accepts
llama.cpp's own sampling fields and a `grammar`. The transport is injected,
as in the Ollama adapter, so it is testable without a server.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from ...core.domain import Message, ModelResponse
from ...core.errors import ProviderError

Transport = Callable[[str, Mapping[str, Any], int], Mapping[str, Any]]

# Core's option names that llama-server spells differently.
_RENAMED = {"num_predict": "max_tokens"}
# Passed through unchanged; anything else is dropped rather than guessed at.
_PASSED = {"temperature", "top_p", "top_k", "repeat_penalty", "seed"}


def http_transport(url: str, payload: Mapping[str, Any], timeout: int) -> Mapping[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"llama-server request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"llama-server returned invalid JSON: {exc}") from exc


class LlamaCppProvider:
    """Implements `core.contracts.ModelProvider`."""

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: int = 120,
        transport: Transport | None = None,
        grammar: str | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout_seconds
        self._transport: Transport = transport or http_transport
        self._grammar = grammar

    @property
    def name(self) -> str:
        return "llamacpp"

    def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        if not messages:
            raise ProviderError("generate() requires at least one message")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
        }
        for key, value in (options or {}).items():
            if key in _RENAMED:
                payload[_RENAMED[key]] = value
            elif key in _PASSED:
                payload[key] = value
        if self._grammar:
            payload["grammar"] = self._grammar

        raw = self._transport(f"{self._host}/v1/chat/completions", payload, self._timeout)
        try:
            choice = raw["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"llama-server response missing choices[0].message.content; got keys: {sorted(raw)}"
            ) from exc
        usage = raw.get("usage") or {}
        return ModelResponse(
            text=text,
            model=raw.get("model", model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
            raw=raw,
        )
