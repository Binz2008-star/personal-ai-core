"""Extraction rules.

A rule inspects an experience and, if it recognizes a memorable claim,
proposes a `MemoryCandidate`. Rules are pure and side-effect free: they
never write, never emit events, never call the gate or the pipeline.

Each rule has a documented producer for exactly one `MemoryType`. That
one-to-one mapping is not architectural pedantry; it is what makes the
producer accountability real. A new type may be added when a new rule
produces it, not before.

Language handling
-----------------

`ExplicitInstructionRule` inspects both English and Arabic markers; the
claim it captures is language-agnostic (the user is stating a preference).
`CorrectionRule` inspects **English markers only**. Arabic corrections are
still stored and retrieved by the rest of the system -- only the *detection*
of a correction signal in English text is what this rule does. Extending
the rule to Arabic markers is a later change; adding a stub that pretends
to detect Arabic while not really would be worse than the current gap.
"""
from __future__ import annotations

import re
from typing import Protocol, Sequence, runtime_checkable

from ..core.domain import UNDETERMINED_LANGUAGE
from ..core.memory import (
    ExperienceRecord,
    MemoryCandidate,
    MemoryProvenance,
    MemoryType,
)


@runtime_checkable
class ExtractionRule(Protocol):
    """A pure proposer of memory candidates.

    Every rule declares its identifier and the `MemoryType` it produces, so
    the pipeline can carry the audit trail without asking the rule to lie
    about itself.
    """

    @property
    def rule_id(self) -> str: ...

    @property
    def produces(self) -> MemoryType: ...

    def propose(self, experience: ExperienceRecord) -> Sequence[MemoryCandidate]: ...


def _provenance(
    experience: ExperienceRecord, promoted_by: str
) -> MemoryProvenance:
    event_id = experience.event_ids[0] if experience.event_ids else experience.id
    return MemoryProvenance(
        session_id=experience.session_id,
        event_id=event_id,
        message_id=experience.message_id,
        promoted_by=promoted_by,
    )


def _language(experience: ExperienceRecord) -> str:
    hint = experience.signals.get("language")
    if isinstance(hint, str) and hint:
        return hint
    return UNDETERMINED_LANGUAGE


class ExplicitInstructionRule:
    """Detects a user directly stating a preference.

    English markers: "I prefer", "I like", "always ...", "please always".
    Arabic markers: "أفضل" (I prefer), "دائما" (always).

    Produces `MemoryType.PREFERENCES`.
    """

    rule_id = "rule:explicit_instruction"
    produces = MemoryType.PREFERENCES

    _english_markers = (
        re.compile(r"\bI (?:prefer|like|always|want|need)\b", re.IGNORECASE),
        re.compile(r"\bplease always\b", re.IGNORECASE),
        re.compile(r"\balways (?:use|answer|reply|give me)\b", re.IGNORECASE),
    )
    _arabic_markers = (
        re.compile(r"أفضل"),
        re.compile(r"دائما"),
    )

    def propose(
        self, experience: ExperienceRecord
    ) -> Sequence[MemoryCandidate]:
        text = experience.text.strip()
        if not text:
            return ()
        matched = any(p.search(text) for p in self._english_markers) or any(
            p.search(text) for p in self._arabic_markers
        )
        if not matched:
            return ()
        return (
            MemoryCandidate(
                experience_id=experience.id,
                type=self.produces,
                content=text,
                language=_language(experience),
                confidence=0.85,
                provenance=_provenance(experience, self.rule_id),
                rule=self.rule_id,
                rationale="explicit preference marker detected",
            ),
        )


