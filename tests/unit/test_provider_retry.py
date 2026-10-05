"""One retry when nothing answered, and none for anything else.

Gap analysis P1-7. A single refused or reset connection -- Ollama restarting,
the machine waking -- failed the turn. Now the provider waits two seconds
and asks once more, for `unreachable` only: a timeout has already waited the
whole limit, and a malformed response or a missing model will not change.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from personal_ai_core.core.domain import Message, Role
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.failures import RETRY_DELAY_SECONDS
from personal_ai_core.runtime.llamacpp.provider import LlamaCppProvider
from personal_ai_core.runtime.ollama.provider import OllamaProvider

HELLO = [Message(session_id="s", role=Role.USER, content="hello")]


class Answers:
    """A transport that raises or answers from a list, in order."""

    def __init__(self, *outcomes: Exception | Mapping[str, Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


OLLAMA_OK = {"model": "m", "message": {"content": "hi"}, "done_reason": "stop"}
LLAMACPP_OK = {"model": "m", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}


def _unreachable() -> ProviderError:
    return ProviderError("request failed: [Errno 111] Connection refused", kind="unreachable")


def _ollama(transport, slept):
    return OllamaProvider("http://h", transport=transport, sleep=slept.append)


def _llamacpp(transport, slept):
    return LlamaCppProvider("http://h", transport=transport, sleep=slept.append)


@pytest.mark.parametrize(("build", "ok"), [(_ollama, OLLAMA_OK), (_llamacpp, LLAMACPP_OK)],
                         ids=["ollama", "llamacpp"])
def test_nothing_answering_once_is_asked_again_after_a_pause(build, ok):
    slept: list[float] = []
    transport = Answers(_unreachable(), ok)
    reply = build(transport, slept).generate(model="m", messages=HELLO)
    assert reply.text == "hi"
    assert transport.calls == 2 and slept == [RETRY_DELAY_SECONDS]


@pytest.mark.parametrize("build", [_ollama, _llamacpp], ids=["ollama", "llamacpp"])
def test_nothing_answering_twice_fails_with_the_second_failure(build):
    slept: list[float] = []
    second = _unreachable()
    transport = Answers(_unreachable(), second)
    with pytest.raises(ProviderError) as caught:
        build(transport, slept).generate(model="m", messages=HELLO)
    assert caught.value is second
    assert caught.value.__context__ is None
    assert transport.calls == 2 and slept == [RETRY_DELAY_SECONDS]


@pytest.mark.parametrize("failure", [
    ProviderError("timed out", kind="timeout"),
    ProviderError("HTTP Error 404: Not Found", kind="http_status", status=404),
    ProviderError("invalid JSON", kind="invalid_response"),
    ProviderError("anything else"),
], ids=["timeout", "missing-model", "invalid-response", "unclassified"])
@pytest.mark.parametrize("build", [_ollama, _llamacpp], ids=["ollama", "llamacpp"])
def test_every_other_failure_is_not_retried(build, failure):
    slept: list[float] = []
    transport = Answers(failure)
    with pytest.raises(ProviderError) as caught:
        build(transport, slept).generate(model="m", messages=HELLO)
    assert caught.value is failure
    assert transport.calls == 1 and slept == []


def test_the_pause_is_real_time_by_default():
    import time

    assert OllamaProvider("http://h")._sleep is time.sleep
    assert LlamaCppProvider("http://h")._sleep is time.sleep
