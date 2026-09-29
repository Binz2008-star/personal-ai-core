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
from dataclasses import replace
from typing import Any

import pytest
from support.postgres_target import (
    NO_SERVER_IDENTITY,
    server_identity,
    server_url,
)
from support.postgres_target import (
    reset_to_empty as _reset_to_empty,
)

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
    _EXPECTED_TABLES,
    SCHEMA_VERSION,
    DatabaseIdentity,
    DatabaseIdentityError,
    DatabaseVerdict,
    PostgresEventRepository,
    PostgresMemoryRepository,
    PostgresMessageRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    SchemaIntent,
    SchemaVersionMismatch,
    UnapprovedDatabaseError,
    approve_test_database,
    connect,
    drop_all,
    inspect_database,
)

# The two conditions that gate this suite, stated separately so the skip
# reason can say which one is missing. `find_spec` rather than `import`
# because an absent driver must skip, not raise at collection.
DRIVER_PRESENT = importlib.util.find_spec("psycopg") is not None
# Resolved through the one module that owns this decision, at COLLECTION time.
# It returns "" when no server is advertised (so the skip gate below reads
# naturally) and RAISES when POSTGRES_TEST_URL names the same database as
# DATABASE_URL -- a misconfiguration that must not be able to render as "no
# server available".
URL = server_url()
# This suite's own statement of which database it is reaching. Derived from the
# resolved test URL on purpose; see `support.postgres_target.server_identity`
# for why that is the test's assertion rather than the library's self-attestation.
#
# `or NO_SERVER_IDENTITY` rather than `None`: this is a module-level constant,
# and the skip marker below means it is never consumed when there is no server.
# If that ever changed, the sentinel would make `connect` refuse loudly instead
# of the confirmation quietly going missing.
IDENTITY: DatabaseIdentity = server_identity() or NO_SERVER_IDENTITY

pytestmark = pytest.mark.skipif(
    not (DRIVER_PRESENT and URL),
    reason="no PostgreSQL server reached; run with POSTGRES_TEST_URL set",
)


def open_database(intent: SchemaIntent) -> Any:
    """`connect` with this suite's declared intent and identity."""
    return connect(URL, intent=intent, identity=IDENTITY)


def clean_up(connection: Any) -> None:
    """Drop this project's tables, and only with a live approval.

    The approval is minted here rather than cached, because `drop_all`
    re-verifies it against the connection it is handed: an approval is a
    capability, and a capability that does not expire when the database changes
    underneath it is not one.
    """
    drop_all(connection, approval=approve_test_database(connection))


def raw_connection() -> Any:
    """A driver connection to the test database that does NOT go through the
    gate, for test SETUP that must create something the gate would refuse.

    The tests below plant a foreign `users` table, a foreign `jobs` table and
    a view named `users` -- and they can only do that against an empty
    database, because a database that already holds this project's schema has
    those six names taken. `open_database(INITIALIZE)` cannot be used, and
    neither can `connect` in any other mode: the whole point is a state
    `connect` will refuse to reach.

    So setup and teardown speak to the server directly, while every ASSERTION
    about the gate goes through `connect`/`drop_all`/`approve_test_database`.
    The gate is never the thing under test here; it is the thing the test
    attacks, and it is attacked through its real entry points.
    """
    import psycopg
    from psycopg.rows import dict_row

    connection: Any = psycopg.connect(URL, autocommit=True)
    connection.row_factory = dict_row
    return connection


def reset_to_empty() -> None:
    """The shared bootstrap, bound to THIS module's resolved URL.

    The implementation lives in `support.postgres_target` because two modules
    need it and a bootstrap that bypasses the safety gate must exist in exactly
    one place -- see that function's docstring for why it is allowed to, and for
    why it takes the URL as an argument instead of re-reading the environment:
    tests here monkeypatch `POSTGRES_TEST_URL`, and a bootstrap that trusted the
    environment would clean the wrong database during their teardown.
    """
    _reset_to_empty(URL)


