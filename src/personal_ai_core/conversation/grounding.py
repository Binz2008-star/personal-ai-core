"""Turning retrieved evidence into one turn's grounding.

Application layer. Every collaborator arrives as a protocol from
`core.contracts`; nothing here knows an index, an embedding model or a store
exists.

Two decisions shape this module.

**The grounding message is never persisted.** It is derived from the query and
the index at one moment, and it is rebuilt on the next turn. Storing it would
make the conversation history un-reproducible and would charge the budget for
the same evidence again on every subsequent turn.

**No evidence means no message.** When retrieval finds nothing, or the budget
admits nothing, `Grounding.message` is `None` and the model sees the
conversation exactly as it would have without retrieval. An empty evidence
block is worse than none: it invites an answer that claims to have consulted
sources it never received.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..core.contracts import (
    ContextAssembler,
    ContextBudgetPolicy,
    ModelSpecLike,
    Retriever,
    TokenEstimator,
)
from ..core.context import BudgetedContext, ContextAllocation, ContextBudget
from ..core.domain import UNDETERMINED_LANGUAGE, Message, Role
from ..core.knowledge import RetrievalQuery, RetrievalResult

GROUNDING_PREAMBLE = (
    "The following passages were retrieved from the user's own documents for "
    "this question. Each is labelled with the source it came from and the "
    "character range it occupies in that source, so any claim drawn from it "
    "can be checked.\n"
    "Use them where they are relevant. Where they do not answer the question, "
    "say so rather than filling the gap."
)


@dataclass(frozen=True, slots=True)
class Grounding:
    """What grounding produced for one turn.

    Carries the accounting, not only the message: the allocation that set the
    budget, and the assembled context including everything excluded. That is
    what makes `CONTEXT_ASSEMBLED` an audit record rather than a claim.
    """

    message: Message | None
    context: BudgetedContext
    allocation: ContextAllocation
    retrieved: int

    @property
    def used(self) -> int:
        return len(self.context.selected)

    @property
    def dropped(self) -> int:
        return self.context.dropped_count


def render_evidence(results: Sequence[RetrievalResult]) -> str:
    """Render selected results deterministically, with checkable citations.

    The source URI and character range travel with each passage on purpose. A
    citation the reader cannot resolve back to a span of a named document is
    not a citation.
    """
    blocks = []
    for position, result in enumerate(results, start=1):
        provenance = result.provenance
        source = provenance.source_uri or provenance.document_id
        blocks.append(
            f"[{position}] {source} (characters {provenance.start}-{provenance.end})\n"
            f"{result.chunk.text}"
        )
    return "\n\n".join(blocks)


class ContextBuilder:
    """Retrieval, budgeting and assembly for a single turn.

    One collaborator rather than four on `ConversationService`, because the
    four only ever act together and the order they act in is itself a decision:
    measure the history, derive the budget from the *active* model, retrieve,
    then fit. A caller free to reorder those would eventually derive a budget
    from a stale model or fit before measuring.
    """

    def __init__(
        self,
        *,
        retriever: Retriever,
        assembler: ContextAssembler,
        budget_policy: ContextBudgetPolicy,
        estimator: TokenEstimator,
        limit: int = 5,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._retriever = retriever
        self._assembler = assembler
        self._budget_policy = budget_policy
        self._estimator = estimator
        self._limit = limit

    def build(
        self,
        *,
        session_id: str,
        query: str,
        language: str,
        model: ModelSpecLike,
        history: Sequence[Message],
    ) -> Grounding:
        """Ground one turn. Raises whatever retrieval raises.

        Retrieval failure is **not** swallowed. An answer produced without the
        evidence it was supposed to use, by a caller who believes the evidence
        was used, is the exact failure this layer exists to prevent. The
        service records the failure as an event and lets it propagate, so the
        caller can decide whether to retry ungrounded.
        """
        history_tokens = sum(
            self._estimator.estimate(message.content) for message in history
        )
        allocation = self._budget_policy.allocate(
            model=model, history_tokens=history_tokens
        )
        budget = allocation.budget(source=self._describe_source())

        if not query.strip():
            # Nothing to retrieve for. Not an error, and not worth a round trip.
            return Grounding(
                message=None,
                context=BudgetedContext(budget=budget),
                allocation=allocation,
                retrieved=0,
            )

        results = self._retriever.retrieve(
            RetrievalQuery(text=query, limit=self._limit, language=language)
        )
        context = self._assembler.assemble(results, budget=budget)

        message = None
        if context.selected:
            # Carries the real session id so it is coherent with the turn it
            # grounds -- but it is never handed to the message repository.
            # See this module's docstring.
            message = Message(
                session_id=session_id,
                role=Role.SYSTEM,
                content=f"{GROUNDING_PREAMBLE}\n\n{render_evidence(context.selected)}",
                language=UNDETERMINED_LANGUAGE,
            )

        return Grounding(
            message=message,
            context=context,
            allocation=allocation,
            retrieved=len(results),
        )

    def _describe_source(self) -> str:
        source = getattr(self._budget_policy, "source", None)
        return source if isinstance(source, str) else type(self._budget_policy).__name__


def summarize(grounding: Grounding) -> dict:
    """The `CONTEXT_ASSEMBLED` event payload.

    Includes the exclusions and their reasons. The exclusions are the part
    that explains a bad answer; an event recording only what was kept cannot
    distinguish "never retrieved" from "retrieved and dropped".
    """
    allocation = grounding.allocation
    excluded: dict[str, int] = {}
    for item in grounding.context.excluded:
        excluded[item.reason.value] = excluded.get(item.reason.value, 0) + 1
    # Distinct rather than "the first one": if two results ever disagree about
    # which embedder ranked them, the audit trail must show that rather than
    # pick one and look consistent.
    embedders = sorted(
        {
            r.provenance.embedding_model_id
            for r in grounding.context.selected
            if r.provenance.embedding_model_id is not None
        }
    )
    return {
        "retrieved": grounding.retrieved,
        "used": grounding.used,
        "embedding_model_ids": embedders,
        "dropped": grounding.dropped,
        "excluded_by_reason": excluded,
        "evidence_tokens": grounding.context.token_estimate,
        "budget_tokens": grounding.context.budget.available_tokens,
        "budget_source": grounding.context.budget.source,
        "context_window": allocation.context_window,
        "history_tokens": allocation.history,
        "generation_reserve": allocation.generation_reserve,
        "overhead": allocation.overhead,
        "overcommitted": allocation.overcommitted,
        "chunk_ids": [r.chunk.id for r in grounding.context.selected],
    }
