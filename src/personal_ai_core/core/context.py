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


class ExclusionReason(str, Enum):
    """Why a retrieved result did not reach the context.

    Recorded per result. A budget that drops evidence without saying which or
    why is indistinguishable from one that is working correctly.
    """

    BUDGET_EXHAUSTED = "budget_exhausted"
    DUPLICATE = "duplicate"
    BELOW_THRESHOLD = "below_threshold"
    LANGUAGE_MISMATCH = "language_mismatch"
    POLICY = "policy"


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
