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

from ...core.domain import Message, ModelResponse
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
    """Implements `core.contracts.ModelProvider`."""

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

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
        }
        if options:
            payload["options"] = dict(options)

        raw = self._transport(f"{self._host}/api/chat", payload, self._timeout)

        message = raw.get("message")
        if not isinstance(message, Mapping) or "content" not in message:
            raise ProviderError(
                "ollama response missing message.content; "
                f"got keys: {sorted(raw)}"
            )

        return ModelResponse(
            text=message["content"],
            model=raw.get("model", model),
            prompt_tokens=raw.get("prompt_eval_count"),
            completion_tokens=raw.get("eval_count"),
            finish_reason=raw.get("done_reason"),
            raw=raw,
        )
