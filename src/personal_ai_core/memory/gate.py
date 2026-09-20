"""The promotion gate.

A pure decision function. The gate never writes, never emits and never
mutates. The pipeline acts on its verdicts.

Three decisions:

- PROMOTED: the candidate crosses its type's confidence threshold and
  contradicts no active record. The pipeline writes an ACTIVE record.
- REJECTED: the candidate falls below the threshold, or duplicates an
  existing active record verbatim (idempotency). The pipeline writes a
  REJECTED record so the audit trail records the attempt.
- HELD: the candidate contradicts an active record. The pipeline records
  the conflict without silently overwriting; resolution is a later
  operation, not part of the gate's job.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from ..core.memory import (
    MemoryCandidate,
    MemoryRecord,
    MemoryType,
    PromotionDecision,
    PromotionOutcome,
)


# Defaults are per MEMORY_ARCHITECTURE.md and are conservative enough that a
# noisy match cannot cross the line on its own. Overrides go through
# `DefaultPromotionGate(thresholds=...)` so a change is one place and one
# reviewer, not scattered constants.
_DEFAULT_THRESHOLDS: Mapping[MemoryType, float] = {
    MemoryType.PREFERENCES: 0.75,
    MemoryType.LESSONS: 0.70,
    MemoryType.SEMANTIC: 0.65,
    MemoryType.EPISODIC: 0.65,
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


class DefaultPromotionGate:
    """Threshold + idempotency + conflict resolution.

    Contradiction is detected on same `type` and same `session_id` when the
    normalized content differs from an active record but shares a
    normalization prefix of at least `_CONFLICT_PREFIX` characters. That is
    intentionally simple: better a real contradiction reported as a hold
    than a subtle heuristic that hides a real conflict.
    """

    _CONFLICT_PREFIX = 12

    def __init__(
        self,
        *,
        thresholds: Mapping[MemoryType, float] | None = None,
    ) -> None:
        self._thresholds = dict(_DEFAULT_THRESHOLDS)
        if thresholds is not None:
            self._thresholds.update(thresholds)

    def evaluate(
        self,
        candidate: MemoryCandidate,
        active: Sequence[MemoryRecord],
    ) -> PromotionOutcome:
        threshold = self._thresholds.get(candidate.type)
        if threshold is None:
            return PromotionOutcome(
                decision=PromotionDecision.REJECTED,
                candidate_id=candidate.id,
                reason=(
                    f"no threshold configured for MemoryType {candidate.type.value!r}"
                ),
            )

        if candidate.confidence < threshold:
            return PromotionOutcome(
                decision=PromotionDecision.REJECTED,
                candidate_id=candidate.id,
                reason=(
                    f"confidence {candidate.confidence:.2f} below threshold "
                    f"{threshold:.2f} for {candidate.type.value}"
                ),
            )

        candidate_norm = _normalize(candidate.content)
        candidate_session = candidate.provenance.session_id

        for record in active:
            if record.session_id != candidate_session:
                continue
            if record.type is not candidate.type:
                continue
            record_norm = _normalize(record.content)
            if record_norm == candidate_norm:
                # Idempotency: the same claim, already recorded. This is a
                # rejection so the attempt is auditable, not a silent no-op.
                return PromotionOutcome(
                    decision=PromotionDecision.REJECTED,
                    candidate_id=candidate.id,
                    reason=(
                        f"idempotent: identical active record {record.id} already exists"
                    ),
                    conflicts_with=record.id,
                )
            prefix = self._CONFLICT_PREFIX
            if (
                len(record_norm) >= prefix
                and len(candidate_norm) >= prefix
                and record_norm[:prefix] == candidate_norm[:prefix]
            ):
                return PromotionOutcome(
                    decision=PromotionDecision.HELD,
                    candidate_id=candidate.id,
                    reason=(
                        f"conflict with active record {record.id}: "
                        f"same type and session, differing content"
                    ),
                    conflicts_with=record.id,
                )

        return PromotionOutcome(
            decision=PromotionDecision.PROMOTED,
            candidate_id=candidate.id,
            reason=(
                f"confidence {candidate.confidence:.2f} meets threshold "
                f"{threshold:.2f}; no conflict"
            ),
        )
