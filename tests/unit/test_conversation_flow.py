"""The Phase 1 vertical slice, end to end, with a fake transport."""
import pytest

from personal_ai_core.conversation.factory import build_in_memory_service
from personal_ai_core.core.domain import EventType, Role, SessionStatus
from personal_ai_core.core.errors import ProviderError


def fake_transport(reply="hello back"):
    def transport(url, payload, timeout):
        return {
            "model": payload["model"],
            "message": {"role": "assistant", "content": reply},
            "prompt_eval_count": 5,
            "eval_count": 2,
            "done_reason": "stop",
        }

    return transport


def test_full_slice_user_session_message_provider_response_event():
    service, events = build_in_memory_service(transport=fake_transport())

    user = service.create_user()
    session = service.start_session(user.id)
    reply = service.send(session_id=session.id, content="hello")

    assert reply.role is Role.ASSISTANT
    assert reply.content == "hello back"

    history = service.history(session.id)
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]

    recorded = [e.type for e in events.list_for_session(session.id)]
    assert recorded == [
        EventType.SESSION_STARTED,
        EventType.MESSAGE_RECEIVED,
        EventType.GENERATION_REQUESTED,
        EventType.GENERATION_COMPLETED,
    ]


def test_generation_uses_the_boss_model():
    service, events = build_in_memory_service(transport=fake_transport())
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hi")

    requested = next(
        e for e in events.all() if e.type is EventType.GENERATION_REQUESTED
    )
    assert requested.payload["model"] == "huihui_ai/qwen2.5-abliterate:7b"
    assert requested.payload["provider"] == "ollama"


def test_arabic_message_language_is_preserved_through_the_slice():
    service, _ = build_in_memory_service(transport=fake_transport("مرحبا بك"))
    session = service.start_session(service.create_user().id)
    reply = service.send(session_id=session.id, content="مرحبا", language="ar")

    assert reply.content == "مرحبا بك"
    assert reply.language == "ar"
    assert all(m.language == "ar" for m in service.history(session.id))


def test_conversation_history_accumulates_across_turns():
    service, _ = build_in_memory_service(transport=fake_transport())
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="one")
    service.send(session_id=session.id, content="two")
    assert len(service.history(session.id)) == 4


def test_provider_failure_is_recorded_as_an_event_and_reraised():
    def failing(url, payload, timeout):
        raise ProviderError("ollama down")

    service, events = build_in_memory_service(transport=failing)
    session = service.start_session(service.create_user().id)

    with pytest.raises(ProviderError):
        service.send(session_id=session.id, content="hi")

    types = [e.type for e in events.list_for_session(session.id)]
    assert EventType.GENERATION_FAILED in types
    assert EventType.GENERATION_COMPLETED not in types
    # a failed turn leaves the user message, not a fabricated reply
    assert [m.role for m in service.history(session.id)] == [Role.USER]


def test_closing_a_session_records_an_event():
    service, events = build_in_memory_service(transport=fake_transport())
    session = service.start_session(service.create_user().id)
    closed = service.close_session(session.id)
    assert closed.status is SessionStatus.CLOSED
    assert events.list_for_session(session.id)[-1].type is EventType.SESSION_CLOSED


def test_unknown_session_is_rejected():
    service, _ = build_in_memory_service(transport=fake_transport())
    with pytest.raises(KeyError):
        service.send(session_id="nope", content="hi")


def test_has_session_agrees_with_send():
    """F-2: the agent asks this before recording; it must refuse exactly
    the sessions `send` refuses."""
    service, _ = build_in_memory_service(transport=fake_transport())
    session = service.start_session(service.create_user().id)
    assert service.has_session(session.id)
    assert not service.has_session("nope")
    with pytest.raises(KeyError):
        service.send(session_id="nope", content="hi")
