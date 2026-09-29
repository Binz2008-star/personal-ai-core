"""FeedbackRecorder and InMemoryFeedbackRepository (ADR-017 §3.1).

The durable form of a FeedbackRecord is a FEEDBACK_RECORDED event in the
shared event store, and `append` is atomic and idempotent at the repository
boundary: the same judgment recorded twice records once.
"""
from __future__ import annotations

import pytest

from personal_ai_core.core.feedback import (
    FEEDBACK_EVENT_TYPE,
    FEEDBACK_IDEMPOTENCY_KEY,
    FeedbackOutcome,
    FeedbackRecord,
    as_feedback_event,
    feedback_idempotency_key,
    feedback_record_from_event,
)
from personal_ai_core.core.domain import Event, EventType
from personal_ai_core.learning.feedback import FeedbackRecorder
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryFeedbackRepository,
)


def _seed_event(
    events: InMemoryEventRepository, session_id: str = "s1"
) -> Event:
    event = Event(
        session_id=session_id,
        type=EventType.GENERATION_COMPLETED,
        payload={"text": "reply"},
    )
    events.append(event)
    return event


def _recorder() -> tuple[FeedbackRecorder, InMemoryFeedbackRepository, InMemoryEventRepository]:
    events = InMemoryEventRepository()
    repo = InMemoryFeedbackRepository(events)
    return FeedbackRecorder(repo), repo, events


def test_recording_feedback_emits_one_feedback_recorded_event_with_the_reserved_key():
    recorder, _, events = _recorder()
    source = _seed_event(events)

    recorded = recorder.record(
        source_event_id=source.id,
        session_id="s1",
        outcome=FeedbackOutcome.BAD,
    )

    assert recorded.outcome is FeedbackOutcome.BAD
    all_events = events.all()
    assert len(all_events) == 2  # the source event + the feedback event
    (feedback_event,) = [e for e in all_events if e.id != source.id]
    assert feedback_event.type is FEEDBACK_EVENT_TYPE
    assert feedback_event.type is EventType.FEEDBACK_RECORDED
    assert feedback_event.payload[FEEDBACK_IDEMPOTENCY_KEY] == recorded.idempotency_key
    assert feedback_event.payload["source_event_id"] == source.id
    assert feedback_event.payload["session_id"] == "s1"
    assert feedback_event.payload["outcome"] == "bad"
    assert feedback_event.payload["feedback_id"] == recorded.id


def test_recording_the_same_judgment_twice_is_an_atomic_noop():
    recorder, repo, events = _recorder()
    source = _seed_event(events)

    first = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.GOOD
    )
    second = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.GOOD
    )

    assert second is not first
    assert second == first
    assert second.id == first.id
    feedback_events = [
        e for e in events.all() if e.type is FEEDBACK_EVENT_TYPE
    ]
    assert len(feedback_events) == 1
    assert len(repo.list_for_session("s1")) == 1


def test_a_retry_returns_the_stored_record_and_appends_nothing():
    recorder, repo, events = _recorder()
    source = _seed_event(events)

    # Caller retries after a confused first attempt: identical key, identical
    # judgment. The repository boundary deduplicates, not the caller.
    recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )
    retried = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )
    assert retried.id != source.id
    assert len(repo.list_for_source(source.id)) == 1
    assert len([e for e in events.all() if e.type is FEEDBACK_EVENT_TYPE]) == 1


def test_a_different_outcome_is_a_different_judgment_and_both_are_recorded():
    recorder, repo, events = _recorder()
    source = _seed_event(events)

    recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.GOOD
    )
    recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )

    assert [r.outcome for r in repo.list_for_source(source.id)] == [
        FeedbackOutcome.GOOD,
        FeedbackOutcome.BAD,
    ]


def test_source_event_must_exist_or_append_raises():
    recorder, repo, events = _recorder()

    with pytest.raises(ValueError, match="does not exist in session"):
        recorder.record(
            source_event_id="no-such-event",
            session_id="s1",
            outcome=FeedbackOutcome.BAD,
        )
    # Nothing was recorded and no feedback event leaked into the stream.
    assert repo.list_for_session("s1") == ()
    assert all(e.type is not FEEDBACK_EVENT_TYPE for e in events.all())


