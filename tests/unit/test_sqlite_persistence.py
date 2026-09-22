"""The SQLite backend stores what the in-memory one holds — ADR-010 D+B.

Two kinds of claim are checked, and only the second is about SQLite:

  SAME BEHAVIOUR   the two implementations of each contract agree, so
                   swapping one for the other cannot change what the layers
                   above see. Written as one parametrised suite over both,
                   because a conformance test that runs against one
                   implementation is a test of that implementation.

  DURABILITY       what the in-memory one cannot do: survive the process.
                   Asserted by closing the connection and opening the file
                   again, which is the only way to check it.

The atomicity case (R2) has its own section. `supersede` is the whole of this
system's multi-row write, and the reason ADR-010 chose an engine with
transactions over an append-only log.
"""
from __future__ import annotations

import sqlite3

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
from personal_ai_core.persistence.sqlite import (
    SCHEMA_VERSION,
    SchemaVersionMismatch,
    SqliteEventRepository,
    SqliteMemoryRepository,
    SqliteMessageRepository,
    SqliteSessionRepository,
    SqliteUserRepository,
    connect,
)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "core.db")
    yield connection
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


@pytest.fixture(params=["in_memory", "sqlite"])
def users(request, db):
    return InMemoryUserRepository() if request.param == "in_memory" else (
        SqliteUserRepository(db)
    )


@pytest.fixture(params=["in_memory", "sqlite"])
def sessions(request, db):
    return InMemorySessionRepository() if request.param == "in_memory" else (
        SqliteSessionRepository(db)
    )


@pytest.fixture(params=["in_memory", "sqlite"])
def messages(request, db):
    return InMemoryMessageRepository() if request.param == "in_memory" else (
        SqliteMessageRepository(db)
    )


@pytest.fixture(params=["in_memory", "sqlite"])
def events(request, db):
    return InMemoryEventRepository() if request.param == "in_memory" else (
        SqliteEventRepository(db)
    )


@pytest.fixture(params=["in_memory", "sqlite"])
def memories(request, db):
    return InMemoryMemoryRepository() if request.param == "in_memory" else (
        SqliteMemoryRepository(db)
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


def test_a_failed_supersede_leaves_neither_write_behind(db):
    """The crash R2 describes: a partial supersede leaves two ACTIVE records
    making contradictory claims, or a link to a record never demoted.

    Driven by making the SECOND write fail, which is the only ordering where
    a non-transactional implementation would leave the first one committed.
    """
    store = SqliteMemoryRepository(db)
    old = a_record(content="prefers tea")
    store.write(old)

    new = a_record(content="prefers coffee")
    original_write = store._write  # noqa: SLF001 -- driving the failure
    calls = {"n": 0}

    def fail_on_second(record):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        original_write(record)

    store._write = fail_on_second  # noqa: SLF001
    with pytest.raises(sqlite3.OperationalError):
        store.supersede(old.id, new)
    store._write = original_write  # noqa: SLF001

    # Neither write survived: the old record is still ACTIVE and the new one
    # is absent. A non-transactional store would have demoted the old one.
    still_there = store.read(old.id)
    assert still_there is not None
    assert still_there.status is MemoryStatus.ACTIVE
    assert store.read(new.id) is None
    assert [r.content for r in store.list_active()] == ["prefers tea"]


# --- durability: the thing the in-memory store cannot do ------------------


def test_everything_survives_closing_and_reopening_the_file(tmp_path):
    path = tmp_path / "core.db"
    db = connect(path)
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

    SqliteUserRepository(db).add(user)
    SqliteSessionRepository(db).add(session)
    SqliteMessageRepository(db).add(message)
    SqliteEventRepository(db).append(event)
    SqliteMemoryRepository(db).write(record)
    db.close()

    reopened = connect(path)
    assert SqliteUserRepository(reopened).get(user.id) == user
    assert SqliteSessionRepository(reopened).get(session.id) == session
    assert SqliteMessageRepository(reopened).list_for_session(session.id) == (message,)

    stored_event = SqliteEventRepository(reopened).list_for_session(session.id)[0]
    assert stored_event == event
    assert dict(stored_event.payload) == dict(event.payload)

    assert SqliteMemoryRepository(reopened).read(record.id) == record
    reopened.close()


def test_the_order_is_a_stored_fact_not_an_artefact_of_a_list(tmp_path):
    """ADR-010's R3. uuid4 does not sort and utcnow collides heavily, so the
    in-memory order is a property of a Python list. `seq` makes it a column
    that survives the process."""
    path = tmp_path / "core.db"
    db = connect(path)
    repository = SqliteEventRepository(db)
    sent = [
        Event(session_id="s-1", type=EventType.MESSAGE_RECEIVED, payload={"i": i})
        for i in range(20)
    ]
    for event in sent:
        repository.append(event)
    db.close()

    reopened = connect(path)
    read_back = SqliteEventRepository(reopened).list_for_session("s-1")
    assert [e.payload["i"] for e in read_back] == list(range(20))
    reopened.close()


# --- the schema is versioned from the first row ---------------------------


def test_the_schema_version_is_written_on_creation(tmp_path):
    db = connect(tmp_path / "core.db")
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == (
        SCHEMA_VERSION
    )
    db.close()


def test_a_file_from_another_schema_version_is_refused(tmp_path):
    """Refused, not migrated. A backend that silently adapts to a file it does
    not recognise is how data is quietly lost."""
    path = tmp_path / "core.db"
    db = connect(path)
    db.execute("UPDATE schema_version SET version = 99")
    db.commit()
    db.close()

    with pytest.raises(SchemaVersionMismatch, match="99"):
        connect(path)


# --- and they satisfy the contracts ---------------------------------------


def test_every_sqlite_repository_satisfies_its_protocol(db):
    assert isinstance(SqliteUserRepository(db), UserRepository)
    assert isinstance(SqliteSessionRepository(db), SessionRepository)
    assert isinstance(SqliteMessageRepository(db), MessageRepository)
    assert isinstance(SqliteEventRepository(db), EventRepository)
    assert isinstance(SqliteMemoryRepository(db), MemoryStore)
