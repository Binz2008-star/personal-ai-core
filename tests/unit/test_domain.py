"""Domain contract tests: values are immutable and events are evidence."""
import dataclasses

import pytest

from personal_ai_core.core.domain import (
    UNDETERMINED_LANGUAGE,
    Event,
    EventType,
    Message,
    Role,
    Session,
    SessionStatus,
    User,
)


def test_domain_objects_are_frozen():
    for obj in (
        User(),
        Session(user_id="u"),
        Message(session_id="s", role=Role.USER, content="c"),
        Event(session_id="s", type=EventType.SESSION_STARTED),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, "id", "mutated")


def test_event_payload_cannot_be_mutated_after_recording():
    payload = {"model": "x"}
    event = Event(session_id="s", type=EventType.GENERATION_REQUESTED, payload=payload)
    with pytest.raises(TypeError):
        event.payload["model"] = "y"  # type: ignore[index]  # the point
    # mutating the caller's dict must not reach the recorded event
    payload["model"] = "z"
    assert event.payload["model"] == "x"


def test_message_carries_language_defaulting_to_undetermined():
    assert Message(session_id="s", role=Role.USER, content="c").language == UNDETERMINED_LANGUAGE
    assert Message(session_id="s", role=Role.USER, content="مرحبا", language="ar").language == "ar"


def test_closing_a_session_returns_a_new_value():
    session = Session(user_id="u")
    closed = session.closed()
    assert session.status is SessionStatus.ACTIVE
    assert closed.status is SessionStatus.CLOSED
    assert closed.id == session.id


def test_ids_are_unique():
    assert User().id != User().id
