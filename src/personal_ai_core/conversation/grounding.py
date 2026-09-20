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
    ContextBudgetPolicy,
    HybridContextAssembler,
    MemoryRetriever,
    ModelSpecLike,
    Retriever,
    TokenEstimator,
)
from ..core.context import (
    BudgetedContext,
    ContextAllocation,
    HybridBudgetedContext,
)
from ..core.domain import UNDETERMINED_LANGUAGE, Message, Role
from ..core.knowledge import RetrievalQuery, RetrievalResult
from ..core.memory import (
    MemoryEvidence,
    MemoryQuery,
    MemoryRetrievalError,
)

GROUNDING_PREAMBLE = (
    "The following passages were retrieved from the user's own documents for "
    "this question. Each is labelled with the source it came from and the "
    "character range it occupies in that source, so any claim drawn from it "
    "can be checked.\n"
    "Use them where they are relevant. Where they do not answer the question, "
    "say so rather than filling the gap."
)

MEMORY_PREAMBLE = (
    "The following are things this system previously recorded about the user, "
    "each with the rule that promoted it and when. They are recollections, not "
    "retrieved sources: treat them as the user's own stated context, and defer "
    "to anything they say now that contradicts one."
)


@dataclass(frozen=True, slots=True)
class Grounding:
    """What grounding produced for one turn.

    Carries the accounting, not only the message: the allocation that set the
    budget, and the assembled context including everything excluded. That is
    what makes `CONTEXT_ASSEMBLED` an audit record rather than a claim.

    `memory_enabled` records whether a recall path was wired, which is a
    different fact from whether it returned anything. Inferring it from
    `memories_retrieved` would make "recall is off" and "recall found
    nothing" indistinguishable, and those two need different answers when
    someone asks why an answer lacked context.
    """

    message: Message | None
    context: HybridBudgetedContext
    allocation: ContextAllocation
    retrieved: int
    memory_enabled: bool = False
    memories_retrieved: int = 0
    memory_error: MemoryRetrievalError | None = None

    @property
    def used(self) -> int:
        return len(self.context.document_context.selected)

    @property
    def dropped(self) -> int:
        return self.context.document_context.dropped_count

    @property
    def memories_used(self) -> int:
        return self.context.memories_used

    @property
    def memories_dropped(self) -> int:
        return self.context.memories_dropped


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


