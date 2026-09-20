"""The promotion pipeline.

This module is the **sole writer** to `MemoryStore` in the codebase. A
guard test asserts that no other `src/` module contains a call to
`.write(` bound to a store. If a second caller appeared, the invariant
`ExperiencePipeline is the sole MemoryStore writer` would be quietly gone;
the guard exists so it cannot be.

The pipeline is not wired into `ConversationService`. It is called by
whoever is running the memory subsystem, with its own `EventRepository`
and `MemoryStore`. That separation is the structural side of
`Event != Memory`: the conversation path cannot reach memory because there
is no wire.
"""
from __future__ import annotations

from typing import Sequence

from ..core.contracts import EventRepository, MemoryStore, PromotionGate
from ..core.domain import Event, EventType
from ..core.memory import (
    ExperienceRecord,
    MemoryRecord,
    MemoryStatus,
    PromotionDecision,
    PromotionOutcome,
)
from .rules import ExtractionRule, default_rules


class ExperiencePipeline:
    """Turns experiences into memory decisions.

    For each experience the pipeline:

    1. Asks every rule to propose candidates.
    2. Asks the gate to decide on each candidate against the current
       active set.
    3. Writes a record for PROMOTED and REJECTED decisions; a HELD
       decision writes no record.
    4. Emits exactly one `MEMORY_*` event per decision.
    """

    def __init__(
        self,
        *,
        rules: Sequence[ExtractionRule] | None = None,
        gate: PromotionGate,
        store: MemoryStore,
        events: EventRepository,
    ) -> None:
        self._rules = tuple(rules) if rules is not None else tuple(default_rules())
        self._gate = gate
        self._store = store
        self._events = events

    def ingest(self, experience: ExperienceRecord) -> Sequence[PromotionOutcome]:
        outcomes: list[PromotionOutcome] = []
        for rule in self._rules:
            for candidate in rule.propose(experience):
                active = self._store.list_active()
                outcome = self._gate.evaluate(candidate, active)

                if outcome.decision is PromotionDecision.PROMOTED:
                    record = MemoryRecord(
                        session_id=experience.session_id,
                        type=candidate.type,
                        content=candidate.content,
                        language=candidate.language,
                        provenance=candidate.provenance,
                        status=MemoryStatus.ACTIVE,
                        confidence=candidate.confidence,
                    )
                    written = self._store.write(record)
                    self._emit(
                        experience,
                        EventType.MEMORY_PROMOTED,
                        outcome=outcome,
                        memory_id=written.id,
                        candidate=candidate,
                    )
                    outcomes.append(
                        PromotionOutcome(
                            decision=outcome.decision,
                            candidate_id=outcome.candidate_id,
                            reason=outcome.reason,
                            conflicts_with=outcome.conflicts_with,
                            memory_id=written.id,
                        )
                    )
                elif outcome.decision is PromotionDecision.REJECTED:
                    record = MemoryRecord(
                        session_id=experience.session_id,
                        type=candidate.type,
                        content=candidate.content,
                        language=candidate.language,
                        provenance=candidate.provenance,
                        status=MemoryStatus.REJECTED,
                        confidence=candidate.confidence,
                    )
                    written = self._store.write(record)
                    self._emit(
                        experience,
                        EventType.MEMORY_REJECTED,
                        outcome=outcome,
                        memory_id=written.id,
                        candidate=candidate,
                    )
                    outcomes.append(
                        PromotionOutcome(
                            decision=outcome.decision,
                            candidate_id=outcome.candidate_id,
                            reason=outcome.reason,
                            conflicts_with=outcome.conflicts_with,
                            memory_id=written.id,
                        )
                    )
                else:  # PromotionDecision.HELD
                    self._emit(
                        experience,
                        EventType.MEMORY_CONFLICT_DETECTED,
                        outcome=outcome,
                        memory_id=None,
                        candidate=candidate,
                    )
                    outcomes.append(outcome)
        return tuple(outcomes)

    def _emit(
        self,
        experience: ExperienceRecord,
        event_type: EventType,
        *,
        outcome: PromotionOutcome,
        memory_id: str | None,
        candidate,
    ) -> None:
        payload: dict = {
            "candidate_id": outcome.candidate_id,
            "experience_id": experience.id,
            "rule": candidate.rule,
            "memory_type": candidate.type.value,
            "reason": outcome.reason,
        }
        if memory_id is not None:
            payload["memory_id"] = memory_id
        if outcome.conflicts_with is not None:
            payload["conflicts_with"] = outcome.conflicts_with
        self._events.append(
            Event(
                session_id=experience.session_id,
                type=event_type,
                payload=payload,
                message_id=experience.message_id,
                actor="memory_pipeline",
            )
        )
