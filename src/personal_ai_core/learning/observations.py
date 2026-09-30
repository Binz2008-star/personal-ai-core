"""Observation derivation: ADR-017 Unit 2 (Amendment A1, §15.4-§15.5).

    derive_observations(events, feedback) -> (observations, unobserved)

Pure. It takes values, not repositories; performs no I/O; reads no clock;
calls no model; mints no id; and does not mutate its inputs (D7). Its callers
are explicitly authorized consumers only (D4): the tests, and `pac
--observations` (#97), a read-only display that affects no turn.

Ordering is a precondition, not a computation (D7). The caller passes the
feedback in the order the repository returned it, which is `seq` order, and
that order is authoritative: nothing here sorts by `occurred_at` or anything
else. The events are used only to look up each source event, so their order
does not matter.

Nothing here can reach memory: `learning/` imports `core` only.
"""
from __future__ import annotations

from typing import Iterable

from ..core.domain import Event, EventType
from ..core.feedback import CORRECTION_KEY, FeedbackOutcome, FeedbackRecord
from ..core.observation import Observation, UnobservedFeedback
from .outcomes import effect_for


def derive_observations(
    events: Iterable[Event],
    feedback: Iterable[FeedbackRecord],
) -> tuple[tuple[Observation, ...], tuple[UnobservedFeedback, ...]]:
    """One Observation per judged GENERATION_COMPLETED event; the rest unobserved.

    Groups feedback by `source_event_id` regardless of actor (D3). Output
    order follows the first reference of each source in the supplied
    feedback (D7).
    """
    by_id = {event.id: event for event in events}

    groups: dict[str, list[FeedbackRecord]] = {}
    for record in feedback:
        groups.setdefault(record.source_event_id, []).append(record)

    observations: list[Observation] = []
    unobserved: list[UnobservedFeedback] = []
    for source_id, records in groups.items():
        source = by_id.get(source_id)
        if source is None or source.type is not EventType.GENERATION_COMPLETED:
            # D2: returned, never silently discarded, never a failure.
            unobserved.append(
                UnobservedFeedback(
                    source_event_id=source_id,
                    source_event_type=None if source is None else source.type.value,
                    feedback_ids=tuple(r.id for r in records),
                )
            )
            continue
        observations.append(_observe(source, records))

    return tuple(observations), tuple(unobserved)


def _observe(source: Event, records: list[FeedbackRecord]) -> Observation:
    effective = records[-1]  # the last supplied record (D1, D7)
    return Observation(
        session_id=source.session_id,
        source_event_id=source.id,
        source_message_id=source.message_id,
        feedback_ids=tuple(r.id for r in records),
        effective_outcome=effective.outcome,
        effective_effect=effective.effect or effect_for(effective.outcome),
        conflicted=len({r.outcome for r in records}) > 1,
        conflicts=tuple(
            (r.id, r.outcome, r.occurred_at)
            for r in records[:-1]
            if r.outcome is not effective.outcome
        ),
        correction=_correction(effective),
    )


def _correction(record: FeedbackRecord) -> str | None:
    """D5: the effective record's text, only on CORRECTION; no other key read.

    A non-string value is not correction text and is returned as None rather
    than converted, because converting would invent text the user never wrote.
    """
    if record.outcome is not FeedbackOutcome.CORRECTION:
        return None
    text = record.payload.get(CORRECTION_KEY)
    return text if isinstance(text, str) else None
