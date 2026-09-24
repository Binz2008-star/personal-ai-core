"""A conversation survives the process -- on the server store (ADR-016), wired.

`build_server_service` is the same slice as `build_persistent_service` with
the rows over the wire instead of in a local file. The claim tested here is
the one the SQLite integration tests make, one store up: close the service,
open it again on the same database, and the history, the events and the
session are still there.

Deliberately written as "restart" tests like the SQLite ones: calling
`connect` twice in one process proves the rows are on the server; it does not
prove the service composed around them behaves the same afterwards, and that
is the claim a user cares about.

Server-gated like the backend conformance: the whole module skips unless a
PostgreSQL server is advertised via POSTGRES_TEST_URL -- the same variable,
never defaulted to the production DATABASE_URL.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Any, Mapping

import pytest

from personal_ai_core.conversation.factory import (
    build_in_memory_service,
    build_server_service,
)
from personal_ai_core.core.domain import EventType, Role
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.persistence.postgres import (
    PostgresMessageRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    drop_all,
)

DRIVER_PRESENT = importlib.util.find_spec("psycopg") is not None
# `os.environ.get` without a default types as `str | None`; the empty string
# is equally falsy for the skip gate, so type the value as a `str` and never
# let a possibly-None URL flow into the builder.
TEST_URL: str = os.environ.get("POSTGRES_TEST_URL", "")
URL = TEST_URL

pytestmark = pytest.mark.skipif(
    not (DRIVER_PRESENT and TEST_URL),
    reason="no PostgreSQL server reached; run with POSTGRES_TEST_URL set",
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


def test_a_conversation_survives_a_rebuild():
    """The claim the in-memory slice cannot make, on the server store."""
    first = build_server_service(database_url=URL, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.service.send(session_id=session.id, content="remember this")
    first.service.send(session_id=session.id, content="and this")
    first.close()

    # A new process would build a new slice from the same database URL. This
    # does the same thing: nothing is carried across but the URL.
    second = build_server_service(database_url=URL, transport=RecordingTransport())
    try:
        assert second.service.create_user() is not None  # the store still works

        stored = PostgresMessageRepository(second.connection).list_for_session(
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
        drop_all(second.connection)
        second.close()


def test_the_session_and_its_user_survive_a_rebuild():
    first = build_server_service(database_url=URL, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.close()

    second = build_server_service(database_url=URL, transport=RecordingTransport())
    try:
        assert PostgresSessionRepository(second.connection).get(session.id) == session
        assert PostgresUserRepository(second.connection).get(user.id) == user
    finally:
        drop_all(second.connection)
        second.close()


def test_the_event_trail_survives_a_rebuild_in_order():
    """Events are the audit trail. A trail that does not survive the process
    is a trail for a question nobody asks twice."""
    first = build_server_service(database_url=URL, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="one")
    first.close()

    second = build_server_service(database_url=URL, transport=RecordingTransport())
    try:
        recorded = [e.type for e in second.events.list_for_session(session.id)]
        assert recorded == [
            EventType.SESSION_STARTED,
            EventType.MESSAGE_RECEIVED,
            EventType.GENERATION_REQUESTED,
            EventType.GENERATION_COMPLETED,
        ]
    finally:
        drop_all(second.connection)
        second.close()


def test_a_restarted_conversation_continues_with_its_history():
    """The behavioural consequence, not just the stored rows: the second
    build sends the FIRST build's turns to the model."""
    first = build_server_service(database_url=URL, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="my name is Rico")
    first.close()

    transport = RecordingTransport()
    second = build_server_service(database_url=URL, transport=transport)
    try:
        second.service.send(session_id=session.id, content="what did I say?")
        sent = [m["content"] for m in transport.last["messages"]]
        assert "my name is Rico" in sent
    finally:
        drop_all(second.connection)
        second.close()


def test_the_server_slice_behaves_like_the_in_memory_one_within_a_run():
    """Same observable behaviour, so swapping the store cannot change what the
    layers above see. The identity message is first in both, the reply comes
    back in both, and the event sequence matches."""
    memory_transport = RecordingTransport()
    in_memory, memory_events = build_in_memory_service(transport=memory_transport)

    server_transport = RecordingTransport()
    server = build_server_service(database_url=URL, transport=server_transport)
    try:
        results = []
        for service, transport in (
            (in_memory, memory_transport),
            (server.service, server_transport),
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
            e.type for e in server.events.all()
        ]
    finally:
        drop_all(server.connection)
        server.close()


def test_the_grounded_server_slice_hands_back_the_durable_store():
    """Wiring is observable, not assumed: a turn through the factory-built
    service reports recall configured, and the durable store is handed back."""
    slice_ = build_server_service(
        database_url=URL, transport=RecordingTransport(), grounded=True
    )
    try:
        assert slice_.memories is not None
        user = slice_.service.create_user()
        session = slice_.service.start_session(user.id)
        slice_.service.send(session_id=session.id, content="remember this")

        assembled = [
            e
            for e in slice_.events.list_for_session(session.id)
            if e.type is EventType.CONTEXT_ASSEMBLED
        ]
        assert assembled, "no CONTEXT_ASSEMBLED event recorded"
        payload = assembled[-1].payload
        assert payload["memory_enabled"] is True
        assert payload["memories_retrieved"] == 0
    finally:
        drop_all(slice_.connection)
        slice_.close()