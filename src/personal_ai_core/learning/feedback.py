"""Feedback recording (ADR-017 §3.1).

`FeedbackRecorder` is how a user's (or rule's) judgement of a prior event
enters the system. It is constructed with the `core.contracts.FeedbackRepository`
boundary only -- the boundary owns the atomic, idempotent `append` and the
source-event-existence check (review point 1) -- and it has no access to
events, memories or the promotion store, so it cannot write anything except
through that boundary.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..core.contracts import FeedbackRepository
from ..core.feedback import (
    FeedbackOutcome,
    FeedbackRecord,
    feedback_idempotency_key,
)
from .outcomes import effect_for


class FeedbackRecorder:
    """Records a judgement of one prior event through the repository boundary.

    `record` builds the deterministic idempotency key and delegates to the
    boundary's atomic `append`. Recording the same judgement twice is a
    no-op that returns the stored (first) record -- the caller can observe
    the duplicate by comparing `returned.id == record.id`.

    It also resolves the label's effect code (ADR-017 §3.2) and stores it on
    the record, so a `FeedbackRecorder`-produced record always carries the
    disposition its label commits to. The resolution is pure and local; no
    rule runs, no observation is derived, and nothing is promoted.
    """

    def __init__(self, repo: FeedbackRepository) -> None:
        self._repo = repo

    def record(
        self,
        *,
        source_event_id: str,
        session_id: str,
        outcome: FeedbackOutcome,
        actor: str = "user",
        payload: Mapping[str, Any] | None = None,
    ) -> FeedbackRecord:
        key = feedback_idempotency_key(
            session_id=session_id,
            source_event_id=source_event_id,
            outcome=outcome,
            actor=actor,
        )
        record = FeedbackRecord(
            idempotency_key=key,
            source_event_id=source_event_id,
            session_id=session_id,
            actor=actor,
            outcome=outcome,
            effect=effect_for(outcome),
            payload=dict(payload or {}),
        )
        return self._repo.append(record)