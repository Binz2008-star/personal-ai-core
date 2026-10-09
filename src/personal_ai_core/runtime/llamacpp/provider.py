"""LlamaCppProvider -- a ModelProvider backed by a local llama-server.

Speaks llama-server's OpenAI-compatible chat endpoint, which also accepts
llama.cpp's own sampling fields and a `grammar`. The transport is injected,
as in the Ollama adapter, so it is testable without a server.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from ...core.domain import Message, ModelResponse
from ...core.errors import ProviderError
from ..failures import transport_failure, with_one_retry

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
        kind, status = transport_failure(exc)
        raise ProviderError(f"llama-server request failed: {exc}", kind=kind, status=status) from exc
    except http.client.IncompleteRead as exc:
        # The server answered and the body was cut short: what came is not a
        # reply, and asking again would wait out the whole generation twice.
        raise ProviderError(f"llama-server reply was cut short: {exc!r}",
                            kind="invalid_response") from exc
    except http.client.HTTPException as exc:
        # Not an HTTP reply at all (BadStatusLine and the like): as with a
        # refused connection, nothing usable answered at that address.
        kind, status = transport_failure(exc)
        raise ProviderError(f"llama-server request failed: {exc!r}", kind=kind, status=status) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError(f"llama-server returned invalid JSON: {exc}",
                            kind="invalid_response") from exc


class LlamaCppProvider:
    """Implements `core.contracts.ModelProvider`."""

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: int = 120,
        transport: Transport | None = None,
        grammar: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout_seconds
        self._transport: Transport = transport or http_transport
        self._grammar = grammar
        self._sleep = sleep

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
            raise ProviderError("generate() requires at least one message", kind="invalid_request")
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

        raw = with_one_retry(lambda: self._transport(f"{self._host}/v1/chat/completions", payload, self._timeout),
                             sleep=self._sleep)
        if not isinstance(raw, Mapping):
            # Valid JSON, but `[1, 2]`, `null` or `"hi"` is not a chat reply.
            raise ProviderError(
                f"llama-server response is not a JSON object: {type(raw).__name__}",
                kind="invalid_response",
            )
        try:
            choice = raw["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"llama-server response missing choices[0].message.content; got keys: {sorted(raw)}",
                kind="invalid_response",
            ) from exc
        if not isinstance(choice, Mapping) or not isinstance(text, str):
            # A null or a number is not a reply: past here it would reach the
            # language guard, or the store, as text.
            raise ProviderError(
                f"llama-server response choices[0].message.content is not text: {type(text).__name__}",
                kind="invalid_response",
            )
        usage = raw.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        return ModelResponse(
            text=text,
            model=raw.get("model", model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
            raw=raw,
        )
