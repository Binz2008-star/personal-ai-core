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
    ExcludedMemory,
    ExcludedResult,
    ExclusionReason,
    HybridBudgetedContext,
)
from ..core.knowledge import RetrievalResult
from ..core.memory import MemoryEvidence


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


class HybridContextAssembler:
    """Fits documents and recalled memories into one shared budget.

    No source is given precedence *by rule*: both stream into one sort and
    spend from one cumulative counter, and nothing checks the source type
    when deciding what fits.

    The two scores are not, however, on a calibrated common scale, and the
    earlier wording here claimed they were. A document at output position
    `r` scores `1/r`; a memory carries its ranker's relevance. So rank 1
    scores 1.0 and always wins a contested slot, while rank 2 scores 0.5
    and loses to any memory above it -- and `SimpleMemoryRetriever`
    routinely produces 0.5-0.85. Under a tight budget, memories can
    therefore displace documents ranked 2 and below.

    That behaviour is defensible: a stated preference often should outrank
    the fifth-best passage. It is written down because it is a real ranking
    consequence of the chosen scales, not a neutral merge -- and a
    docstring claiming neutrality would have hidden it. Calibrating the two
    scales is a policy decision, deliberately not taken here.

    Document rank is the result's **1-based position in the retriever's
    output**, which is the interpretation the rest of the system already
    uses -- `RankFusion.fuse` returns ids best first, and
    `GreedyContextAssembler` treats input order as authoritative.
    `RetrievalProvenance.ranks` is deliberately not consulted: it holds
    per-arm, pre-fusion ranks, and collapsing them here would invent a
    second fusion policy alongside the real one.

    Duplicate detection is within a source, not across. A passage and a
    memory with identical text are two different claims -- one is what a
    document says, the other is what the user told us -- and dropping
    either as a duplicate of the other would lose that distinction.
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def assemble(
        self,
        *,
        results: Sequence[RetrievalResult],
        memories: Sequence[MemoryEvidence],
        budget: ContextBudget,
    ) -> HybridBudgetedContext:
        candidates: list[tuple[float, str, str, RetrievalResult | MemoryEvidence]] = []

        for rank, result in enumerate(results, start=1):
            candidates.append((1.0 / rank, "doc", result.chunk.id, result))
        for evidence in memories:
            candidates.append(
                (evidence.relevance, "memory", evidence.record.id, evidence)
            )

        # Total order. `source_type` is an alphabetic tiebreaker only --
        # "doc" sorts before "memory" at identical scores and that is all it
        # means. It is not a precedence rule.
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

        doc_selected: list[RetrievalResult] = []
        doc_excluded: list[ExcludedResult] = []
        mem_selected: list[MemoryEvidence] = []
        mem_excluded: list[ExcludedMemory] = []
        seen_chunk_ids: set[str] = set()
        seen_doc_text: set[str] = set()
        seen_memory_ids: set[str] = set()
        seen_memory_text: set[str] = set()
        doc_tokens = 0
        memory_tokens = 0
        used = 0

        for _score, _source_type, _identity, candidate in candidates:
            # Dispatch on the candidate's own type rather than the sort tag:
            # the tag exists to order the merge, and using it to decide
            # behaviour would make two things depend on one string.
            if isinstance(candidate, RetrievalResult):
                result = candidate
                fingerprint = _fingerprint(result.chunk.text)
                if result.chunk.id in seen_chunk_ids or fingerprint in seen_doc_text:
                    doc_excluded.append(
                        ExcludedResult(result=result, reason=ExclusionReason.DUPLICATE)
                    )
                    continue
                cost = self._estimator.estimate(result.chunk.text)
                if used + cost > budget.available_tokens:
                    doc_excluded.append(
                        ExcludedResult(
                            result=result,
                            reason=ExclusionReason.BUDGET_EXHAUSTED,
                            token_cost=cost,
                        )
                    )
                    continue
                doc_selected.append(result)
                seen_chunk_ids.add(result.chunk.id)
                seen_doc_text.add(fingerprint)
                doc_tokens += cost
                used += cost
            else:
                evidence = candidate
                fingerprint = _fingerprint(evidence.record.content)
                if (
                    evidence.record.id in seen_memory_ids
                    or fingerprint in seen_memory_text
                ):
                    mem_excluded.append(
                        ExcludedMemory(
                            evidence=evidence, reason=ExclusionReason.DUPLICATE
                        )
                    )
                    continue
                cost = self._estimator.estimate(evidence.record.content)
                if used + cost > budget.available_tokens:
                    mem_excluded.append(
                        ExcludedMemory(
                            evidence=evidence,
                            reason=ExclusionReason.BUDGET_EXHAUSTED,
                            token_cost=cost,
                        )
                    )
                    continue
                mem_selected.append(evidence)
                seen_memory_ids.add(evidence.record.id)
                seen_memory_text.add(fingerprint)
                memory_tokens += cost
                used += cost

        return HybridBudgetedContext(
            document_context=BudgetedContext(
                selected=tuple(doc_selected),
                excluded=tuple(doc_excluded),
                token_estimate=doc_tokens,
                budget=budget,
            ),
            selected_memories=tuple(mem_selected),
            excluded_memories=tuple(mem_excluded),
            memory_token_estimate=memory_tokens,
        )
