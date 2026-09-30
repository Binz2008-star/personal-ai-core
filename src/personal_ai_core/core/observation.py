"""The Observation value (ADR-017 Amendment A1, §15.2-§15.3).

An Observation says what the feedback on one reply amounts to: which reply
was judged, by which records, with what effective outcome, and whether the
judgements disagree. It is a frozen, in-memory value produced by a pure
function (`learning.observations.derive_observations`). It is not persisted,
not an event, not feedback and not memory (review point 6).

It has no random id. It is identified by what it was derived from,
`(source_event_id, feedback_ids)`, so equal input gives an equal value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .feedback import FeedbackOutcome

# (feedback_id, outcome, occurred_at). occurred_at is carried as data and is
# never used to order anything (D7).
Conflict = tuple[str, FeedbackOutcome, datetime]


@dataclass(frozen=True, slots=True)
class Observation:
    """One judged GENERATION_COMPLETED event and the feedback on it (§15.3)."""

    session_id: str
    source_event_id: str
    # The assistant message the source event names, or None if it names none.
    source_message_id: str | None
    # Every record on the source event, in the order supplied (the
    # repository's seq order, D7).
    feedback_ids: tuple[str, ...]
    # The outcome of the last supplied record (D1).
    effective_outcome: FeedbackOutcome
    # The effect code stored on that record, carried forward rather than
    # re-derived; resolved from the outcome only when the stored code is empty.
    effective_effect: str
    # True iff the records hold more than one distinct outcome (D1). A
    # conflicted Observation is not promotable.
    conflicted: bool
    # Earlier records whose outcome differs from the effective one, in the
    # order supplied.
    conflicts: tuple[Conflict, ...]
    # The effective record's correction text, only when the effective outcome
    # is CORRECTION (D5). User-written and untrusted at every later stage.
    correction: str | None


@dataclass(frozen=True, slots=True)
class UnobservedFeedback:
    """Feedback whose source is not an eligible GENERATION_COMPLETED event (D2).

    Returned rather than discarded, so a caller can inspect or report it.
    `source_event_type` is the stored type value, or None when the source
    event is absent from the supplied events. No new enum is introduced.
    """

    source_event_id: str
    source_event_type: str | None
    feedback_ids: tuple[str, ...]
