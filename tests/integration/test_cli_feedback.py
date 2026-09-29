"""`pac --feedback`: a judgement of the latest reply, recorded and nothing else.

B1 of the Events / Feedback / Learning scope. The source event is the
session's latest GENERATION_COMPLETED, in the event store's own order; the
write is `FeedbackRecorder.record` through the SQLite `FeedbackRepository`,
whose unique index is the only duplicate arbiter.

Each test drives the real entry point, then reads the database file back
through a fresh connection -- the store is the witness, not the output.
"""
from __future__ import annotations

import io
import re
from typing import Any, Mapping

from personal_ai_core.app.cli import CORRECTION_KEY, main
from personal_ai_core.conversation.factory import PersistentSlice, ServerSlice
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.core.feedback import (
    FEEDBACK_EVENT_TYPE,
    FEEDBACK_IDEMPOTENCY_KEY,
    FeedbackOutcome,
)
from personal_ai_core.learning.outcomes import effect_for
from personal_ai_core.persistence.sqlite import (
    SqliteEventRepository,
    SqliteFeedbackRepository,
    SqliteMemoryRepository,
    SqliteMessageRepository,
    connect,
)


def transport_saying(text: str):
    def _transport(url: str, payload: Mapping[str, Any], timeout: int):
        return {"model": payload["model"], "message": {"content": text}}

    return _transport


def no_model(url: str, payload: Mapping[str, Any], timeout: int):
    raise ProviderError("feedback must not call the model")


def run(argv, *, lines=(), transport=None):
    out = io.StringIO()
    code = main(
        argv,
        transport=transport or transport_saying("ok"),
        stdin=iter(lines),
        stdout=out,
        env={},
    )
    return code, out.getvalue()


