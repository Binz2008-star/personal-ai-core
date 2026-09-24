"""The Postgres backend stores what the others hold -- ADR-016.

The same two kinds of claim as the SQLite conformance, and one more:

  SAME BEHAVIOUR   the Postgres implementations agree with the in-memory ones
                   on every contract, so swapping the backend cannot change
                   what the layers above see. Written as one parametrised
                   suite over both substrates.

  DURABILITY       what the in-memory store cannot do: survive the process.
                   Asserted by closing the connection and opening another to
                   the same server, exactly as the SQLite file reopens a file.

  PARITY           the ordering behaviours the two durable engines would
                   naturally differ on, pinned so the substrates agree there
                   too -- most importantly the seq-bump of a rewritten memory
                   record (the lesson Phase 5's CI flake taught: two stores
                   that order differently are a defect, not a style choice).

SERVER-GATED
------------
These tests need a real PostgreSQL server, which CI does not advertise. The
whole module skips -- an accounted, named skip -- unless a test URL is given:

  POSTGRES_TEST_URL  required to run; NEVER defaulted to the production
                     DATABASE_URL, so a test run can never reach it by
                     accident.

Every test cleans up after itself: the fixture drops the tables this backend
creates when the test finishes, so a verification run against a server leaves
it exactly as it found it.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from personal_ai_core.core.contracts import (
    EventRepository,
    MemoryStore,
    MessageRepository,
    SessionRepository,
    UserRepository,
)
from personal_ai_core.core.domain import (
    Event,
    EventType,
    Message,
    Role,
    Session,
    SessionStatus,
    User,
)
from personal_ai_core.core.memory import (
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository
from personal_ai_core.persistence.postgres import (
    SCHEMA_VERSION,
    SchemaVersionMismatch,
    PostgresEventRepository,
    PostgresMemoryRepository,
    PostgresMessageRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    connect,
    drop_all,
)

# The two conditions that gate this suite, stated separately so the skip
# reason can say which one is missing. `find_spec` rather than `import`
# because an absent driver must skip, not raise at collection.
DRIVER_PRESENT = importlib.util.find_spec("psycopg") is not None
# `os.environ.get` without a default types as `str | None`; the empty string
# is equally falsy for the skip gate, so type the value as a `str` and never
# let a possibly-None URL flow into connect(URL).
TEST_URL: str = os.environ.get("POSTGRES_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not (DRIVER_PRESENT and TEST_URL),
    reason="no PostgreSQL server reached; run with POSTGRES_TEST_URL set",
)

URL = TEST_URL  # non-None once the module is not skipped


@pytest.fixture
def pg():
    connection = connect(URL)
    yield connection
    drop_all(connection)
    connection.close()


def a_record(content="a memory", status=MemoryStatus.ACTIVE, session="s-1"):
    return MemoryRecord(
        session_id=session,
        type=MemoryType.PREFERENCES,
        content=content,
        language="en",
        status=status,
        confidence=0.9,
        provenance=MemoryProvenance(
            session_id=session, event_id="e-1", promoted_by="rule:test"
        ),
    )


# --- the two implementations agree ----------------------------------------
#
# Each of these runs twice: once against the store that has always been there
# and once against the new one. A difference between them is a difference the
# layers above would see.


@pytest.fixture(params=["in_memory", "postgres"])
def users(request, pg):
    return InMemoryUserRepository() if request.param == "in_memory" else (
        PostgresUserRepository(pg)
    )


@pytest.fixture(params=["in_memory", "postgres"])
def sessions(request, pg):
    return InMemorySessionRepository() if request.param == "in_memory" else (
        PostgresSessionRepository(pg)
    )


@pytest.fixture(params=["in_memory", "postgres"])
def messages(request, pg):
    return InMemoryMessageRepository() if request.param == "in_memory" else (
        PostgresMessageRepository(pg)
    )


@pytest.fixture(params=["in_memory", "postgres"])
def events(request, pg):
    return InMemoryEventRepository() if request.param == "in_memory" else (
        PostgresEventRepository(pg)
    )


@pytest.fixture(params=["in_memory", "postgres"])
def memories(request, pg):
    return InMemoryMemoryRepository() if request.param == "in_memory" else (
        PostgresMemoryRepository(pg)
    )


def test_a_user_round_trips(users):
    user = User()
    users.add(user)
    assert users.get(user.id) == user
    assert users.get("absent") is None


def test_a_session_round_trips_and_updates(sessions):
    session = Session(user_id="u-1")
    sessions.add(session)
    assert sessions.get(session.id) == session

    sessions.update(session.closed())
    assert sessions.get(session.id).status is SessionStatus.CLOSED


def test_updating_an_unknown_session_raises(sessions):
    """Both raise KeyError. An UPDATE matching no row is a silent no-op in
    SQL, which would make one implementation report a lost write and the
    other not."""
    with pytest.raises(KeyError):
        sessions.update(Session(user_id="u-1"))


def test_messages_come_back_in_order_for_their_session(messages):
    first = Message(session_id="s-1", role=Role.USER, content="one")
    second = Message(session_id="s-1", role=Role.ASSISTANT, content="two")
    other = Message(session_id="s-2", role=Role.USER, content="elsewhere")
    for message in (first, second, other):
        messages.add(message)

    assert messages.list_for_session("s-1") == (first, second)
    assert messages.list_for_session("s-2") == (other,)
    assert messages.list_for_session("s-3") == ()


def test_events_come_back_in_order_for_their_session(events):
    first = Event(session_id="s-1", type=EventType.SESSION_STARTED, payload={"a": 1})
    second = Event(session_id="s-1", type=EventType.MESSAGE_RECEIVED)
    events.append(first)
    events.append(second)

    stored = events.list_for_session("s-1")
    assert [e.id for e in stored] == [first.id, second.id]
    assert dict(stored[0].payload) == {"a": 1}


def test_a_memory_round_trips_with_its_provenance(memories):
    record = a_record()
    memories.write(record)
    assert memories.read(record.id) == record
    assert memories.read("absent") is None


def test_list_active_excludes_other_statuses(memories):
    active = a_record(content="kept")
    rejected = a_record(content="dropped", status=MemoryStatus.REJECTED)
    memories.write(active)
    memories.write(rejected)

    assert [r.content for r in memories.list_active()] == ["kept"]


def test_a_rewritten_memory_keeps_sqlite_replaces_ordering(pg):
    """The parity decision from the module docstring, pinned against the
    engine it names.

    SQLite's `INSERT OR REPLACE` deletes and reinserts, so rewriting a record
    moves it to the end of `ORDER BY seq`. A Postgres `ON CONFLICT ... DO
    UPDATE` keeps the old seq and orders differently -- the exact "two
    substrates disagree" class that hit Phase 5 CI once. Run the identical
    writes through the SQLite backend and this one and compare the order the
    two DURABLE engines produce; the in-memory store has no seq, so the claim
    is only meaningful for engines that persist an order.
    """
    from dataclasses import replace

    from personal_ai_core.persistence.sqlite import (
        SqliteMemoryRepository,
        connect as sqlite_connect,
    )

    sqlite_db = sqlite_connect(":memory:")
    try:
        for store in (SqliteMemoryRepository(sqlite_db), PostgresMemoryRepository(pg)):
            first = a_record(content="prefers tea")
            second = a_record(content="also likes coffee", session="s-2")
            store.write(first)
            store.write(second)

            # Rewrite FIRST (same id, new content): SQLite moves it to the end.
            store.write(replace(first, content="prefers tea, now hotter"))

            assert [r.content for r in store.list_active()] == [
                "also likes coffee",
                "prefers tea, now hotter",
            ], f"{type(store).__name__} ordered a rewritten record differently"
    finally:
        sqlite_db.close()


# --- R2: supersede is the whole atomicity requirement ---------------------


def test_supersede_demotes_the_old_and_links_the_new(memories):
    old = a_record(content="prefers tea")
    memories.write(old)

    new = memories.supersede(old.id, a_record(content="prefers coffee"))

    assert new.supersedes == old.id
    assert memories.read(old.id).status is MemoryStatus.SUPERSEDED
    assert [r.content for r in memories.list_active()] == ["prefers coffee"]


def test_superseding_an_unknown_record_raises(memories):
    with pytest.raises(KeyError):
        memories.supersede("absent", a_record())


def test_superseding_a_non_active_record_raises(memories):
    rejected = a_record(status=MemoryStatus.REJECTED)
    memories.write(rejected)
    with pytest.raises(ValueError, match="rejected"):
        memories.supersede(rejected.id, a_record())


def test_a_failed_supersede_leaves_neither_write_behind(pg):
    """The crash R2 describes, driven on the server store.

    Making the second write fail is the only ordering where a non-transactional
    implementation would leave the first one committed. Both rows land or
    neither -- the same assertion the SQLite file runs, against the engine
    ADR-016 chose for managed durability rather than file transactions.
    """
    store = PostgresMemoryRepository(pg)
    old = a_record(content="prefers tea")
    store.write(old)

    new = a_record(content="prefers coffee")
    original_write = store._write  # noqa: SLF001 -- driving the failure
    calls = {"n": 0}

    def fail_on_second(record):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated mid-transaction failure")
        original_write(record)

    store._write = fail_on_second  # noqa: SLF001
    with pytest.raises(RuntimeError):
        store.supersede(old.id, new)
    store._write = original_write  # noqa: SLF001

    still_there = store.read(old.id)
    assert still_there is not None
    assert still_there.status is MemoryStatus.ACTIVE
    assert store.read(new.id) is None
    assert [r.content for r in store.list_active()] == ["prefers tea"]


# --- durability: the thing the in-memory store cannot do ------------------


def test_everything_survives_closing_and_reopening_the_connection():
    first = connect(URL)
    user = User()
    session = Session(user_id=user.id)
    message = Message(session_id=session.id, role=Role.USER, content="مرحبا",
                      language="ar")
    event = Event(
        session_id=session.id,
        type=EventType.MESSAGE_RECEIVED,
        payload={"role": "user", "language": "ar", "nested": {"k": [1, 2]}},
    )
    record = a_record(session=session.id)

    PostgresUserRepository(first).add(user)
    PostgresSessionRepository(first).add(session)
    PostgresMessageRepository(first).add(message)
    PostgresEventRepository(first).append(event)
    PostgresMemoryRepository(first).write(record)
    first.close()

    reopened = None
    try:
        reopened = connect(URL)
        assert PostgresUserRepository(reopened).get(user.id) == user
        assert PostgresSessionRepository(reopened).get(session.id) == session
        assert (
            PostgresMessageRepository(reopened).list_for_session(session.id)
            == (message,)
        )

        stored_event = PostgresEventRepository(reopened).list_for_session(
            session.id
        )[0]
        assert stored_event == event
        assert dict(stored_event.payload) == dict(event.payload)

        assert PostgresMemoryRepository(reopened).read(record.id) == record
    finally:
        if reopened is not None:
            drop_all(reopened)
            reopened.close()


def test_the_order_is_a_stored_fact_not_an_artefact_of_a_list():
    """ADR-010's R3, against the server: `seq` is a stored column, so the
    order survives connections the way sqlite's survives the process."""
    first = connect(URL)
    repository = PostgresEventRepository(first)
    sent = [
        Event(session_id="s-1", type=EventType.MESSAGE_RECEIVED, payload={"i": i})
        for i in range(20)
    ]
    for event in sent:
        repository.append(event)
    first.close()

    reopened = None
    try:
        reopened = connect(URL)
        read_back = PostgresEventRepository(reopened).list_for_session("s-1")
        assert [e.payload["i"] for e in read_back] == list(range(20))
    finally:
        if reopened is not None:
            drop_all(reopened)
            reopened.close()


