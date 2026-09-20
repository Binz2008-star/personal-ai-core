"""HybridContextAssembler: two sources, one budget, no privilege.

The load-bearing test here is `test_document_rank_is_output_position`.
`RetrievalResult` has no `rank` field, and `provenance.ranks` holds
per-arm, *pre-fusion* ranks -- a result can be rank 1 in the lexical arm
and rank 8 in the vector arm. Collapsing those would invent a second
fusion policy alongside the real one, so the assembler uses the
retriever's output order, which is the interpretation the rest of the
system already relies on.
"""
from __future__ import annotations

from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.core.context import ContextBudget, ExclusionReason
from personal_ai_core.core.knowledge import (
    Chunk,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)
from personal_ai_core.core.memory import (
    MemoryEvidence,
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)


class WordEstimator:
    """One token per whitespace-separated word. Predictable arithmetic."""

    model_id = "word-count"

    def estimate(self, text: str) -> int:
        return len(text.split())


def chunk(text: str, *, chunk_id: str | None = None) -> Chunk:
    kwargs = {
        "document_id": "doc-1",
        "version_id": "ver-1",
        "text": text,
        "ordinal": 0,
        "start": 0,
        "end": len(text),
    }
    if chunk_id is not None:
        kwargs["id"] = chunk_id
    return Chunk(**kwargs)  # type: ignore[arg-type]


def result(c: Chunk, *, lexical_rank: int = 1) -> RetrievalResult:
    return RetrievalResult(
        chunk=c,
        provenance=RetrievalProvenance(
            document_id=c.document_id,
            version_id=c.version_id,
            chunk_id=c.id,
            start=c.start,
            end=c.end,
            methods=(RetrievalMethod.LEXICAL,),
            ranks={RetrievalMethod.LEXICAL: lexical_rank},
        ),
    )


def memory(content: str, *, relevance: float, memory_id: str | None = None) -> MemoryEvidence:
    kwargs = {
        "session_id": "s1",
        "type": MemoryType.PREFERENCES,
        "content": content,
        "language": "en",
        "provenance": MemoryProvenance(
            session_id="s1", event_id="e1", promoted_by="rule:test"
        ),
        "status": MemoryStatus.ACTIVE,
        "confidence": 0.9,
    }
    if memory_id is not None:
        kwargs["id"] = memory_id
    return MemoryEvidence(record=MemoryRecord(**kwargs), relevance=relevance)  # type: ignore[arg-type]


def assembler() -> HybridContextAssembler:
    return HybridContextAssembler(WordEstimator())


# --- rank derivation --------------------------------------------------------


def test_document_rank_is_output_position_not_provenance_ranks():
    """Output order decides the score; `provenance.ranks` is not consulted.

    The provenance ranks below deliberately disagree with sequence order.
    If the assembler ever started reading them, the selection order would
    invert and this fails.
    """
    first = result(chunk("alpha", chunk_id="c1"), lexical_rank=9)
    second = result(chunk("beta", chunk_id="c2"), lexical_rank=5)
    third = result(chunk("gamma", chunk_id="c3"), lexical_rank=1)

    context = assembler().assemble(
        results=[first, second, third],
        memories=[],
        budget=ContextBudget(available_tokens=100),
    )

    # Scored 1/1, 1/2, 1/3 by position -- so output order is preserved,
    # which is the opposite of what provenance.ranks would have produced.
    assert [r.chunk.id for r in context.document_context.selected] == ["c1", "c2", "c3"]


def test_retrieval_result_gained_no_rank_field():
    """Phase 4 must not have widened the Phase 2 type."""
    assert set(RetrievalResult.__dataclass_fields__) == {"chunk", "provenance"}


# --- no source is privileged ------------------------------------------------


def test_a_strong_document_outranks_a_weak_memory():
    context = assembler().assemble(
        results=[result(chunk("doc text", chunk_id="c1"))],      # score 1.0
        memories=[memory("weak memory", relevance=0.2, memory_id="m1")],
        budget=ContextBudget(available_tokens=2),                # room for one
    )
    assert [r.chunk.id for r in context.document_context.selected] == ["c1"]
    assert context.selected_memories == ()


def test_a_strong_memory_outranks_a_weak_document():
    # Four documents: the fourth scores 1/4 = 0.25, below the memory's 0.9.
    results = [result(chunk(f"doc {i}", chunk_id=f"c{i}")) for i in range(1, 5)]
    context = assembler().assemble(
        results=results,
        memories=[memory("strong memory", relevance=0.9, memory_id="m1")],
        budget=ContextBudget(available_tokens=2),                # room for one
    )
    # The first document scores 1.0 and wins the single slot; the memory at
    # 0.9 beats documents 2..4, proving neither source is privileged.
    assert [r.chunk.id for r in context.document_context.selected] == ["c1"]

    bigger = assembler().assemble(
        results=results,
        memories=[memory("strong memory", relevance=0.9, memory_id="m1")],
        budget=ContextBudget(available_tokens=4),                # room for two
    )
    assert [r.chunk.id for r in bigger.document_context.selected] == ["c1"]
    assert [e.record.id for e in bigger.selected_memories] == ["m1"]


