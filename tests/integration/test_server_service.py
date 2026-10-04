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
from typing import Any, Mapping

import pytest
from support.postgres_target import (
    NO_SERVER_IDENTITY,
    reset_to_empty,
    server_identity,
    server_url,
)

from personal_ai_core.conversation.factory import (
    build_in_memory_service,
    build_server_service,
)
from personal_ai_core.core.domain import EventType, Role
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.persistence.postgres import (
    DatabaseIdentity,
    PostgresMessageRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    SchemaIntent,
    approve_test_database,
    drop_all,
)

DRIVER_PRESENT = importlib.util.find_spec("psycopg") is not None
# Resolved through the one module that owns this decision, at COLLECTION time:
# it returns "" when no server is advertised and RAISES when POSTGRES_TEST_URL
# names the same database as DATABASE_URL. A skip would render that
# misconfiguration as "no server available" and nobody would look twice.
URL = server_url()
# Non-optional, so the constant can be handed to `connect(identity=...)` with
# no way for the confirmation to go missing. `NO_SERVER_IDENTITY` is the
# no-server fallback and the skip marker below means it is never reached; if it
# ever were, `connect` would refuse it loudly.
IDENTITY: DatabaseIdentity = server_identity() or NO_SERVER_IDENTITY

pytestmark = pytest.mark.skipif(
    not (DRIVER_PRESENT and URL),
    reason="no PostgreSQL server reached; run with POSTGRES_TEST_URL set",
)


@pytest.fixture(scope="module", autouse=True)
def _known_starting_point():
    """Establish a VACUOUS database before any test in this module runs.

    The same bootstrap, and the same module scope, as the backend conformance
    module -- for the same reason. The first build below says INITIALIZE, and
    INITIALIZE is only legal against an empty database, so a leftover from an
    interrupted run would fail every test here for a reason that has nothing to
    do with what they test.

    This is not cosmetic. `tests/integration/` is collected BEFORE
    `tests/unit/`, so before this fixture existed this module ran FIRST and
    inherited whatever the previous run left behind -- which is how a stale
    table on a disposable server produced six failures that had nothing to do
    with the server slice and then vanished on the next run, once the unit
    module happened to reset the database first. A suite whose result depends
    on directory ordering is not reporting on the code.

    Module-scoped rather than per-test, so a relation leaked by one of these
    tests still shows up: the next INITIALIZE refuses it as OCCUPIED instead of
    quietly absorbing it.
    """
    reset_to_empty(URL)


def build(*, intent: SchemaIntent, **kwargs: Any):
    """`build_server_service` against the test database, with intent declared.

    The builder forwards both `intent` and `identity` to `connect`, and neither
    has a default. That is the point of the gate: a caller that has not said
    whether it is creating a schema or using one has not consented, and a
    confirmation read off the URL would agree with that URL by construction.

    So every build below says which it is, and the first build of a test says
    INITIALIZE while every later one says OPERATE. The second build of a
    conversation is not re-initializing anything; it is opening a database that
    already holds this project's schema.
    """
    return build_server_service(
        database_url=URL, intent=intent, identity=IDENTITY, **kwargs
    )


def clean_up(slice_) -> None:
    """Drop the tables, with an approval minted against the live connection."""
    drop_all(slice_.connection, approval=approve_test_database(slice_.connection))


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
    first = build(intent=SchemaIntent.INITIALIZE, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.service.send(session_id=session.id, content="remember this")
    first.service.send(session_id=session.id, content="and this")
    first.close()

    # A new process would build a new slice from the same database URL. This
    # does the same thing: nothing is carried across but the URL.
    second = build(intent=SchemaIntent.OPERATE, transport=RecordingTransport())
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
        clean_up(second)
        second.close()


def test_the_session_and_its_user_survive_a_rebuild():
    first = build(intent=SchemaIntent.INITIALIZE, transport=RecordingTransport())
    user = first.service.create_user()
    session = first.service.start_session(user.id)
    first.close()

    second = build(intent=SchemaIntent.OPERATE, transport=RecordingTransport())
    try:
        assert PostgresSessionRepository(second.connection).get(session.id) == session
        assert PostgresUserRepository(second.connection).get(user.id) == user
    finally:
        clean_up(second)
        second.close()


def test_the_event_trail_survives_a_rebuild_in_order():
    """Events are the audit trail. A trail that does not survive the process
    is a trail for a question nobody asks twice."""
    first = build(intent=SchemaIntent.INITIALIZE, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="one")
    first.close()

    second = build(intent=SchemaIntent.OPERATE, transport=RecordingTransport())
    try:
        recorded = [e.type for e in second.events.list_for_session(session.id)]
        assert recorded == [
            EventType.SESSION_STARTED,
            EventType.MESSAGE_RECEIVED,
            # Every turn is measured against the window, ungrounded ones too.
            EventType.CONTEXT_ASSEMBLED,
            EventType.GENERATION_REQUESTED,
            EventType.GENERATION_COMPLETED,
        ]
    finally:
        clean_up(second)
        second.close()


def test_a_restarted_conversation_continues_with_its_history():
    """The behavioural consequence, not just the stored rows: the second
    build sends the FIRST build's turns to the model."""
    first = build(intent=SchemaIntent.INITIALIZE, transport=RecordingTransport())
    session = first.service.start_session(first.service.create_user().id)
    first.service.send(session_id=session.id, content="my name is Rico")
    first.close()

    transport = RecordingTransport()
    second = build(intent=SchemaIntent.OPERATE, transport=transport)
    try:
        second.service.send(session_id=session.id, content="what did I say?")
        sent = [m["content"] for m in transport.last["messages"]]
        assert "my name is Rico" in sent
    finally:
        clean_up(second)
        second.close()


def test_the_server_slice_behaves_like_the_in_memory_one_within_a_run():
    """Same observable behaviour, so swapping the store cannot change what the
    layers above see. The identity message is first in both, the reply comes
    back in both, and the event sequence matches."""
    memory_transport = RecordingTransport()
    in_memory, memory_events = build_in_memory_service(transport=memory_transport)

    server_transport = RecordingTransport()
    server = build(intent=SchemaIntent.INITIALIZE, transport=server_transport)
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
        clean_up(server)
        server.close()


def test_the_grounded_server_slice_hands_back_the_durable_store():
    """Wiring is observable, not assumed: a turn through the factory-built
    service reports recall configured, and the durable store is handed back."""
    slice_ = build(
        intent=SchemaIntent.INITIALIZE, transport=RecordingTransport(), grounded=True
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
        clean_up(slice_)
        slice_.close()