class CorrectionRule:
    """Detects the user correcting a prior statement or belief.

    English markers only: "no, actually", "that's wrong", "correction:",
    "I meant". Arabic correction detection is not implemented; the
    limitation is documented so the gap is visible rather than papered over.

    Produces `MemoryType.LESSONS`.
    """

    rule_id = "rule:correction"
    produces = MemoryType.LESSONS

    _english_markers = (
        re.compile(r"\bno,?\s+actually\b", re.IGNORECASE),
        re.compile(r"\bthat'?s wrong\b", re.IGNORECASE),
        re.compile(r"\bcorrection:", re.IGNORECASE),
        re.compile(r"\bI meant\b", re.IGNORECASE),
        re.compile(r"\bactually,?\s+it'?s\b", re.IGNORECASE),
    )

    def propose(
        self, experience: ExperienceRecord
    ) -> Sequence[MemoryCandidate]:
        text = experience.text.strip()
        if not text:
            return ()
        if not any(p.search(text) for p in self._english_markers):
            return ()
        return (
            MemoryCandidate(
                experience_id=experience.id,
                type=self.produces,
                content=text,
                language=_language(experience),
                confidence=0.75,
                provenance=_provenance(experience, self.rule_id),
                rule=self.rule_id,
                rationale="correction marker detected (English)",
            ),
        )


class RepetitionRule:
    """Recognizes a claim the user has repeated at or above a threshold.

    Repetition is not measured here; the caller records how many times an
    experience's normalized text has appeared, on the `repetition_count`
    signal, and this rule promotes only when that count reaches
    `threshold`. Threshold defaults to 3 per MEMORY_ARCHITECTURE.md.

    Produces `MemoryType.SEMANTIC`.
    """

    rule_id = "rule:repetition"
    produces = MemoryType.SEMANTIC

    def __init__(self, *, threshold: int = 3) -> None:
        if threshold < 2:
            raise ValueError(f"repetition threshold must be >= 2, got {threshold}")
        self._threshold = threshold

    def propose(
        self, experience: ExperienceRecord
    ) -> Sequence[MemoryCandidate]:
        count = experience.signals.get("repetition_count")
        if not isinstance(count, int) or count < self._threshold:
            return ()
        text = experience.text.strip()
        if not text:
            return ()
        return (
            MemoryCandidate(
                experience_id=experience.id,
                type=self.produces,
                content=text,
                language=_language(experience),
                confidence=min(0.6 + 0.05 * count, 0.95),
                provenance=_provenance(experience, self.rule_id),
                rule=self.rule_id,
                rationale=f"repeated {count} times (threshold {self._threshold})",
            ),
        )


class InferenceRule:
    """Extracts a first-person factual claim as an episodic memory.

    Recognizes a limited set of self-report shapes: "my name is X",
    "I am X", "I live in X", "I work at X", plus their Arabic equivalents
    ("اسمي", "أعمل في", "أسكن في"). More elaborate inference is out of scope
    for the first pass; adding a shape here is a code change, not a config
    tweak, so the rule cannot silently drift.

    Produces `MemoryType.EPISODIC`.
    """

    rule_id = "rule:inference"
    produces = MemoryType.EPISODIC

    _english_markers = (
        re.compile(r"\bmy name is\b", re.IGNORECASE),
        re.compile(r"\bI (?:am|'m) (?:a |an )?\w+", re.IGNORECASE),
        re.compile(r"\bI live in\b", re.IGNORECASE),
        re.compile(r"\bI work (?:at|for)\b", re.IGNORECASE),
    )
    _arabic_markers = (
        re.compile(r"اسمي"),
        re.compile(r"أعمل في"),
        re.compile(r"أسكن في"),
    )

    def propose(
        self, experience: ExperienceRecord
    ) -> Sequence[MemoryCandidate]:
        text = experience.text.strip()
        if not text:
            return ()
        matched = any(p.search(text) for p in self._english_markers) or any(
            p.search(text) for p in self._arabic_markers
        )
        if not matched:
            return ()
        return (
            MemoryCandidate(
                experience_id=experience.id,
                type=self.produces,
                content=text,
                language=_language(experience),
                confidence=0.7,
                provenance=_provenance(experience, self.rule_id),
                rule=self.rule_id,
                rationale="first-person self-report detected",
            ),
        )


def default_rules() -> Sequence[ExtractionRule]:
    """The rule set used by the pipeline unless a caller overrides it."""
    return (
        ExplicitInstructionRule(),
        CorrectionRule(),
        RepetitionRule(),
        InferenceRule(),
    )
