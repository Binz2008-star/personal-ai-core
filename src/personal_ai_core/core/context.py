"""Context budget types.

Types only. No estimator, no compression, no assembly.

**A context budget is not a model's context window.** The Boss model's 8192 is
a property of the model, recorded in `ModelRegistry`. A budget is what this
system chooses to spend on retrieved evidence, after identity, conversation
history, tool observations and the generation reserve have taken their share.
Deriving one directly from the other is the mistake ADR-005 records: the
audited source hard-coded 24000 for a 32K model, and a budget that silently
exceeds what fits fails as truncation — the quietest failure there is.

A budget is therefore constructed explicitly, never inferred here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .knowledge import RetrievalResult
from .memory import MemoryEvidence


class ExclusionReason(str, Enum):
    """Why a retrieved result did not reach the context.

    Recorded per result. A budget that drops evidence without saying which or
    why is indistinguishable from one that is working correctly.
    """

    BUDGET_EXHAUSTED = "budget_exhausted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """How many tokens retrieved evidence may occupy.

    Explicit by construction. `source` records where the number came from --
    a model registry entry, a policy, or a caller -- so a budget can never be
    traced back to "someone hard-coded it".
    """

    available_tokens: int
    source: str = "explicit"

    def __post_init__(self) -> None:
        if self.available_tokens < 0:
            raise ValueError(
                f"budget must be non-negative, got {self.available_tokens}"
            )


@dataclass(frozen=True, slots=True)
class ContextAllocation:
    """How one model's context window was divided for a single turn.

    Every share is recorded, not just the answer. ADR-005's failure was a
    budget nobody could trace: the audited source hard-coded 24000 for a 32K
    model, and because only the final number existed there was nothing to
    check it against.

    The arithmetic is deliberately visible and deliberately conservative --
    `evidence` is whatever survives after the other shares are taken, floored
    at zero. A budget is never allowed to go negative and silently wrap into
    "plenty of room".

    `identity` is ADR-011 prerequisite A. ADR-005's Decision apportions the
    window "across identity, conversation, memory, knowledge, tool
    observations and generation", and this module's own docstring says the
    same -- while the field did not exist. The share was promised in two
    places and implemented in neither, so identity tokens would have come out
    of `overhead` or silently shrunk `evidence`: a budget nobody could trace,
    which is the failure ADR-005 exists to prevent.

    It defaults to zero because `identity/` is not built. A default invented
    for a component that does not exist would shrink `evidence` today for no
    benefit, and the number would be fabricated. Zero is the honest value
    until something supplies a real one -- but the share is settable now, so
    it is a real field rather than a placeholder.
    """

    context_window: int
    generation_reserve: int
    overhead: int
    history: int
    identity: int = 0

    SHARES = ("generation_reserve", "overhead", "history", "identity")

    def __post_init__(self) -> None:
        for name in ("context_window", *self.SHARES):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def spoken_for(self) -> int:
        return sum(getattr(self, name) for name in self.SHARES)

    @property
    def evidence(self) -> int:
        """Tokens left for retrieved evidence. Never negative."""
        return max(0, self.context_window - self.spoken_for)

    @property
    def overcommitted(self) -> bool:
        """The turn already exceeds the window before any evidence is added.

        Not an error here -- the caller decides -- but it must be visible.
        A long enough conversation reaches this, and reaching it silently is
        how a model starts truncating history nobody asked it to drop.
        """
        return self.spoken_for > self.context_window

    def budget(self, *, source: str) -> "ContextBudget":
        return ContextBudget(available_tokens=self.evidence, source=source)


@dataclass(frozen=True, slots=True)
class ExcludedResult:
    """A result that was retrieved but not included, and why."""

    result: RetrievalResult
    reason: ExclusionReason
    token_cost: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetedContext:
    """The outcome of fitting retrieval results into a budget.

    Carries what was excluded, not only what was kept. The exclusions are the
    part that explains a bad answer.
    """

    selected: tuple[RetrievalResult, ...] = ()
    excluded: tuple[ExcludedResult, ...] = ()
    token_estimate: int = 0
    budget: ContextBudget = field(default_factory=lambda: ContextBudget(0))

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected", tuple(self.selected))
        object.__setattr__(self, "excluded", tuple(self.excluded))
        if self.token_estimate < 0:
            raise ValueError("token_estimate must be non-negative")

    @property
    def within_budget(self) -> bool:
        return self.token_estimate <= self.budget.available_tokens

    @property
    def dropped_count(self) -> int:
        return len(self.excluded)


# --- Phase 4: documents and memories under one budget ----------------------


@dataclass(frozen=True, slots=True)
class ExcludedMemory:
    """A recalled memory that was not included, and why.

    Reuses `ExclusionReason`. A memory is dropped for the same two reasons
    a passage is -- the budget ran out, or it duplicates something already
    selected -- and inventing parallel reasons would make the two halves of
    one context report the same event under different names.
    """

    evidence: MemoryEvidence
    reason: ExclusionReason
    token_cost: int | None = None


@dataclass(frozen=True, slots=True)
class HybridBudgetedContext:
    """Documents and memories fitted into one shared budget.

    Composition, not inheritance. `document_context` is an ordinary
    `BudgetedContext` and still means exactly what it meant in Phase 2:
    which passages were selected, which were dropped, and how many tokens
    the passages cost. Subclassing it would have quietly redefined
    `token_estimate` for every existing reader of that type.

    The two halves are tracked separately and summed here, so
    "how much did evidence cost" and "how much did memory cost" remain
    separately answerable after the fact.
    """

    document_context: BudgetedContext
    selected_memories: tuple[MemoryEvidence, ...] = ()
    excluded_memories: tuple[ExcludedMemory, ...] = ()
    memory_token_estimate: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_memories", tuple(self.selected_memories))
        object.__setattr__(self, "excluded_memories", tuple(self.excluded_memories))
        if self.memory_token_estimate < 0:
            raise ValueError("memory_token_estimate must be non-negative")

    @property
    def token_estimate(self) -> int:
        """Tokens spent across both sources.

        Equal by construction to the assembler's cumulative counter: both
        halves drew from one budget, so the total is what that budget saw.
        """
        return self.document_context.token_estimate + self.memory_token_estimate

    @property
    def budget(self) -> ContextBudget:
        """The one budget both sources competed for."""
        return self.document_context.budget

    @property
    def within_budget(self) -> bool:
        return self.token_estimate <= self.budget.available_tokens

    @property
    def memories_used(self) -> int:
        return len(self.selected_memories)

    @property
    def memories_dropped(self) -> int:
        return len(self.excluded_memories)