@pytest.fixture(scope="module", autouse=True)
def _known_starting_point():
    """Establish a VACUOUS database ONCE, before any test in this module runs.

    Scoped to the module rather than to each test, and that is the whole
    design. INITIALIZE is now only legal against an empty database, so a
    leftover from a previous run would make the first test fail for a reason
    that has nothing to do with it. But resetting per test would throw away the
    property worth having: if a test leaks a relation, the NEXT test's
    INITIALIZE is refused as OCCUPIED, and the leak is reported rather than
    silently absorbed. One reset at the start establishes the starting point;
    after that the suite holds itself to the gate.
    """
    reset_to_empty()


@pytest.fixture
def pg():
    connection = open_database(SchemaIntent.INITIALIZE)
    yield connection
    clean_up(connection)
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
    )
    from personal_ai_core.persistence.sqlite import (
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
    first = open_database(SchemaIntent.INITIALIZE)
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
        # OPERATE, not INITIALIZE: the schema exists now, and re-declaring the
        # intent to create it is how a second builder ends up silently
        # re-applying a schema to a database it should only be using.
        reopened = open_database(SchemaIntent.OPERATE)
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
            clean_up(reopened)
            reopened.close()


def test_the_order_is_a_stored_fact_not_an_artefact_of_a_list():
    """ADR-010's R3, against the server: `seq` is a stored column, so the
    order survives connections the way sqlite's survives the process."""
    first = open_database(SchemaIntent.INITIALIZE)
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
        reopened = open_database(SchemaIntent.OPERATE)
        read_back = PostgresEventRepository(reopened).list_for_session("s-1")
        assert [e.payload["i"] for e in read_back] == list(range(20))
    finally:
        if reopened is not None:
            clean_up(reopened)
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
    data is quietly lost.

    Declared as OPERATE, and that is now the only way to reach the version
    check. The database still has this project's shape, so it reads as PAC --
    but INITIALIZE refuses a non-empty database before it ever looks at a row,
    and asking to CREATE a schema is not the question this test is asking.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    connection.execute("UPDATE schema_version SET version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionMismatch, match="99"):
        open_database(SchemaIntent.OPERATE)
    # The refused connection must not have corrupted the database it refused
    # -- but a normal connect() refuses it too, and that is the point of the
    # gate. Open a raw driver connection instead, deliberately bypassing the
    # version check, and read what connect() refused to touch.
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
    # Still needs a live approval: the capability is about the database, not
    # about how the connection was opened.
    clean_up(clean)
    clean.close()


# --- and they satisfy the contracts ---------------------------------------


def test_every_postgres_repository_satisfies_its_protocol(pg):
    assert isinstance(PostgresUserRepository(pg), UserRepository)
    assert isinstance(PostgresSessionRepository(pg), SessionRepository)
    assert isinstance(PostgresMessageRepository(pg), MessageRepository)
    assert isinstance(PostgresEventRepository(pg), EventRepository)
    assert isinstance(PostgresMemoryRepository(pg), MemoryStore)


# ============================================================================
# THE DESTRUCTIVE-ENTRY GATE, AGAINST A REAL SERVER
# ============================================================================
#
# The classification is proven driver-free in `test_postgres_safety.py`. What
# cannot be proven without a server is the part that matters most: that
# `connect` and `drop_all` actually consult it, and that a refusal leaves the
# database exactly as it was found. Every test below asserts the SECOND half
# too -- the surviving table is the evidence, not the raised exception.


def relation_inventory(connection) -> dict[str, list[str]]:
    """relkind -> relation names, for the schema a connection is pointed at."""
    found: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT c.relkind, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind IN "
        "  ('r','p','v','m','S','f','c','i','I') ORDER BY c.relname"
    ).fetchall():
        found.setdefault(row["relkind"], []).append(row["relname"])
    return found


def planted_survives(connection, name: str) -> bool:
    return connection.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()[
        "t"
    ] is not None


def test_a_freshly_initialized_database_reads_as_pac_and_nothing_else():
    """The happy path, and the claim PAC rests on: EXACTLY the expected set.

    Not "the six tables are there". If `_SCHEMA` ever started leaving another
    relation behind -- a fourth sequence, a helper table, a trigger table --
    every OPERATE in production would start refusing a database this backend
    created, and the drift test below is what says so before that happens.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        inspection = inspect_database(connection)
        assert inspection.verdict is DatabaseVerdict.PAC
        assert set(inspection.snapshot.relations) == {
            "events", "memories", "messages", "schema_version", "sessions", "users",
            "events_seq_seq", "memories_seq_seq", "messages_seq_seq",
        }
        # And the shape the classifier compares against is the shape `_SCHEMA`
        # actually produced, not a hand-written guess about it.
        for name, expected in inspection.snapshot.columns.items():
            if name in _EXPECTED_TABLES:
                assert expected == _EXPECTED_TABLES[name], f"{name} drifted"
    finally:
        clean_up(connection)
        connection.close()


def test_dropping_everything_returns_the_database_to_vacuous():
    """And leaves nothing behind -- including the indexes, and the sequences."""
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        clean_up(connection)
        inventory = relation_inventory(connection)
        assert inventory == {}, f"drop_all left {inventory}"
        assert inspect_database(connection).verdict is DatabaseVerdict.VACUOUS
    finally:
        connection.close()


def test_an_unrelated_users_table_is_never_taken_for_this_projects_schema():
    """The regression this whole gate exists for.

    An unrelated application can own a table called `users` with real rows
    behind it. `_DROP_ALL` is the exact inverse of `_SCHEMA`, so without the
    gate, pointing this backend at such a database and cleaning up after a test
    run drops that table, CASCADE, with everything it owns. Both intents must
    refuse, and the table must still be there afterwards.
    """
    reset_to_empty()
    plant = raw_connection()
    try:
        plant.execute(
            "CREATE TABLE users (id integer, email text, password_hash text, "
            "role text, is_active boolean)"
        )
        plant.execute(
            "INSERT INTO users (id, email) VALUES (1, 'someone@example.com')"
        )

        assert inspect_database(plant).verdict is DatabaseVerdict.FOREIGN

        for intent in (SchemaIntent.INITIALIZE, SchemaIntent.OPERATE):
            with pytest.raises(DatabaseIdentityError, match="foreign"):
                open_database(intent)

        # The evidence. An exception is a claim; the surviving row is the proof.
        assert planted_survives(plant, "users")
        assert plant.execute(
            "SELECT email FROM users WHERE id = 1"
        ).fetchone()["email"] == "someone@example.com"
        # And this project's schema was never created alongside it.
        assert not planted_survives(plant, "events")
    finally:
        plant.execute("DROP TABLE users")
        plant.close()
    reset_to_empty()


def test_a_database_of_unrelated_tables_is_occupied_and_refused():
    """No name collision, and still not an empty database.

    `jobs` and `leads` match none of this project's six names, so a
    collision-only check would call this VACUOUS and INITIALIZE would create
    six tables inside somebody else's application. The surviving tables are the
    evidence.
    """
    reset_to_empty()
    plant = raw_connection()
    try:
        plant.execute("CREATE TABLE jobs (id integer, payload text)")
        plant.execute("CREATE TABLE leads (id integer, email text)")

        assert inspect_database(plant).verdict is DatabaseVerdict.OCCUPIED

        with pytest.raises(DatabaseIdentityError, match="occupied"):
            open_database(SchemaIntent.INITIALIZE)
        with pytest.raises(DatabaseIdentityError, match="occupied"):
            open_database(SchemaIntent.OPERATE)

        assert planted_survives(plant, "jobs")
        assert planted_survives(plant, "leads")
        assert not planted_survives(plant, "users")
    finally:
        plant.execute("DROP TABLE jobs")
        plant.execute("DROP TABLE leads")
        plant.close()
    reset_to_empty()


def test_a_view_named_users_is_foreign_not_a_table():
    """A view is not a table, and the gate reads the kind rather than guessing.

    A view called `users` whose columns happened to match this project's
    `users` would pass a column-only comparison, and the backend would then
    insert into it. `pg_class.relkind` is what distinguishes them.
    """
    reset_to_empty()
    plant = raw_connection()
    try:
        plant.execute("CREATE VIEW users AS SELECT 'a'::text AS id, 'b'::text AS created_at")

        inspection = inspect_database(plant)
        assert inspection.verdict is DatabaseVerdict.FOREIGN
        assert "v" in inspection.reason

        for intent in (SchemaIntent.INITIALIZE, SchemaIntent.OPERATE):
            with pytest.raises(DatabaseIdentityError):
                open_database(intent)
        assert planted_survives(plant, "users")
    finally:
        plant.execute("DROP VIEW users")
        plant.close()
    reset_to_empty()


def test_this_projects_tables_coexisting_with_a_foreign_table_are_refused():
    """All six correct, plus somebody else's: not our database.

    This is a database this project was installed INTO, which is a worse
    situation than one it merely resembles, so the verdict is OCCUPIED and
    both intents refuse.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        connection.execute("CREATE TABLE somebody_elses (id integer)")
        inspection = inspect_database(connection)
        assert inspection.verdict is DatabaseVerdict.OCCUPIED
        with pytest.raises(DatabaseIdentityError, match="occupied"):
            open_database(SchemaIntent.OPERATE)
        with pytest.raises(DatabaseIdentityError, match="occupied"):
            open_database(SchemaIntent.INITIALIZE)
    finally:
        connection.execute("DROP TABLE somebody_elses")
        clean_up(connection)
        connection.close()


def test_five_of_the_six_tables_is_partial_and_both_intents_refuse_it():
    reset_to_empty()
    plant = open_database(SchemaIntent.INITIALIZE)
    try:
        plant.execute("DROP TABLE memories CASCADE")
        assert inspect_database(plant).verdict is DatabaseVerdict.PARTIAL
        for intent in (SchemaIntent.INITIALIZE, SchemaIntent.OPERATE):
            with pytest.raises(DatabaseIdentityError, match="partial"):
                open_database(intent)
    finally:
        plant.close()
    reset_to_empty()


def test_operate_applies_no_ddl_even_for_a_table_it_could_recreate():
    """"OPERATE performs no DDL", made observable.

    Relation counts would not show this: `CREATE TABLE IF NOT EXISTS` against
    tables that already exist is a no-op that still executes. So drop a table
    this backend would happily have re-created, and require that OPERATE
    REFUSES and leaves it absent. If `_SCHEMA` ran under OPERATE, this test
    would find the table back and the connection would have succeeded.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        connection.execute("DROP TABLE events CASCADE")
        with pytest.raises(DatabaseIdentityError, match="partial"):
            open_database(SchemaIntent.OPERATE)
        assert not planted_survives(connection, "events")
    finally:
        connection.close()
        # `clean_up` CANNOT be used here, and that is the gate working: a
        # PARTIAL database is not in `_APPROVAL_ALLOWS`, so `drop_all` refuses
        # it. A damaged schema is exactly the state the bootstrap exists to
        # undo, so the bootstrap is what undoes it.
        reset_to_empty()


def test_operate_on_an_intact_database_changes_nothing():
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        before = relation_inventory(connection)
        other = open_database(SchemaIntent.OPERATE)
        try:
            assert relation_inventory(other) == before
        finally:
            other.close()
    finally:
        clean_up(connection)
        connection.close()


def test_connect_refuses_a_database_the_caller_did_not_confirm():
    """The identity check, against a real connection.

    Everything else in this file confirms the database the way the resolver
    derived it, which is the right thing for a suite to do and the wrong thing
    to rely on -- a check that always agrees proves nothing. So here the
    caller asserts something different from the truth and the connection must
    be refused, having written nothing.
    """
    try:
        reset_to_empty()
        for field, wrong in (
            ("host", DatabaseIdentity("not-the-host.example", 5432, "pac_test")),
            ("port", DatabaseIdentity(host="localhost", port=1, database="pac_test")),
            ("database", DatabaseIdentity("localhost", 5432, "not_the_database")),
        ):
            identity = replace(IDENTITY, **{field: wrong})
            with pytest.raises(DatabaseIdentityError, match="confirmed"):
                connect(URL, intent=SchemaIntent.INITIALIZE, identity=identity)
        # The proof that the refusals wrote nothing: read the database over a
        # connection that did not go through `connect` at all, and find it
        # still exactly as empty as the three refusals found it. Asserting
        # AFTER a successful INITIALIZE would prove nothing -- that call creates
        # the six tables, so PAC is what it must read.
        probe = raw_connection()
        try:
            assert inspect_database(probe).verdict is DatabaseVerdict.VACUOUS
        finally:
            probe.close()
    finally:
        reset_to_empty()


def test_initializing_refuses_an_already_initialized_database():
    """Initialization is a separate, deliberate act, not a side effect of
    opening a connection. A second builder must say OPERATE."""
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        with pytest.raises(DatabaseIdentityError, match="empty database"):
            open_database(SchemaIntent.INITIALIZE)
    finally:
        clean_up(connection)
        connection.close()


# --- the search-path pin ----------------------------------------------------


def point_the_database_at(schema_sql: str) -> Any:
    """Make every NEW connection to this database resolve `search_path` to the
    given schema, and return a connection to set it up with.

    `ALTER DATABASE ... SET`, not `SET search_path` on a live connection, and
    that distinction is the whole difficulty of testing the pin. `connect()`
    opens its own connection, so a session setting made here would never reach
    it -- the pin happens inside a transaction on a connection this test does
    not have. A per-database default IS inherited by that connection, which is
    the only way to hand `connect` a session whose `search_path` disagrees with
    the default.

    `schema_sql` is raw SQL, not a string, so a deliberately hostile name is
    quoted by the server's own `quote_ident` rather than by this test.
    """
    raw = raw_connection()
    try:
        raw.execute(f"ALTER DATABASE pac_test SET search_path TO {schema_sql}")
    except BaseException:
        raw.close()
        raise
    return raw


def restore_default_search_path(raw: Any) -> None:
    """Undo `point_the_database_at`. Every test that uses it must call this."""
    try:
        raw.execute("ALTER DATABASE pac_test RESET search_path")
    finally:
        raw.close()
    reset_to_empty()


def test_inspection_and_ddl_land_in_the_same_schema():
    """The pin, and the property it exists to guarantee.

    `_SCHEMA` writes unqualified names, so those names land wherever
    `search_path` resolves. Point the database at some other schema entirely
    and the gate must inspect THAT schema and write to THAT schema -- not
    inspect one and write to the other, which is the failure the pin exists to
    rule out and which no amount of correct reasoning elsewhere would catch.
    """
    reset_to_empty()
    raw = point_the_database_at("pac_side")
    try:
        raw.execute("CREATE SCHEMA IF NOT EXISTS pac_side")

        connection = open_database(SchemaIntent.INITIALIZE)
        try:
            inspection = inspect_database(connection)
            assert inspection.schema == "pac_side"
            assert inspection.verdict is DatabaseVerdict.PAC
            landed = connection.execute(
                "SELECT count(*) AS n FROM pg_class x "
                "JOIN pg_namespace n ON n.oid = x.relnamespace "
                "WHERE n.nspname = 'pac_side' AND x.relkind = 'r'"
            ).fetchone()["n"]
            assert landed == 6, "DDL landed somewhere other than the pinned schema"
            # ...and NOT in the schema it would have used without the pin.
            # This is the half that matters: a gate that inspected `pac_side`
            # while `_SCHEMA` wrote to `public` would pass the line above and
            # still be wrong.
            public_tables = connection.execute(
                "SELECT count(*) AS n FROM pg_class x "
                "JOIN pg_namespace n ON n.oid = x.relnamespace "
                "WHERE n.nspname = 'public' AND x.relkind = 'r'"
            ).fetchone()["n"]
            assert public_tables == 0
        finally:
            connection.close()
    finally:
        raw.execute("DROP SCHEMA IF EXISTS pac_side CASCADE")
        restore_default_search_path(raw)


def test_the_pin_survives_a_schema_name_that_breaks_naive_quoting():
    """`quote_ident` is applied server-side, so this cannot inject.

    A `search_path` built by string-formatting an identifier is an injection
    point; one built with PostgreSQL's own quoting function is not -- including
    for a name containing a double quote, a comma and a space, all three of
    which are structural in a `search_path` value. A comma is the sharpest of
    the three: unquoted, it silently turns one schema name into two.
    """
    reset_to_empty()
    setup = raw_connection()
    hostile = 'we"ird, name'
    quoted = setup.execute(
        "SELECT quote_ident(%s) AS q", (hostile,)
    ).fetchone()["q"]
    setup.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted}")
    setup.close()

    raw = point_the_database_at(quoted)
    try:
        connection = open_database(SchemaIntent.INITIALIZE)
        try:
            assert inspect_database(connection).schema == hostile
            landed = connection.execute(
                "SELECT count(*) AS n FROM pg_class x "
                "JOIN pg_namespace n ON n.oid = x.relnamespace "
                "WHERE n.nspname = %s AND x.relkind = 'r'", (hostile,)
            ).fetchone()["n"]
            assert landed == 6
            # If the name had been interpolated into the `search_path` value
            # rather than quoted, the comma would have split it and the
            # schema it resolved to would be the prefix before the quote --
            # a different, non-existent schema, and `current_schema()` would
            # not be the hostile name at all.
            assert connection.execute("SELECT current_schema() AS n").fetchone()[
                "n"
            ] == hostile
        finally:
            connection.close()
    finally:
        drop = raw_connection()
        try:
            drop.execute(f"DROP SCHEMA IF EXISTS {quoted} CASCADE")
        finally:
            drop.close()
        restore_default_search_path(raw)


# --- the drop capability -----------------------------------------------------


def test_drop_all_refuses_without_an_approval():
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        with pytest.raises(TypeError):
            # Deliberately invalid: `approval` is a required keyword argument,
            # and the refusal has to come from the call signature, before the
            # body runs. The ignore is scoped to this one line for that reason.
            drop_all(connection)  # type: ignore[call-arg]
        assert planted_survives(connection, "users")
    finally:
        clean_up(connection)
        connection.close()


def test_drop_all_refuses_an_approval_for_another_database():
    """A token is not authority: the approval is re-checked against the live
    connection, on every field it binds.

    One wrong field is enough. An approval that only bound the host would pass
    here; an approval that binds host, port, database AND schema is what
    "cannot succeed against an unapproved database" actually requires.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        genuine = approve_test_database(connection)
        for field, value in (
            ("host", "somewhere-else.example"),
            ("port", genuine.identity.port + 1),
            ("database", "some_other_database"),
        ):
            forged = replace(
                genuine, identity=replace(genuine.identity, **{field: value})
            )
            with pytest.raises(UnapprovedDatabaseError, match="approved for"):
                drop_all(connection, approval=forged)
            # The schema field binds too.
        with pytest.raises(UnapprovedDatabaseError, match="approved for"):
            drop_all(connection, approval=replace(genuine, schema="somewhere_else"))
        # After all five refusals, everything is still standing.
        for table in ("users", "sessions", "messages", "events", "memories"):
            assert planted_survives(connection, table)
    finally:
        clean_up(connection)
        connection.close()


def test_an_approval_is_refused_when_the_database_is_no_longer_safe():
    """The verdict is re-checked at drop time, not only when the approval was
    minted. Something foreign appeared since; the drop is refused."""
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        approval = approve_test_database(connection)
        connection.execute("CREATE TABLE interloper (id integer)")
        with pytest.raises(UnapprovedDatabaseError, match="foreign objects"):
            drop_all(connection, approval=approval)
        assert planted_survives(connection, "users")
        assert planted_survives(connection, "interloper")
    finally:
        connection.execute("DROP TABLE interloper")
        clean_up(connection)
        connection.close()


def test_approval_is_refused_for_a_database_that_is_not_the_declared_test_one(
    monkeypatch,
):
    """POSTGRES_TEST_URL names the target; a connection to anything else is
    not approved, however innocent that anything else looks."""
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        monkeypatch.setenv(
            "POSTGRES_TEST_URL", URL.replace("/pac_test", "/somewhere_else")
        )
        with pytest.raises(UnapprovedDatabaseError, match="POSTGRES_TEST_URL names"):
            approve_test_database(connection)
        assert planted_survives(connection, "users")
    finally:
        # `reset_to_empty`, not `clean_up`: this test's own subject is a
        # `POSTGRES_TEST_URL` that cannot approve anything, and `monkeypatch`
        # has not yet undone it inside the `finally`. Asking the approval
        # mechanism to clean up after a test about the approval mechanism
        # refusing would just re-run the refusal.
        connection.close()
        reset_to_empty()


def test_approval_is_refused_when_the_url_is_the_same_database_spelled_differently(
    monkeypatch,
):
    """Normalised, not string-compared.

    Adding a query parameter does not change which database a URL reaches. A
    string comparison would wave this through; a normalised identity does not.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        separator = "&" if "?" in URL else "?"
        monkeypatch.setenv(
            "POSTGRES_TEST_URL", f"{URL}{separator}application_name=something_else"
        )
        # Same database, so the identity matches and approval is still granted
        # -- which is the point: a differing spelling is not a different target.
        assert approve_test_database(connection).identity == inspect_database(
            connection
        ).identity

        monkeypatch.setenv(
            "POSTGRES_TEST_URL",
            URL.replace(f"@{IDENTITY.host}", "@127.0.0.1"),
        )
        with pytest.raises(UnapprovedDatabaseError, match="POSTGRES_TEST_URL names"):
            approve_test_database(connection)
    finally:
        connection.close()
        reset_to_empty()


def test_approval_is_refused_when_no_test_url_is_advertised(monkeypatch):
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        monkeypatch.delenv("POSTGRES_TEST_URL", raising=False)
        with pytest.raises(UnapprovedDatabaseError, match="not set"):
            approve_test_database(connection)
        assert planted_survives(connection, "users")
    finally:
        connection.close()
        reset_to_empty()


def test_a_configured_denylist_refuses_an_otherwise_legitimate_database(
    monkeypatch,
):
    """The OPTIONAL hardening, exercised.

    Not the guard -- a database with a foreign `users` is refused without
    consulting configuration at all, which the other tests here show. This is
    the one case the shape check cannot cover: a database that IS entirely
    pac-shaped and still must not be dropped.

    A whole URL, because that is what the variable holds: `database_identity_of`
    takes a URL and refuses to guess at a bare hostname, so a denylist entry
    that is not a URL is a loud error rather than a silently ignored line.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        monkeypatch.setenv("PAC_PROTECTED_DATABASE_URLS", URL)
        with pytest.raises(UnapprovedDatabaseError, match="PAC_PROTECTED_DATABASE_URLS"):
            approve_test_database(connection)

        # And a drop refuses too, even with an approval minted before the
        # denylist was set -- the check runs at drop time, not only at minting.
        monkeypatch.delenv("PAC_PROTECTED_DATABASE_URLS")
        genuine = approve_test_database(connection)
        monkeypatch.setenv("PAC_PROTECTED_DATABASE_URLS", URL)
        with pytest.raises(UnapprovedDatabaseError, match="PAC_PROTECTED_DATABASE_URLS"):
            drop_all(connection, approval=genuine)
        assert planted_survives(connection, "users")
    finally:
        monkeypatch.delenv("PAC_PROTECTED_DATABASE_URLS", raising=False)
        connection.close()
        reset_to_empty()


def test_a_denylist_entry_that_is_not_a_url_is_an_error_not_a_silent_no_op(
    monkeypatch,
):
    """A denylist that quietly ignores what it cannot parse is not a denylist.

    `database_identity_of` refuses a URL that names no host, and that refusal
    propagates out of `_protected_identities` rather than being caught and
    dropped. A typo in the denylist therefore fails loudly at the moment
    someone tries to drop a database -- which is the one moment it matters.
    """
    connection = open_database(SchemaIntent.INITIALIZE)
    try:
        monkeypatch.setenv("PAC_PROTECTED_DATABASE_URLS", "not-a-url")
        with pytest.raises(ValueError, match="no host"):
            approve_test_database(connection)
    finally:
        monkeypatch.delenv("PAC_PROTECTED_DATABASE_URLS", raising=False)
        connection.close()
        reset_to_empty()