def converse(db, *lines: str) -> str:
    """Start a stored session, take `lines` as turns, return the session id."""
    code, output = run(["--database", str(db)], lines=lines)
    assert code == 0, output
    match = re.search(r"^session: (\S+)", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def feedback(db, session_id, label, *extra, transport=no_model):
    # `no_model` by default: every feedback run here would fail loudly if it
    # reached the model, which is the "never uses the Boss model" check.
    return run(
        ["--database", str(db), "--session", session_id, "--feedback", label, *extra],
        transport=transport,
    )


def stored(db):
    """Fresh connection: what a later process would read."""
    connection = connect(db)
    try:
        events = SqliteEventRepository(connection).all()
        records = []
        for session_id in {e.session_id for e in events}:
            records.extend(SqliteFeedbackRepository(connection).list_for_session(session_id))
        messages = sum(
            len(SqliteMessageRepository(connection).list_for_session(s))
            for s in {e.session_id for e in events}
        )
        memories = SqliteMemoryRepository(connection).list_active()
        return events, records, messages, memories
    finally:
        connection.close()


def feedback_events(events):
    return [e for e in events if e.type is FEEDBACK_EVENT_TYPE]


def replies(events):
    return [e for e in events if e.type is EventType.GENERATION_COMPLETED]


# --- it records -----------------------------------------------------------


def test_valid_feedback_records_exactly_one_feedback_event(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = feedback(db, session_id, "good")

    assert code == 0, output
    assert "feedback recorded: good" in output
    events, records, _, _ = stored(db)
    assert len(feedback_events(events)) == 1
    [record] = records
    assert record.outcome is FeedbackOutcome.GOOD
    assert record.actor == "user"
    assert record.session_id == session_id
    assert record.effect == effect_for(FeedbackOutcome.GOOD)
    assert dict(record.payload) == {}
    assert FEEDBACK_IDEMPOTENCY_KEY in feedback_events(events)[0].payload


def test_the_source_is_the_latest_generation_completed(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "first", "second", "third")

    code, output = feedback(db, session_id, "bad")

    assert code == 0, output
    events, [record], _, _ = stored(db)
    session_replies = [e for e in replies(events) if e.session_id == session_id]
    assert len(session_replies) == 3
    assert record.source_event_id == session_replies[-1].id
    assert session_replies[-1].id in output


def test_a_reply_in_a_later_run_becomes_the_new_source(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "first")
    feedback(db, session_id, "good")
    code, output = run(
        ["--database", str(db), "--session", session_id], lines=["again"]
    )
    assert code == 0, output

    code, output = feedback(db, session_id, "good")

    assert code == 0, output
    events, records, _, _ = stored(db)
    assert len(records) == 2
    assert {r.source_event_id for r in records} == {e.id for e in replies(events)}


# --- it refuses, cleanly and without writing ------------------------------


def test_a_session_with_no_reply_yet_is_refused(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db)  # session started, no turn taken

    code, output = feedback(db, session_id, "good")

    assert code == 2
    assert f"session {session_id} has no reply to give feedback on yet" in output
    events, records, _, _ = stored(db)
    assert records == [] and feedback_events(events) == []


def test_an_unknown_session_is_refused(tmp_path):
    db = tmp_path / "core.db"
    converse(db, "hello")

    code, output = feedback(db, "no-such-session", "good")

    assert code == 2
    assert "no such session: no-such-session" in output
    assert feedback_events(stored(db)[0]) == []


def test_a_missing_database_is_refused_and_not_created(tmp_path):
    db = tmp_path / "absent.db"

    code, output = feedback(db, "any", "good")

    assert code == 2
    assert "no stored conversations at" in output
    assert not db.exists()


def test_an_unknown_label_is_refused_before_anything_is_opened(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")
    before = db.stat().st_mtime_ns

    code, output = feedback(db, session_id, "excellent")

    assert code == 2
    assert "unknown feedback label: 'excellent'" in output
    for outcome in FeedbackOutcome:
        assert outcome.value in output
    assert db.stat().st_mtime_ns == before
    assert feedback_events(stored(db)[0]) == []


def test_feedback_needs_a_session(tmp_path):
    db = tmp_path / "core.db"
    converse(db, "hello")

    code, output = run(["--database", str(db), "--feedback", "good"], transport=no_model)

    assert code == 2
    assert "--feedback needs --session ID" in output


def test_feedback_is_not_offered_on_the_ephemeral_slice():
    code, output = run(
        ["--ephemeral", "--session", "s", "--feedback", "good"], transport=no_model
    )

    assert code == 2
    assert "cannot be used with --ephemeral" in output


def test_feedback_does_not_combine_with_a_conversation_mode(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = feedback(db, session_id, "good", "--agent")

    assert code == 2
    assert "cannot be combined" in output
    assert feedback_events(stored(db)[0]) == []


# --- idempotency ----------------------------------------------------------


def test_the_same_judgement_twice_is_stored_once(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    first_code, first = feedback(db, session_id, "wrong")
    second_code, second = feedback(db, session_id, "wrong")

    assert (first_code, second_code) == (0, 0)
    assert "feedback recorded: wrong" in first
    assert "feedback already recorded: wrong" in second
    events, records, _, _ = stored(db)
    assert len(records) == 1
    assert len(feedback_events(events)) == 1


def test_a_different_label_on_the_same_reply_is_a_separate_judgement(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    feedback(db, session_id, "good")
    feedback(db, session_id, "too_slow")

    _, records, _, _ = stored(db)
    assert sorted(r.outcome.value for r in records) == ["good", "too_slow"]


# --- durability -----------------------------------------------------------


def test_feedback_survives_reopening_the_database(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")
    feedback(db, session_id, "remember_this")

    # A new process on the same file: the conversation continues, and the
    # judgement is still there to be read.
    code, output = run(
        ["--database", str(db), "--session", session_id], lines=["still there?"]
    )
    assert code == 0, output

    _, records, _, _ = stored(db)
    assert [r.outcome for r in records] == [FeedbackOutcome.REMEMBER_THIS]


# --- CORRECTION and payload -----------------------------------------------


def test_correction_carries_its_text(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "what is the capital of Australia?")

    code, output = feedback(
        db, session_id, "correction", "--correction", "  Canberra,\n not Sydney  "
    )

    assert code == 0, output
    _, [record], _, _ = stored(db)
    assert record.outcome is FeedbackOutcome.CORRECTION
    assert dict(record.payload) == {CORRECTION_KEY: "Canberra, not Sydney"}


def test_correction_without_text_is_accepted_with_no_payload(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = feedback(db, session_id, "correction")

    assert code == 0, output
    _, [record], _, _ = stored(db)
    assert dict(record.payload) == {}


def test_correction_text_on_another_label_is_refused(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = feedback(db, session_id, "good", "--correction", "text")

    assert code == 2
    assert "--correction goes with --feedback correction, not good" in output
    assert feedback_events(stored(db)[0]) == []


def test_correction_text_without_feedback_is_refused(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = run(
        ["--database", str(db), "--session", session_id, "--correction", "text"],
        transport=no_model,
    )

    assert code == 2
    assert "--correction goes with --feedback correction" in output
    assert feedback_events(stored(db)[0]) == []


def test_blank_correction_text_is_refused(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello")

    code, output = feedback(db, session_id, "correction", "--correction", "  \n ")

    assert code == 2
    assert "--correction needs the text" in output
    assert feedback_events(stored(db)[0]) == []


# --- what it must not touch -----------------------------------------------


def test_feedback_writes_no_memory_and_leaves_the_conversation_unchanged(tmp_path):
    db = tmp_path / "core.db"
    session_id = converse(db, "hello", "remember that I prefer tea")
    events_before, _, messages_before, memories_before = stored(db)

    for label in ("remember_this", "forget_this", "preference", "correction"):
        code, output = feedback(db, session_id, label)
        assert code == 0, output

    events_after, records, messages_after, memories_after = stored(db)
    assert len(memories_before) == 0 and len(memories_after) == 0
    assert messages_after == messages_before
    # The only new events are the four judgements.
    new = [e for e in events_after if e.id not in {b.id for b in events_before}]
    assert len(new) == 4 and all(e.type is FEEDBACK_EVENT_TYPE for e in new)
    assert len(records) == 4


def test_the_feedback_path_is_on_the_sqlite_slice_only():
    assert "feedback" in PersistentSlice.__dataclass_fields__
    assert "feedback" not in ServerSlice.__dataclass_fields__