# --- token accounting -------------------------------------------------------


def test_document_tokens_and_memory_tokens_are_tracked_separately():
    context = assembler().assemble(
        results=[result(chunk("one two three", chunk_id="c1"))],   # 3 tokens
        memories=[memory("four five", relevance=0.9, memory_id="m1")],  # 2 tokens
        budget=ContextBudget(available_tokens=100),
    )
    assert context.document_context.token_estimate == 3
    assert context.memory_token_estimate == 2
    assert context.token_estimate == 5


def test_the_budget_is_shared_not_per_source():
    """A memory that fits alone does not fit after documents took the room."""
    context = assembler().assemble(
        results=[result(chunk("one two three four", chunk_id="c1"))],  # 4 tokens
        memories=[memory("five six", relevance=0.5, memory_id="m1")],  # 2 tokens
        budget=ContextBudget(available_tokens=5),
    )
    assert context.document_context.token_estimate == 4
    assert context.selected_memories == ()
    assert len(context.excluded_memories) == 1
    assert context.excluded_memories[0].reason is ExclusionReason.BUDGET_EXHAUSTED
    assert context.excluded_memories[0].token_cost == 2


def test_a_candidate_that_does_not_fit_is_skipped_not_a_stopping_point():
    """Iteration continues so one oversized item does not discard the rest."""
    big = result(chunk("a b c d e f g h", chunk_id="big"))       # 8 tokens
    small = result(chunk("x", chunk_id="small"))                 # 1 token
    context = assembler().assemble(
        results=[big, small],
        memories=[],
        budget=ContextBudget(available_tokens=3),
    )
    assert [r.chunk.id for r in context.document_context.selected] == ["small"]
    assert [e.result.chunk.id for e in context.document_context.excluded] == ["big"]


def test_token_estimate_never_exceeds_the_budget():
    results = [result(chunk(" ".join(["w"] * n), chunk_id=f"c{n}")) for n in range(1, 8)]
    memories = [
        memory(" ".join(["m"] * n), relevance=0.5 + n / 100, memory_id=f"m{n}")
        for n in range(1, 8)
    ]
    for available in range(0, 40):
        budget = ContextBudget(available_tokens=available)
        context = assembler().assemble(
            results=results, memories=memories, budget=budget
        )
        assert context.token_estimate <= available
        assert context.within_budget


# --- duplicates -------------------------------------------------------------


def test_duplicate_documents_are_excluded():
    a = result(chunk("same words here", chunk_id="c1"))
    b = result(chunk("same  words   here", chunk_id="c2"))   # whitespace differs
    context = assembler().assemble(
        results=[a, b], memories=[], budget=ContextBudget(available_tokens=100)
    )
    assert len(context.document_context.selected) == 1
    assert context.document_context.excluded[0].reason is ExclusionReason.DUPLICATE


def test_duplicate_memories_are_excluded():
    context = assembler().assemble(
        results=[],
        memories=[
            memory("identical claim", relevance=0.9, memory_id="m1"),
            memory("identical  claim", relevance=0.8, memory_id="m2"),
        ],
        budget=ContextBudget(available_tokens=100),
    )
    assert len(context.selected_memories) == 1
    assert context.excluded_memories[0].reason is ExclusionReason.DUPLICATE


def test_a_document_and_a_memory_with_identical_text_are_both_kept():
    """They are different claims: one is what a source says, one is what
    the user told us. Deduplicating across sources would lose that."""
    context = assembler().assemble(
        results=[result(chunk("prefers Arabic", chunk_id="c1"))],
        memories=[memory("prefers Arabic", relevance=0.9, memory_id="m1")],
        budget=ContextBudget(available_tokens=100),
    )
    assert len(context.document_context.selected) == 1
    assert len(context.selected_memories) == 1


# --- shape ------------------------------------------------------------------


def test_empty_inputs_produce_an_empty_context():
    context = assembler().assemble(
        results=[], memories=[], budget=ContextBudget(available_tokens=10)
    )
    assert context.document_context.selected == ()
    assert context.selected_memories == ()
    assert context.token_estimate == 0
    assert context.memories_used == 0
    assert context.memories_dropped == 0


def test_the_shared_budget_is_reachable_from_the_hybrid_context():
    budget = ContextBudget(available_tokens=42, source="test")
    context = assembler().assemble(results=[], memories=[], budget=budget)
    assert context.budget is budget