# --- the connection lifecycle is a pinned contract -------------------------
#
# The lifecycle decision (module docstring of `persistence/postgres.py`):
# autocommit connections, writes committed by explicit `transaction()`
# blocks, and never `with connection:` -- psycopg 3.3 changed that to close
# the connection. These tests pin the parts a regression could ship silently.


def test_an_explicit_commit_inside_a_write_unit_is_refused(pg):
    """The commit() guardrail.

    Writes commit exactly when their `transaction()` block exits, never by a
    stray `connection.commit()` in the middle of a write unit -- psycopg
    refuses the call with ProgrammingError while a Transaction context is
    open. That refusal is what makes a supersede atomic: nothing can split
    it in half from outside the repository.
    """
    import psycopg

    with pytest.raises(psycopg.ProgrammingError, match="forbidden"):
        with pg.transaction():
            pg.execute("SELECT 1").fetchone()
            pg.commit()


# --- the schema is versioned from the first row ---------------------------


def test_the_schema_version_is_written_on_creation(pg):
    assert pg.execute("SELECT version FROM schema_version").fetchone()[
        "version"
    ] == SCHEMA_VERSION


def test_a_database_from_another_schema_version_is_refused():
    """Refused, not migrated -- same discipline as the SQLite backend: a
    backend that silently adapts to a database it does not recognise is how
    data is quietly lost."""
    connection = connect(URL)
    connection.execute("UPDATE schema_version SET version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionMismatch, match="99"):
        connect(URL)
    # The refused connection must not have corrupted the database it refused
    # -- but a normal connect() refuses it too, and that is the point of the
    # gate. Open a raw driver connection instead, deliberately bypassing the
    # version check, and read what connect() refused to touch.
    from typing import Any

    import psycopg
    from psycopg.rows import dict_row

    # Connection[Any], exactly how `persistence/postgres.py` types the
    # connections it opens (bare connect, row_factory assigned after), and
    # autocommit like those too: on a raw non-autocommit connection the
    # SELECT would leave the connection inside an implicit transaction, so
    # drop_all's transaction() block would be a savepoint that close() then
    # discards -- and the cleanup would silently not happen.
    clean: psycopg.Connection[Any] = psycopg.connect(URL, autocommit=True)
    clean.row_factory = dict_row
    version_row = clean.execute("SELECT version FROM schema_version").fetchone()
    assert version_row is not None
    assert (
        version_row["version"] == 99
    )  # untouched -- version 99 still there, nothing migrated
    drop_all(clean)
    clean.close()


# --- and they satisfy the contracts ---------------------------------------


def test_every_postgres_repository_satisfies_its_protocol(pg):
    assert isinstance(PostgresUserRepository(pg), UserRepository)
    assert isinstance(PostgresSessionRepository(pg), SessionRepository)
    assert isinstance(PostgresMessageRepository(pg), MessageRepository)
    assert isinstance(PostgresEventRepository(pg), EventRepository)
    assert isinstance(PostgresMemoryRepository(pg), MemoryStore)