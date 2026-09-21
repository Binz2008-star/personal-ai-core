"""The `Event != Memory` invariant (ADR-003).

Rico's audited implementation lets a conversation turn call `add_memory(...)`
directly, so a passing remark becomes durable memory with no promotion step.
These tests assert the Core does not inherit that.
"""
import pytest

from personal_ai_core.conversation.factory import build_in_memory_service
from personal_ai_core.core.errors import InvariantViolation
from personal_ai_core.persistence.in_memory import SealedMemoryStore


def fake_transport(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": "ok"}, "done_reason": "stop"}


def test_a_conversation_turn_never_attempts_a_memory_write():
    memory = SealedMemoryStore()
    service, events = build_in_memory_service(transport=fake_transport)

    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="remember that I prefer Arabic")
    service.send(session_id=session.id, content="my name is Roben")
    service.close_session(session.id)

    # Events were recorded ...
    assert len(events.list_for_session(session.id)) > 0
    # ... and not one of them reached for memory.
    assert memory.attempted_writes == 0


def test_the_conversation_service_has_no_memory_collaborator():
    """Structural, not behavioural: there is no path to wire memory in.

    A memory write cannot happen by accident if the service was never given a
    memory store to write to.
    """
    service, _ = build_in_memory_service(transport=fake_transport)
    assert not any("memory" in attr.lower() for attr in vars(service))


def test_the_sealed_store_refuses_writes_loudly():
    memory = SealedMemoryStore()
    with pytest.raises(InvariantViolation, match="Event != Memory"):
        memory.write(
            {"type": "preference", "content": "prefers Arabic"}  # type: ignore[arg-type]
        )
    assert memory.attempted_writes == 1


def test_events_are_append_only_with_no_promotion_side_effect():
    service, events = build_in_memory_service(transport=fake_transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="I always want concise answers")

    recorded = events.list_for_session(session.id)
    # Events are evidence of what was said -- they carry no promoted conclusion.
    for event in recorded:
        assert "memory" not in event.type.value
        assert "memory_id" not in event.payload
        assert "promoted" not in event.payload
