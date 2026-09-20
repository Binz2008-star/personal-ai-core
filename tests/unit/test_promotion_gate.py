"""The promotion gate is a pure function: threshold, idempotency, conflict."""
from __future__ import annotations

from personal_ai_core.core.memory import (
    MemoryCandidate,
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    PromotionDecision,
)
from personal_ai_core.memory.gate import DefaultPromotionGate


def _prov(session: str = "s1") -> MemoryProvenance:
    return MemoryProvenance(
        session_id=session, event_id="e1", promoted_by="rule:test"
    )


def _candidate(
    *,
    content: str = "prefers Arabic",
    memory_type: MemoryType = MemoryType.PREFERENCES,
    confidence: float = 0.9,
    session: str = "s1",
) -> MemoryCandidate:
    return MemoryCandidate(
        experience_id="x1",
        type=memory_type,
        content=content,
        language="en",
        confidence=confidence,
        provenance=_prov(session),
        rule="rule:test",
        rationale="",
    )


def _active(
    *,
    content: str,
    memory_type: MemoryType = MemoryType.PREFERENCES,
    session: str = "s1",
) -> MemoryRecord:
    return MemoryRecord(
        session_id=session,
        type=memory_type,
        content=content,
        language="en",
        provenance=_prov(session),
        status=MemoryStatus.ACTIVE,
        confidence=0.9,
    )


def test_promotes_when_confidence_meets_threshold_and_no_conflict():
    gate = DefaultPromotionGate()
    outcome = gate.evaluate(_candidate(confidence=0.9), active=())
    assert outcome.decision is PromotionDecision.PROMOTED
    assert outcome.conflicts_with is None


def test_rejects_when_confidence_below_threshold():
    gate = DefaultPromotionGate()
    outcome = gate.evaluate(_candidate(confidence=0.5), active=())
    assert outcome.decision is PromotionDecision.REJECTED
    assert "threshold" in outcome.reason


def test_idempotency_rejects_a_duplicate_active_record():
    gate = DefaultPromotionGate()
    existing = _active(content="prefers Arabic")
    outcome = gate.evaluate(
        _candidate(content="  Prefers  Arabic  "), active=(existing,)
    )
    assert outcome.decision is PromotionDecision.REJECTED
    assert outcome.conflicts_with == existing.id
    assert "idempotent" in outcome.reason.lower()


def test_conflicting_content_is_held_not_overwritten():
    gate = DefaultPromotionGate()
    existing = _active(content="prefers Arabic replies please")
    outcome = gate.evaluate(
        _candidate(content="prefers Arabic replies only in short form"),
        active=(existing,),
    )
    assert outcome.decision is PromotionDecision.HELD
    assert outcome.conflicts_with == existing.id


def test_different_session_does_not_conflict():
    gate = DefaultPromotionGate()
    existing = _active(content="prefers Arabic", session="s-other")
    outcome = gate.evaluate(
        _candidate(content="prefers something else entirely", session="s1"),
        active=(existing,),
    )
    assert outcome.decision is PromotionDecision.PROMOTED


def test_different_type_does_not_conflict():
    gate = DefaultPromotionGate()
    existing = _active(
        content="prefers Arabic replies", memory_type=MemoryType.PREFERENCES
    )
    outcome = gate.evaluate(
        _candidate(
            content="prefers Arabic answers", memory_type=MemoryType.LESSONS
        ),
        active=(existing,),
    )
    assert outcome.decision is PromotionDecision.PROMOTED


def test_gate_never_writes_and_never_emits():
    """A pure decision does not mutate its inputs."""
    gate = DefaultPromotionGate()
    existing = _active(content="prefers Arabic")
    active = (existing,)
    gate.evaluate(_candidate(content="prefers Arabic"), active=active)
    # `active` is still a one-tuple with the same record identity.
    assert active == (existing,)
    assert existing.status is MemoryStatus.ACTIVE


def test_threshold_override_applies():
    gate = DefaultPromotionGate(thresholds={MemoryType.PREFERENCES: 0.99})
    outcome = gate.evaluate(_candidate(confidence=0.9), active=())
    assert outcome.decision is PromotionDecision.REJECTED
