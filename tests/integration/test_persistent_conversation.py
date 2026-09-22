"""A conversation survives the process — ADR-010 D+B, wired.

The in-memory slice has always worked and has always forgotten. This is the
claim that could not be made before and is the whole reason the backend
exists: close the service, open it again on the same file, and the history,
the events and the session are still there.

The tests are written as "restart" rather than "reopen" deliberately. Calling
`connect` twice inside one process proves the rows are on disk; it does not
prove the service composed around them behaves the same afterwards, and that
is the claim a user cares about.

Everything above `core.contracts` is unchanged. That is the substitution the
boundary was built for, and asserting it is what makes the boundary real
rather than aspirational: the same assertions that hold for the in-memory
slice hold here, and one more holds that cannot hold there.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from personal_ai_core.conversation.factory import (
    build_in_memory_service,
    build_persistent_service,
)
from personal_ai_core.core.domain import EventType, Role
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.persistence.sqlite import (
    SqliteMessageRepository,
    SqliteSessionRepository,
    SqliteUserRepository,
)


class RecordingTransport:
    """Answers without a model, and records what it was asked."""

    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": "ok"}}

    @property
    def last(self) -> Mapping[str, Any]:
        assert self.payloads, "the provider was never called"
        return self.payloads[-1]


@pytest.fixture
def database(tmp_path):
    return tmp_path / "core.db"


def test_a_conversation_survives_a_restart(database):
    """The claim the in-memory slice cannot make."""
    first = build_persistent_service(database=database, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.service.send(session_id=session.id, content="remember this")
    first.service.send(session_id=session.id, content="and this")
    first.close()

    # A new process would build a new slice from the same file. This does the
    # same thing: nothing is carried across but the path.
    second = build_persistent_service(database=database, transport=RecordingTransport())
    try:
        assert second.service.create_user() is not None  # the store still works

        # Read through the repository on the slice's own connection rather
        # than reaching into `service._messages`: a test that pokes a private
        # attribute stops testing the wiring the moment the wiring changes.
        stored = SqliteMessageRepository(second.connection).list_for_session(
            session.id
        )
        assert [m.content for m in stored] == [
            "remember this",
            "ok",
            "and this",
            "ok",
        ]
        assert [m.role for m in stored] == [
            Role.USER,
            Role.ASSISTANT,
            Role.USER,
            Role.ASSISTANT,
        ]
    finally:
        second.close()


def test_the_session_and_its_user_survive_a_restart(database):
    first = build_persistent_service(database=database, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.close()

    second = build_persistent_service(database=database, transport=RecordingTransport())
    try:
        assert SqliteSessionRepository(second.connection).get(session.id) == session
        assert SqliteUserRepository(second.connection).get(user.id) == user
    finally:
        second.close()


def test_the_event_trail_survives_a_restart_in_order(database):
    """Events are the audit trail. A trail that does not survive the process
    is a trail for a question nobody asks twice."""
    first = build_persistent_service(database=database, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="one")
    first.close()

    second = build_persistent_service(database=database, transport=RecordingTransport())
    try:
        recorded = [e.type for e in second.events.list_for_session(session.id)]
        assert recorded == [
            EventType.SESSION_STARTED,
            EventType.MESSAGE_RECEIVED,
            EventType.GENERATION_REQUESTED,
            EventType.GENERATION_COMPLETED,
        ]
    finally:
        second.close()


def test_a_restarted_conversation_continues_with_its_history(database):
    """The behavioural consequence, not just the stored rows: the second
    process sends the FIRST process's turns to the model."""
    first = build_persistent_service(database=database, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="my name is Rico")
    first.close()

    transport = RecordingTransport()
    second = build_persistent_service(database=database, transport=transport)
    try:
        second.service.send(session_id=session.id, content="what did I say?")
        sent = [m["content"] for m in transport.last["messages"]]
        assert "my name is Rico" in sent
    finally:
        second.close()


def test_the_persistent_slice_behaves_like_the_in_memory_one_within_a_run(database):
    """Same observable behaviour, so swapping the store cannot change what the
    layers above see. The identity message is first in both, the reply comes
    back in both, and the event sequence matches."""
    memory_transport = RecordingTransport()
    in_memory, memory_events = build_in_memory_service(transport=memory_transport)

    disk_transport = RecordingTransport()
    persistent = build_persistent_service(database=database, transport=disk_transport)
    try:
        results = []
        for service, transport in (
            (in_memory, memory_transport),
            (persistent.service, disk_transport),
        ):
            session = service.start_session(service.create_user().id)
            reply = service.send(session_id=session.id, content="hello")
            results.append(
                (
                    reply.role,
                    [m["role"] for m in transport.last["messages"]],
                    transport.last["messages"][0]["content"],
                )
            )

        assert results[0] == results[1]
        assert results[0][1] == ["system", "user"]
        assert results[0][2] == DefaultIdentityComposer().text

        assert [e.type for e in memory_events.all()] == [
            e.type for e in persistent.events.all()
        ]
    finally:
        persistent.close()


def test_the_database_is_created_where_it_was_asked_for(tmp_path):
    """`database` is required and has no default, because a library that
    writes to a path the caller did not name loses data somewhere the caller
    does not look."""
    path = tmp_path / "nested" / "core.db"
    path.parent.mkdir()

    slice_ = build_persistent_service(database=path, transport=RecordingTransport())
    try:
        assert path.exists()
    finally:
        slice_.close()