def render_memories(memories: Sequence[MemoryEvidence]) -> str:
    """Render recalled memories with the rule and time that produced them.

    The promoting rule and timestamp travel with each line for the same
    reason a document citation carries its source: a recollection nobody
    can trace back to when and why it was recorded is an assertion, not
    evidence.
    """
    lines = []
    for evidence in memories:
        record = evidence.record
        promoted_at = record.provenance.promoted_at.isoformat()
        lines.append(
            f"- {record.content} "
            f"(recorded by {record.provenance.promoted_by} at {promoted_at})"
        )
    return "\n".join(lines)


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
        assembler: HybridContextAssembler,
        budget_policy: ContextBudgetPolicy,
        estimator: TokenEstimator,
        memory_retriever: MemoryRetriever | None = None,
        limit: int = 5,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._retriever = retriever
        self._assembler = assembler
        self._budget_policy = budget_policy
        self._estimator = estimator
        self._memory_retriever = memory_retriever
        self._limit = limit

    @property
    def memory_enabled(self) -> bool:
        """Whether a recall path is wired. Configuration, not outcome."""
        return self._memory_retriever is not None

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
                context=HybridBudgetedContext(
                    document_context=BudgetedContext(budget=budget)
                ),
                allocation=allocation,
                retrieved=0,
                memory_enabled=self.memory_enabled,
                memories_retrieved=0,
                memory_error=None,
            )

        results = self._retriever.retrieve(
            RetrievalQuery(text=query, limit=self._limit, language=language)
        )
        memories, memory_error = self._recall(
            session_id=session_id, query=query, language=language
        )
        context = self._assembler.assemble(
            results=results, memories=memories, budget=budget
        )

        message = None
        if context.document_context.selected or context.selected_memories:
            # Carries the real session id so it is coherent with the turn it
            # grounds -- but it is never handed to the message repository.
            # See this module's docstring.
            sections = []
            if context.selected_memories:
                sections.append(
                    f"{MEMORY_PREAMBLE}\n\n"
                    f"{render_memories(context.selected_memories)}"
                )
            if context.document_context.selected:
                sections.append(
                    f"{GROUNDING_PREAMBLE}\n\n"
                    f"{render_evidence(context.document_context.selected)}"
                )
            message = Message(
                session_id=session_id,
                role=Role.SYSTEM,
                content="\n\n".join(sections),
                language=UNDETERMINED_LANGUAGE,
            )

        return Grounding(
            message=message,
            context=context,
            allocation=allocation,
            retrieved=len(results),
            memory_enabled=self.memory_enabled,
            memories_retrieved=len(memories),
            memory_error=memory_error,
        )

    def _recall(
        self, *, session_id: str, query: str, language: str
    ) -> tuple[Sequence[MemoryEvidence], MemoryRetrievalError | None]:
        """Recall memories for this turn, degrading rather than failing.

        Recall is enrichment: it adds what the system already knew about the
        user, where document retrieval answers what the user just asked. A
        turn that cannot reach memory is worse than one that can, but it is
        still a turn the user is entitled to -- so the failure is classified
        and recorded rather than raised.

        That is deliberately asymmetric with document retrieval, which does
        raise. An ungrounded answer the caller believes is grounded is the
        failure the grounding layer exists to prevent; a memory-less answer
        is visibly memory-less in the event payload.
        """
        if self._memory_retriever is None:
            return (), None

        # Imported here rather than at module scope: the exception type lives
        # in the memory layer, and this layer may only depend on core.
        try:
            memories = self._memory_retriever.retrieve(
                MemoryQuery(
                    session_id=session_id,
                    text=query,
                    language=language,
                    limit=self._limit,
                )
            )
            return memories, None
        except Exception as exc:  # noqa: BLE001 -- classified below, never re-raised
            classification = getattr(exc, "classification", None)
            if isinstance(classification, MemoryRetrievalError):
                return (), classification
            # A third-party retriever that raised something outside this
            # contract. Classified, never inspected for a message.
            return (), MemoryRetrievalError.INTERNAL

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
    context = grounding.context
    excluded: dict[str, int] = {}
    for item in context.document_context.excluded:
        excluded[item.reason.value] = excluded.get(item.reason.value, 0) + 1
    # Distinct rather than "the first one": if two results ever disagree about
    # which embedder ranked them, the audit trail must show that rather than
    # pick one and look consistent.
    embedders = sorted(
        {
            r.provenance.embedding_model_id
            for r in context.document_context.selected
            if r.provenance.embedding_model_id is not None
        }
    )
    return {
        "retrieved": grounding.retrieved,
        "used": grounding.used,
        "embedding_model_ids": embedders,
        "dropped": grounding.dropped,
        "excluded_by_reason": excluded,
        # `evidence_tokens` keeps its Phase 2 meaning -- documents only.
        # Memory is reported alongside rather than folded in, so the two
        # halves of a shared budget stay separately answerable.
        "evidence_tokens": context.document_context.token_estimate,
        "memory_tokens": context.memory_token_estimate,
        "budget_tokens": context.budget.available_tokens,
        "budget_source": context.budget.source,
        "context_window": allocation.context_window,
        "history_tokens": allocation.history,
        "generation_reserve": allocation.generation_reserve,
        "overhead": allocation.overhead,
        "overcommitted": allocation.overcommitted,
        "chunk_ids": [r.chunk.id for r in context.document_context.selected],
        # Recall accounting. `memory_enabled` is wiring, not outcome: it
        # stays True when recall was configured and returned nothing, so
        # "recall is off" and "recall found nothing" remain distinguishable.
        "memory_enabled": grounding.memory_enabled,
        "memories_retrieved": grounding.memories_retrieved,
        "memories_used": grounding.memories_used,
        "memories_dropped": grounding.memories_dropped,
        # A stable classification or null. Never an exception message: this
        # payload is logged, and a store's error text can name a host, a
        # database or a credential.
        "memory_error": (
            grounding.memory_error.value
            if grounding.memory_error is not None
            else None
        ),
        "memory_ids": [e.record.id for e in context.selected_memories],
    }
