"""SqliteFeedbackRepository (ADR-017 review point 1) on the ADR-010 D+B store.

Feedback's durable form is a FEEDBACK_RECORDED event row in the shared events
table -- no new table, no schema change. Idempotency is enforced by the
DATABASE: the partial unique index `events_feedback_idem_unique` over the
reserved payload key makes one key per feedback row a storage invariant, so
`append` inserts unconditionally and lets the index arbitrate. Two writers on
one connection, two connections, or two threads all reach the same outcome,
because the guarantee is the index rather than a read-then-write predicate.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from personal_ai_core.core.domain import Event, EventType
from personal_ai_core.core.feedback import (
    FeedbackOutcome,
    FeedbackRecord,
    feedback_idempotency_key,
)
from personal_ai_core.learning.feedback import FeedbackRecorder
from personal_ai_core.persistence.sqlite import (
    SCHEMA_VERSION,
    SqliteEventRepository,
    SqliteFeedbackRepository,
    connect,
)


def _db(request) -> sqlite3.Connection:
    db = connect(":memory:")
    request.addfinalizer(db.close)
    return db


def _seed(db: sqlite3.Connection, session_id: str = "s1") -> Event:
    event = Event(
        session_id=session_id,
        type=EventType.GENERATION_COMPLETED,
        payload={"text": "reply"},
    )
    SqliteEventRepository(db).append(event)
    return event


def _record(source_event_id: str, session_id: str = "s1") -> FeedbackRecord:
    return FeedbackRecord(
        idempotency_key=feedback_idempotency_key(
            session_id=session_id,
            source_event_id=source_event_id,
            outcome=FeedbackOutcome.BAD,
            actor="user",
        ),
        source_event_id=source_event_id,
        session_id=session_id,
        actor="user",
        outcome=FeedbackOutcome.BAD,
    )


def test_append_stores_one_row_and_reads_it_back(request):
    db = _db(request)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db)

    recorded = repo.append(_record(source.id))
    assert recorded.outcome is FeedbackOutcome.BAD
    assert [r.id for r in repo.list_for_session("s1")] == [recorded.id]
    assert [r.id for r in repo.list_for_source(source.id)] == [recorded.id]
    # Exactly one feedback event row landed in the shared events table.
    row = db.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type = ?",
        ("feedback.recorded",),
    ).fetchone()
    assert row["n"] == 1


def test_a_duplicate_key_appends_nothing_and_returns_the_stored_record(request):
    db = _db(request)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db)

    first = repo.append(_record(source.id))
    second = repo.append(_record(source.id))
    assert second.id == first.id

    row = db.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type = ?",
        ("feedback.recorded",),
    ).fetchone()
    assert row["n"] == 1


def test_idempotency_holds_across_two_repository_handles_on_one_connection(request):
    db = _db(request)
    source = _seed(db)
    repo_a = SqliteFeedbackRepository(db)
    repo_b = SqliteFeedbackRepository(db)

    first = repo_a.append(_record(source.id))
    # The unique index is the authority, whatever any pre-check believed:
    # a second writer presenting the same key inserts nothing.
    second = repo_b.append(_record(source.id))
    assert second.id == first.id
    assert len(repo_a.list_for_session("s1")) == 1


def test_source_event_must_exist_in_the_same_session(request):
    db = _db(request)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db, session_id="s1")

    with pytest.raises(ValueError, match="does not exist in session"):
        repo.append(_record("no-such-event", "s1"))
    with pytest.raises(ValueError, match="does not exist in session"):
        repo.append(_record(source.id, "other-session"))
    assert repo.list_for_session("s1") == ()


def test_list_for_source_orders_by_seq_not_by_occurred_at(request):
    db = _db(request)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db)
    events = SqliteEventRepository(db)

    # Two feedback records for the same source: total order is append order.
    other = Event(
        session_id="s1", type=EventType.GENERATION_COMPLETED, payload={"text": "x"}
    )
    events.append(other)
    repo.append(
        FeedbackRecord(
            idempotency_key=feedback_idempotency_key(
                session_id="s1",
                source_event_id=source.id,
                outcome=FeedbackOutcome.GOOD,
                actor="user",
            ),
            source_event_id=source.id,
            session_id="s1",
            actor="user",
            outcome=FeedbackOutcome.GOOD,
        )
    )
    repo.append(_record(source.id))

    outcomes = [r.outcome for r in repo.list_for_source(source.id)]
    assert outcomes == [FeedbackOutcome.GOOD, FeedbackOutcome.BAD]


def test_feedback_survives_reopen_like_any_other_durable_event(tmp_path):
    path = tmp_path / "feedback.db"
    db = connect(path)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db)
    repo.append(_record(source.id))
    db.close()

    reopened = connect(path)
    try:
        rebuilt = SqliteFeedbackRepository(reopened).list_for_session("s1")
        assert len(rebuilt) == 1
        assert rebuilt[0].source_event_id == source.id
        assert rebuilt[0].outcome is FeedbackOutcome.BAD
    finally:
        reopened.close()


def test_schema_version_is_unchanged_and_feedback_needs_no_new_schema(request):
    """ADR-017 §12 test 11: Phase 7 adds no schema -- SCHEMA_VERSION stays 1.

    The feedback repository writes only FEEDBACK_RECORDED rows into the
    existing events table; `connect` on a brand-new file applies the same
    schema Phase 3 shipped, and feedback works on it. The one additive object
    is an INDEX, not a table, and it is applied by the same `_SCHEMA`
    initialisation as everything else.
    """
    assert SCHEMA_VERSION == 1
    db = _db(request)
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    # No feedback table: the durable form is an event row.
    assert "feedback" not in tables
    assert "events" in tables


def _indexes(db: sqlite3.Connection) -> dict[str, str]:
    return {
        row["name"]: row["sql"] or ""
        for row in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def test_the_uniqueness_index_exists_after_schema_initialisation(request):
    """The guarantee is a stored object, not an application convention."""
    db = _db(request)
    sql = _indexes(db)["events_feedback_idem_unique"]
    assert "CREATE UNIQUE INDEX" in sql
    # Partial: scoped to feedback rows only, so ordinary events are exempt.
    assert "feedback_idempotency_key" in sql
    assert "feedback.recorded" in sql


def test_the_index_rejects_a_second_row_for_one_key_directly(request):
    """Proves the index is load-bearing, bypassing the repository entirely.

    If this stopped failing, the repository's duplicate handling would be
    reading a guarantee that no longer exists.
    """
    db = _db(request)
    repo = SqliteFeedbackRepository(db)
    source = _seed(db)
    first = repo.append(_record(source.id))

    stored = db.execute(
        "SELECT id, session_id, type, payload, actor, occurred_at FROM events "
        "WHERE type = 'feedback.recorded'"
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO events (id, session_id, type, payload, message_id, "
            "actor, occurred_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (
                "a-different-event-id",
                stored["session_id"],
                "feedback.recorded",
                stored["payload"],  # same idempotency key, new event id
                stored["actor"],
                stored["occurred_at"],
            ),
        )
    assert repo.list_for_source(source.id)[0].id == first.id


def test_an_existing_version_1_database_receives_the_index_on_reconnect(tmp_path):
    """The deployment path for a database written before the index existed.

    A file is created with the index DROPPED but its version row already at 1
    -- exactly the on-disk state of a database shipped before this change. The
    next `connect` must add the index without a version bump and without a
    migration step, because `_SCHEMA` is `IF NOT EXISTS` and re-applied on
    every open.
    """
    path = tmp_path / "pre-index.db"

    legacy = connect(path)
    repo = SqliteFeedbackRepository(legacy)
    source = _seed(legacy)
    repo.append(_record(source.id))
    legacy.execute("DROP INDEX events_feedback_idem_unique")
    legacy.commit()
    assert "events_feedback_idem_unique" not in _indexes(legacy)
    assert legacy.execute(
        "SELECT version FROM schema_version"
    ).fetchone()["version"] == SCHEMA_VERSION
    legacy.close()

    upgraded = connect(path)
    try:
        assert "events_feedback_idem_unique" in _indexes(upgraded)
        # Version untouched, and the pre-existing row is still readable.
        assert upgraded.execute(
            "SELECT version FROM schema_version"
        ).fetchone()["version"] == SCHEMA_VERSION
        assert len(SqliteFeedbackRepository(upgraded).list_for_source(source.id)) == 1
    finally:
        upgraded.close()


def test_idempotency_holds_across_two_separate_connections(tmp_path):
    """Not merely two handles on one connection: two real connections.

    A shared connection could serialise the two writers for reasons that have
    nothing to do with the index, so this uses two files' worth of
    independent state via two connections to the same database.
    """
    path = tmp_path / "two-connections.db"
    first_db = connect(path)
    try:
        source = _seed(first_db)
        first = SqliteFeedbackRepository(first_db).append(_record(source.id))

        second_db = connect(path)
        try:
            second = SqliteFeedbackRepository(second_db).append(_record(source.id))
        finally:
            second_db.close()

        assert second.id == first.id
        assert first_db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'feedback.recorded'"
        ).fetchone()["n"] == 1
    finally:
        first_db.close()


def test_concurrent_writers_on_one_key_produce_exactly_one_row(tmp_path):
    """Threads, separate connections, same key: the index decides, once.

    Whatever the interleaving, exactly one row exists and every caller that
    believed it inserted is handed the id of the single record that won. This
    is the property the removed `WHERE NOT EXISTS` predicate could not supply
    on a backend with MVCC.
    """
    path = tmp_path / "race.db"
    setup = connect(path)
    source = _seed(setup)
    setup.close()

    start = threading.Barrier(4)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def writer() -> None:
        db = connect(path)
        try:
            start.wait(timeout=10)
            stored = SqliteFeedbackRepository(db).append(_record(source.id))
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            with lock:
                errors.append(exc)
            return
        else:
            with lock:
                results.append(stored.id)
        finally:
            db.close()

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 4
    # Every writer was handed the same record...
    assert len(set(results)) == 1
    # ...and it exists exactly once.
    verify = connect(path)
    try:
        assert verify.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'feedback.recorded'"
        ).fetchone()["n"] == 1
    finally:
        verify.close()


def test_a_failed_connect_closes_its_handle_and_points_at_the_audit(tmp_path, monkeypatch):
    """The index refusing to build must not abandon an open connection.

    `CREATE UNIQUE INDEX events_feedback_idem_unique` is applied by `connect`,
    and it refuses while duplicate keys exist. That refusal used to escape as a
    bare `sqlite3.IntegrityError` with the handle still open, which is the one
    thing a caller cannot act on: they are told the index exists but not that
    the audit is how to find out why. This pins both halves -- the handle is
    closed, and the message names the audit that resolves it.

    Closed is asserted the only way `sqlite3` allows: a closed connection
    refuses to `execute`, an open one answers. So the test captures the very
    connection `connect` opened and asks it directly.
    """
    path = tmp_path / "dupes.db"
    seed = connect(path)
    seed.execute("DROP INDEX events_feedback_idem_unique")
    for row_id in ("f1", "f2"):
        seed.execute(
            "INSERT INTO events (id, session_id, type, payload, actor, occurred_at) "
            "VALUES (?, 's1', 'feedback.recorded', ?, 'user', '2024-01-01')",
            (row_id, json.dumps({"feedback_idempotency_key": "dupe"})),
        )
    seed.commit()
    seed.close()

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy)

    with pytest.raises(sqlite3.IntegrityError) as caught:
        connect(path)

    # The operator is told what to do about it, not just that it happened.
    assert "events_feedback_idem_unique" in str(caught.value)
    assert "audit_feedback_rows" in str(caught.value)

    # The handle `connect` opened is gone, not merely unreachable.
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")

    # And the file is still usable by the next reader -- nothing is locked, and
    # nothing was repaired. Opened RAW, because `connect` would (correctly)
    # refuse to apply the schema while those duplicates are still there.
    monkeypatch.undo()
    raw = real_connect(path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'feedback.recorded'"
        ).fetchone()[0] == 2
    finally:
        raw.close()

# ── the recorder over the durable backend ────────────────────────────────────
# The unused `FeedbackRecorder` import above is this section. It was left
# behind by a test that was never written, and it is why this module had an
# F401 while the in-memory recorder was covered: `test_feedback_recorder.py`
# drives `FeedbackRecorder` against `InMemoryEventRepository` only. The
# `learning -> persistence` path -- the one a real deployment takes -- had no
# test at all. These close it.


def test_the_recorder_drives_the_sqlite_backend_end_to_end(request):
    """A judgement recorded through `FeedbackRecorder` lands in the store.

    The recorder is the only production path that builds a `FeedbackRecord`,
    so the backend being reachable through it is a separate fact from the
    backend being usable by direct construction.
    """
    db = _db(request)
    source = _seed(db)
    recorder = FeedbackRecorder(SqliteFeedbackRepository(db))

    record = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.GOOD
    )

    stored = SqliteFeedbackRepository(db).list_for_source(source.id)
    assert len(stored) == 1
    assert stored[0].id == record.id
    assert stored[0].outcome is FeedbackOutcome.GOOD


def test_the_recorder_stamps_the_effect_the_label_commits_to(request):
    """The effect code survives the round-trip through the durable payload.

    This is the assertion that makes `learning/outcomes.py` load-bearing
    rather than decorative: the table is consulted by the recorder and its
    result is persisted, so a member that lost its row would be visible in
    stored data rather than only in a diff.
    """
    from personal_ai_core.learning.outcomes import EVALUATION_SIGNAL

    db = _db(request)
    source = _seed(db)
    recorder = FeedbackRecorder(SqliteFeedbackRepository(db))

    record = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.WRONG
    )

    assert record.effect == EVALUATION_SIGNAL
    row = db.execute(
        "SELECT payload FROM events WHERE type = 'feedback.recorded'"
    ).fetchone()
    assert json.loads(row[0])["effect"] == EVALUATION_SIGNAL
    assert SqliteFeedbackRepository(db).list_for_source(source.id)[0].effect == (
        EVALUATION_SIGNAL
    )


def test_recording_the_same_judgement_twice_through_the_recorder_appends_nothing(request):
    """Idempotency holds on the path callers actually use."""
    db = _db(request)
    source = _seed(db)
    recorder = FeedbackRecorder(SqliteFeedbackRepository(db))
    repo = SqliteFeedbackRepository(db)

    first = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )
    second = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )

    assert second.id == first.id, "the duplicate must return the stored record"
    assert len(repo.list_for_source(source.id)) == 1


def test_the_recorder_refuses_a_source_that_does_not_exist(request):
    db = _db(request)
    source = _seed(db)
    recorder = FeedbackRecorder(SqliteFeedbackRepository(db))

    with pytest.raises(ValueError, match="does not exist in session"):
        recorder.record(
            source_event_id="no-such-event", session_id="s1", outcome=FeedbackOutcome.GOOD
        )
    assert SqliteFeedbackRepository(db).list_for_source(source.id) == ()
