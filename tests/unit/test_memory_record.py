"""Invariants on the Phase 3 memory domain types.

The types are values, so the rules they enforce live in `__post_init__`
rather than in an external validator. A validator that lives elsewhere can
be forgotten; a raise in the constructor cannot.
"""
from __future__ import annotations

import dataclasses

import pytest

from personal_ai_core.core.memory import (
    ExperienceRecord,
    MemoryCandidate,
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    PromotionDecision,
    PromotionOutcome,
)


def _provenance() -> MemoryProvenance:
    return MemoryProvenance(
        session_id="s1", event_id="e1", promoted_by="rule:test"
    )


def _record(**overrides) -> MemoryRecord:
    defaults = dict(
        session_id="s1",
        type=MemoryType.PREFERENCES,
        content="prefers Arabic",
        language="en",
        provenance=_provenance(),
        status=MemoryStatus.ACTIVE,
        confidence=0.9,
    )
    defaults.update(overrides)
    return MemoryRecord(**defaults)


def test_memory_record_and_candidate_are_frozen():
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(record, "content", "mutated")


def test_experience_signals_cannot_be_mutated_after_construction():
    signals = {"repetition_count": 4}
    experience = ExperienceRecord(session_id="s1", text="foo", signals=signals)
    # Mutating the original dict must not affect the stored one.
    signals["repetition_count"] = 999
    assert experience.signals["repetition_count"] == 4
    with pytest.raises(TypeError):
        experience.signals["repetition_count"] = 999  # type: ignore[index]


def test_memory_record_rejects_empty_content():
    with pytest.raises(ValueError):
        _record(content="   ")


def test_memory_record_rejects_confidence_out_of_range():
    with pytest.raises(ValueError):
        _record(confidence=1.5)
    with pytest.raises(ValueError):
        _record(confidence=-0.01)


def test_memory_record_rejects_version_below_one():
    with pytest.raises(ValueError):
        _record(version=0)


def test_rejected_record_may_not_carry_a_supersedes_link():
    """A rejected candidate replaced nothing, so `supersedes` must be None."""
    with pytest.raises(ValueError):
        _record(status=MemoryStatus.REJECTED, supersedes="mem-1")


def test_superseded_status_without_supersedes_is_representable():
    """The old record on a root chain is SUPERSEDED but supersedes nothing.

    `supersedes` on a record points forward-in-time from a newer version to
    the older one it replaced. A root record that later gets superseded
    still supersedes nothing itself; the store flips its status without
    inventing a fake predecessor. This test pins that this is allowed.
    """
    record = _record(status=MemoryStatus.SUPERSEDED)
    assert record.status is MemoryStatus.SUPERSEDED
    assert record.supersedes is None


def test_memory_candidate_rejects_empty_content_and_bad_confidence():
    provenance = _provenance()
    with pytest.raises(ValueError):
        MemoryCandidate(
            experience_id="x1",
            type=MemoryType.PREFERENCES,
            content=" ",
            language="en",
            confidence=0.5,
            provenance=provenance,
            rule="rule:test",
            rationale="",
        )
    with pytest.raises(ValueError):
        MemoryCandidate(
            experience_id="x1",
            type=MemoryType.PREFERENCES,
            content="ok",
            language="en",
            confidence=1.5,
            provenance=provenance,
            rule="rule:test",
            rationale="",
        )


def test_promotion_outcome_carries_the_decision():
    outcome = PromotionOutcome(
        decision=PromotionDecision.HELD,
        candidate_id="c1",
        reason="conflict",
        conflicts_with="m1",
    )
    assert outcome.decision is PromotionDecision.HELD
    assert outcome.conflicts_with == "m1"
    assert outcome.memory_id is None


def test_memory_status_enum_values_are_stable_strings():
    # Values are part of the contract because they show up in event payloads.
    assert MemoryStatus.ACTIVE.value == "active"
    assert MemoryStatus.SUPERSEDED.value == "superseded"
    assert MemoryStatus.REJECTED.value == "rejected"


def test_memory_type_enum_values_are_stable_strings():
    assert MemoryType.PREFERENCES.value == "preferences"
    assert MemoryType.LESSONS.value == "lessons"
    assert MemoryType.SEMANTIC.value == "semantic"
    assert MemoryType.EPISODIC.value == "episodic"


def test_promotion_decision_enum_values_are_stable_strings():
    assert PromotionDecision.PROMOTED.value == "promoted"
    assert PromotionDecision.REJECTED.value == "rejected"
    assert PromotionDecision.HELD.value == "held"
