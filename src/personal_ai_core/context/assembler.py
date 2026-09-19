"""Context assembly.

Implements `core.contracts.ContextAssembler`.

Takes ranked retrieval results and a budget, and produces the subset that fits
**together with a record of everything that did not and why**. The exclusions
are not diagnostics bolted on afterwards; they are half the output. A context
assembler that returns only what it kept makes a bad answer unexplainable:
there is no way to tell "the evidence was not retrieved" from "the evidence was
retrieved and then dropped for four hundred tokens".
"""
from __future__ import annotations

from typing import Sequence

from ..core.contracts import TokenEstimator
from ..core.context import (
    BudgetedContext,
    ContextBudget,
    ExcludedResult,
    ExclusionReason,
)
from ..core.knowledge import RetrievalResult


def _fingerprint(text: str) -> str:
    """Whitespace-insensitive identity, for duplicate detection.

    Overlapping chunks and re-ingested documents routinely produce the same
    passage twice. Paying for it twice is the same as halving the budget.
    """
    return " ".join(text.split())


class GreedyContextAssembler:
    """Fills the budget in rank order, skipping what will not fit.

    Two decisions worth stating:

    *Rank order is respected, not re-optimized.* Packing the budget optimally
    would mean preferring short passages over relevant ones. Retrieval already
    ranked these; reordering here would silently overrule it.

    *A result that does not fit is skipped, not a stopping point.* Scanning
    continues, so one oversized passage does not discard every shorter one
    behind it. The order of what is selected still follows the input ranking.
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def assemble(
        self, results: Sequence[RetrievalResult], *, budget: ContextBudget
    ) -> BudgetedContext:
        selected: list[RetrievalResult] = []
        excluded: list[ExcludedResult] = []
        seen_chunks: set[str] = set()
        seen_text: set[str] = set()
        used = 0

        for result in results:
            fingerprint = _fingerprint(result.chunk.text)
            if result.chunk.id in seen_chunks or fingerprint in seen_text:
                excluded.append(
                    ExcludedResult(result=result, reason=ExclusionReason.DUPLICATE)
                )
                continue

            cost = self._estimator.estimate(result.chunk.text)
            if used + cost > budget.available_tokens:
                excluded.append(
                    ExcludedResult(
                        result=result,
                        reason=ExclusionReason.BUDGET_EXHAUSTED,
                        token_cost=cost,
                    )
                )
                continue

            selected.append(result)
            seen_chunks.add(result.chunk.id)
            seen_text.add(fingerprint)
            used += cost

        return BudgetedContext(
            selected=tuple(selected),
            excluded=tuple(excluded),
            token_estimate=used,
            budget=budget,
        )