def test_source_event_in_a_different_session_is_refused():
    recorder, _, events = _recorder()
    source = _seed_event(events, session_id="s1")

    with pytest.raises(ValueError, match="does not exist in session"):
        recorder.record(
            source_event_id=source.id,
            session_id="other-session",
            outcome=FeedbackOutcome.BAD,
        )


def test_unknown_outcome_is_refused_at_construction():
    with pytest.raises(ValueError):
        FeedbackOutcome("bogus")


def test_a_payload_that_cannot_be_stored_is_refused():
    events = InMemoryEventRepository()
    repo = InMemoryFeedbackRepository(events)
    source = _seed_event(events)

    with pytest.raises(ValueError):
        repo.append(
            FeedbackRecord(
                idempotency_key=feedback_idempotency_key(
                    session_id="s1",
                    source_event_id=source.id,
                    outcome=FeedbackOutcome.BAD,
                    actor="user",
                ),
                source_event_id=source.id,
                session_id="s1",
                actor="user",
                outcome=FeedbackOutcome.BAD,
                payload={"obj": object()},
            )
        )


def test_feedback_record_round_trips_through_its_durable_event():
    events = InMemoryEventRepository()
    repo = InMemoryFeedbackRepository(events)
    source = _seed_event(events)

    recorded = repo.append(
        FeedbackRecord(
            idempotency_key=feedback_idempotency_key(
                session_id="s1",
                source_event_id=source.id,
                outcome=FeedbackOutcome.REMEMBER_THIS,
                actor="user",
            ),
            source_event_id=source.id,
            session_id="s1",
            actor="user",
            outcome=FeedbackOutcome.REMEMBER_THIS,
            payload={"memory_id": "m-1", "note": "keep it"},
        )
    )

    event = next(
        e for e in events.all() if e.type is FEEDBACK_EVENT_TYPE
    )
    rebuilt = feedback_record_from_event(event)
    assert rebuilt == recorded
    assert rebuilt.id == recorded.id
    assert rebuilt.idempotency_key == recorded.idempotency_key
    assert rebuilt.outcome is FeedbackOutcome.REMEMBER_THIS
    assert dict(rebuilt.payload) == {"memory_id": "m-1", "note": "keep it"}
    assert rebuilt.occurred_at == recorded.occurred_at


def test_feedback_record_from_event_refuses_other_event_types():
    event = Event(session_id="s1", type=EventType.MESSAGE_RECEIVED, payload={})
    with pytest.raises(ValueError, match="requires a FEEDBACK_RECORDED"):
        feedback_record_from_event(event)


def test_as_feedback_event_is_the_durable_serialisation_and_it_is_json_safe():
    import json

    events = InMemoryEventRepository()
    source = _seed_event(events)
    recorded = FeedbackRecord(
        idempotency_key=feedback_idempotency_key(
            session_id="s1",
            source_event_id=source.id,
            outcome=FeedbackOutcome.PREFERENCE,
            actor="user",
        ),
        source_event_id=source.id,
        session_id="s1",
        actor="user",
        outcome=FeedbackOutcome.PREFERENCE,
    )
    event = as_feedback_event(recorded)
    # The durable form must be writable down (the Event payload contract).
    json.dumps(dict(event.payload))
    assert event.type is EventType.FEEDBACK_RECORDED


def test_list_for_session_returns_feedback_in_total_order():
    recorder, repo, events = _recorder()
    first_source = _seed_event(events, "s1")
    second_source = _seed_event(events, "s1")

    recorder.record(
        source_event_id=first_source.id, session_id="s1", outcome=FeedbackOutcome.BAD
    )
    recorder.record(
        source_event_id=second_source.id, session_id="s1", outcome=FeedbackOutcome.GOOD
    )

    assert [r.source_event_id for r in repo.list_for_session("s1")] == [
        first_source.id,
        second_source.id,
    ]
    # Other sessions are never visible.
    recorder.record(
        source_event_id=_seed_event(events, "s2").id,
        session_id="s2",
        outcome=FeedbackOutcome.GOOD,
    )
    assert [r.session_id for r in repo.list_for_session("s1")] == ["s1", "s1"]