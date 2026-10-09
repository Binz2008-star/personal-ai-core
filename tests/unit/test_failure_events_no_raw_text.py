"""A failure event keeps a classification, never the error's text.

Gap analysis P1-8. GENERATION_FAILED stored `str(exc)` and RETRIEVAL_FAILED
the retriever's message, and a transport's message names the host, the port
and whatever the server answered; a retriever's can quote a document path, a
database host or the text it failed on. A row in the event store outlives the
terminal the message was printed on, so it now keeps the error's type, a kind
from a fixed list and an HTTP status -- and the person at the terminal still
gets the message, from the exception.
"""
from __future__ import annotations

import ast
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from personal_ai_core.context import NullRedactor, ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator
from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.conversation.factory import build_in_memory_service
from personal_ai_core.conversation.grounding import ContextBuilder
from personal_ai_core.conversation.service import ConversationService
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from personal_ai_core.runtime.llamacpp import provider as llamacpp
from personal_ai_core.runtime.model_registry import ModelRegistry
from personal_ai_core.runtime.ollama import provider as ollama
from personal_ai_core.runtime.ollama.provider import OllamaProvider

RUNTIME = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core" / "runtime"

# What a transport or retriever message can carry. Obviously fake.
HOST = "10.1.2.3"
PATH = "/home/owner/private/notes.md"
TOKEN = "fake-token-for-tests"
SENSITIVE = f"<urlopen error to http://{HOST}:11434{PATH}?token={TOKEN}>"

QUESTION = "ما الفرق بين الذاكرة قصيرة المدى والذاكرة طويلة المدى؟"
CHINESE = "。提供的信息中没有提到您的笔记本电脑的序列号。"


class Scripted:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)

    def __call__(self, url, payload, timeout):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"model": payload["model"], "message": {"content": reply}, "done_reason": "stop"}


def _all_payloads(events, session_id) -> str:
    # Payloads are read-only mappings; `dict` is how json reads one.
    return json.dumps([e.payload for e in events.list_for_session(session_id)],
                      ensure_ascii=False, default=dict)


def _failed(events, session_id, kind: EventType) -> list[dict]:
    return [e.payload for e in events.list_for_session(session_id) if e.type is kind]


# --- generation ---------------------------------------------------------------------


def test_a_failed_generation_keeps_its_kind_and_the_terminal_keeps_the_message():
    failure = ProviderError(f"ollama request failed: {SENSITIVE}", kind="timeout")
    service, events = build_in_memory_service(transport=Scripted(failure))
    session = service.start_session(service.create_user().id)

    with pytest.raises(ProviderError) as caught:
        service.send(session_id=session.id, content="hello")

    assert SENSITIVE in str(caught.value)
    [payload] = _failed(events, session.id, EventType.GENERATION_FAILED)
    assert payload == {"model": payload["model"], "error_type": "ProviderError",
                       "error_kind": "timeout", "status": None}
    stored = _all_payloads(events, session.id)
    assert HOST not in stored and PATH not in stored and TOKEN not in stored


def test_a_failed_retry_keeps_its_kind_too():
    failure = ProviderError(f"ollama request failed: HTTP Error 500 {SENSITIVE}",
                            kind="http_status", status=500)
    service, events = build_in_memory_service(Settings.from_env({}),
                                              transport=Scripted(CHINESE, failure))
    session = service.start_session(service.create_user().id)

    with pytest.raises(ProviderError):
        service.send(session_id=session.id, content=QUESTION)

    [payload] = _failed(events, session.id, EventType.GENERATION_FAILED)
    assert payload["attempt"] == 2
    assert (payload["error_kind"], payload["status"]) == ("http_status", 500)
    assert "error" not in payload
    assert HOST not in _all_payloads(events, session.id)


# --- retrieval ----------------------------------------------------------------------


def test_a_failed_retrieval_keeps_the_errors_type_only():
    class Broken:
        def retrieve(self, query):
            raise RuntimeError(f"could not read {PATH} from db host {HOST}")

    estimator = ScriptAwareTokenEstimator()
    budget_policy = ReserveBasedBudgetPolicy()
    events = InMemoryEventRepository()
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=OllamaProvider("http://unused", transport=Scripted("unused")),
        registry=ModelRegistry.from_settings(Settings(), provider="ollama"),
        budget_policy=budget_policy,
        estimator=estimator,
        identity=DefaultIdentityComposer(),
        context_builder=ContextBuilder(
            retriever=Broken(),
            assembler=HybridContextAssembler(estimator),
            budget_policy=budget_policy,
            estimator=estimator, redactor=NullRedactor(),
        ),
    )
    session = service.start_session(service.create_user().id)

    with pytest.raises(RuntimeError):
        service.send(session_id=session.id, content="what is in my notes?")

    assert _failed(events, session.id, EventType.RETRIEVAL_FAILED) == [
        {"error_type": "RuntimeError"}]
    stored = _all_payloads(events, session.id)
    assert HOST not in stored and PATH not in stored


# --- what the providers classify -------------------------------------------------------


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _raising(exc: BaseException):
    def urlopen(request, timeout=None):
        raise exc
    return urlopen


@pytest.mark.parametrize("transport", [ollama.http_transport, llamacpp.http_transport],
                         ids=["ollama", "llamacpp"])
@pytest.mark.parametrize(("raised", "kind", "status"), [
    (urllib.error.HTTPError("http://x.invalid", 404, "Not Found", None, None), "http_status", 404),  # type: ignore[arg-type]
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "unreachable", None),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", None),
    (TimeoutError("The read operation timed out"), "timeout", None),
    (OSError("network is down"), "unreachable", None),
], ids=["http-404", "refused", "connect-timeout", "read-timeout", "oserror"])
def test_a_transport_failure_is_classified(monkeypatch, transport, raised, kind, status):
    monkeypatch.setattr(urllib.request, "urlopen", _raising(raised))
    with pytest.raises(ProviderError) as caught:
        transport("http://x.invalid/api/chat", {}, 5)
    assert (caught.value.kind, caught.value.status) == (kind, status)


@pytest.mark.parametrize("transport", [ollama.http_transport, llamacpp.http_transport],
                         ids=["ollama", "llamacpp"])
def test_a_response_that_is_not_json_is_an_invalid_response(monkeypatch, transport):
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Body(b"<html>"))
    with pytest.raises(ProviderError) as caught:
        transport("http://x.invalid/api/chat", {}, 5)
    assert caught.value.kind == "invalid_response"


def test_a_provider_error_names_its_kind_from_a_fixed_list():
    assert (ProviderError("x").kind, ProviderError("x").status) == ("unclassified", None)
    with pytest.raises(ValueError):
        ProviderError("x", kind="the server said: hello")


def test_every_provider_failure_in_runtime_names_its_kind():
    """A raise without `kind=` would record "unclassified" and say nothing."""
    unnamed = []
    for path in RUNTIME.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "ProviderError"
                    and not any(k.arg == "kind" for k in node.keywords)):
                unnamed.append(f"{path.name}:{node.lineno}")
    assert unnamed == []
