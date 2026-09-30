"""The `Event != Memory` invariant (ADR-003).

Rico's audited implementation lets a conversation turn call `add_memory(...)`
directly, so a passing remark becomes durable memory with no promotion step.
These tests assert the Core does not inherit that.
"""
import pytest

from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.core.errors import InvariantViolation
from personal_ai_core.persistence.in_memory import SealedMemoryStore
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository


def fake_transport(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": "ok"}, "done_reason": "stop"}


def test_a_conversation_turn_never_attempts_a_memory_write(monkeypatch):
    """Behavioural, with a memory store that is really there to be reached.

    Finding T-1: the earlier version counted writes on a `SealedMemoryStore`
    it never handed to anything, so its assertion held whatever the service
    did. Here recall is ON, so a real memory repository exists inside the
    composed system, and every write or supersede on that repository class
    is recorded. The spy is proved live before the conversation runs.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        InMemoryMemoryRepository, "write", lambda self, record: calls.append("write")
    )
    monkeypatch.setattr(
        InMemoryMemoryRepository,
        "supersede",
        lambda self, old_id, new_record: calls.append("supersede"),
    )
    slice_ = build_grounded_in_memory_service(transport=fake_transport, enable_memory=True)
    assert slice_.memories is not None

    # The spy sees a write on the very repository the system holds ...
    slice_.memories.write(None)  # type: ignore[arg-type]
    assert calls == ["write"]
    calls.clear()

    service, events = slice_.service, slice_.events
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="remember that I prefer Arabic")
    service.send(session_id=session.id, content="my name is Roben")
    service.close_session(session.id)

    # ... events were recorded ...
    assert len(events.list_for_session(session.id)) > 0
    # ... and not one turn reached for memory.
    assert calls == []


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
