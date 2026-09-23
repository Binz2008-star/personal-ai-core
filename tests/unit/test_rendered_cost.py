"""Evidence is charged what it costs once rendered -- Finding F-4.

The assembler used to charge each passage its bare text. The renderer then
wrapped every passage in boundary lines and put a preamble in front of each
section, and #49 made those large: five passages plus both preambles came to
about 483 estimated tokens against a 256-token overhead reserve that was
silently expected to hold them.

Two groups of tests. The first pins that `RenderedEvidenceCost` never charges
less than the renderer spends -- in English, in Arabic, and past position 9,
where positions gain a digit. The second pins how the assembler spends it.
"""
from __future__ import annotations

from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator
from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.core.context import ContextBudget, ExclusionReason
from personal_ai_core.conversation.grounding import (
    GROUNDING_PREAMBLE,
    ContextBuilder,
    MEMORY_PREAMBLE,
    RenderedEvidenceCost,
    render_evidence,
    render_memories,
)
from personal_ai_core.core.contracts import RenderedCost
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

ESTIMATOR = ScriptAwareTokenEstimator()


def passage(text: str, *, chunk_id: str, source: str = "file:///notes/a.md"):
    chunk = Chunk(
        document_id="d1",
        version_id="v1",
        text=text,
        ordinal=0,
        start=0,
        end=len(text),
        id=chunk_id,
    )
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id="d1",
            version_id="v1",
            chunk_id=chunk_id,
            start=0,
            end=len(text),
            methods=(RetrievalMethod.LEXICAL,),
            source_uri=source,
        ),
    )


def recollection(content: str, *, memory_id: str, relevance: float = 0.9):
    return MemoryEvidence(
        record=MemoryRecord(
            session_id="s1",
            type=MemoryType.PREFERENCES,
            content=content,
            language="en",
            provenance=MemoryProvenance(
                session_id="s1", event_id="e1", promoted_by="rule:explicit_instruction"
            ),
            status=MemoryStatus.ACTIVE,
            confidence=0.9,
            id=memory_id,
        ),
        relevance=relevance,
    )


ENGLISH = [passage(f"Passage {i} about rank fusion.", chunk_id=f"c{i}") for i in range(12)]
ARABIC = [
    passage(f"المقطع {i} عن دمج الرتب المتبادلة.", chunk_id=f"a{i}") for i in range(12)
]
MEMORIES = [recollection(f"prefers answer style {i}", memory_id=f"m{i}") for i in range(12)]


# --- the cost model never undercharges ------------------------------------


def test_the_implementation_satisfies_the_contract():
    assert isinstance(RenderedEvidenceCost(ESTIMATOR), RenderedCost)


def charged_for_documents(results):
    cost = RenderedEvidenceCost(ESTIMATOR)
    return cost.document_section() + sum(cost.document(r) for r in results)


def charged_for_memories(memories):
    cost = RenderedEvidenceCost(ESTIMATOR)
    return cost.memory_section() + sum(cost.memory(m) for m in memories)


def test_a_rendered_evidence_section_costs_no_more_than_was_charged():
    """Twelve passages, so positions [10]-[12] carry the extra digit that a
    single-item measurement cannot see."""
    for results in (ENGLISH, ARABIC):
        section = f"{GROUNDING_PREAMBLE}\n\n{render_evidence(results)}"
        assert ESTIMATOR.estimate(section) <= charged_for_documents(results)


def test_a_rendered_memory_section_costs_no_more_than_was_charged():
    section = f"{MEMORY_PREAMBLE}\n\n{render_memories(MEMORIES)}"
    assert ESTIMATOR.estimate(section) <= charged_for_memories(MEMORIES)


def test_the_charge_includes_the_boundary_lines_not_only_the_text():
    """The defect itself: bare text is not what the prompt carries."""
    cost = RenderedEvidenceCost(ESTIMATOR)
    for result in ENGLISH:
        assert cost.document(result) > ESTIMATOR.estimate(result.chunk.text)
    for evidence in MEMORIES:
        assert cost.memory(evidence) > ESTIMATOR.estimate(evidence.record.content)


def test_the_charge_follows_the_source_uri():
    """The URI is rendered in the opening line, so a longer one costs more."""
    cost = RenderedEvidenceCost(ESTIMATOR)
    short = passage("same text", chunk_id="c1", source="file:///a.md")
    long = passage("same text", chunk_id="c1", source="file:///" + "deep/" * 20 + "a.md")
    assert cost.document(long) > cost.document(short)


def test_the_preambles_are_charged():
    cost = RenderedEvidenceCost(ESTIMATOR)
    assert cost.document_section() >= ESTIMATOR.estimate(GROUNDING_PREAMBLE)
    assert cost.memory_section() >= ESTIMATOR.estimate(MEMORY_PREAMBLE)


# --- how the assembler spends it ------------------------------------------


class FlatCost:
    """Round numbers, so the arithmetic below is readable."""

    def document(self, result):
        return 10

    def memory(self, evidence):
        return 20

    def document_section(self):
        return 100

    def memory_section(self):
        return 200


def assemble(results=(), memories=(), *, budget=10_000):
    return HybridContextAssembler(ESTIMATOR, rendered_cost=FlatCost()).assemble(
        results=list(results),
        memories=list(memories),
        budget=ContextBudget(available_tokens=budget, source="test"),
    )


def test_a_section_is_charged_once_with_its_first_item():
    context = assemble(ENGLISH[:3])
    assert context.document_context.token_estimate == 100 + 3 * 10


def test_a_section_with_nothing_selected_costs_nothing():
    context = assemble(ENGLISH[:3])
    assert context.selected_memories == ()
    assert context.memory_token_estimate == 0


def test_both_sections_are_charged_when_both_are_used():
    context = assemble(ENGLISH[:2], MEMORIES[:2])
    assert context.document_context.token_estimate == 100 + 2 * 10
    assert context.memory_token_estimate == 200 + 2 * 20


def test_an_item_that_fits_as_text_but_not_as_rendered_is_excluded():
    """The budget admits the first passage's section and itself (110), not a
    second one's wrapper. Charging text alone would have let it in."""
    context = assemble(ENGLISH[:2], budget=115)
    assert len(context.document_context.selected) == 1
    [excluded] = context.document_context.excluded
    assert excluded.reason is ExclusionReason.BUDGET_EXHAUSTED
    assert excluded.token_cost == 10


def test_without_a_rendered_cost_the_phase_2_meaning_is_kept():
    """Callers that render nothing still charge bare text."""
    context = HybridContextAssembler(ESTIMATOR).assemble(
        results=ENGLISH[:1],
        memories=[],
        budget=ContextBudget(available_tokens=10_000, source="test"),
    )
    assert context.document_context.token_estimate == ESTIMATOR.estimate(
        ENGLISH[0].chunk.text
    )


# --- the whole message, both sections --------------------------------------


class Spec:
    name = "boss"
    provider = "ollama"

    def __init__(self, context_window):
        self.context_window = context_window


class Returns:
    def __init__(self, items):
        self.items = list(items)

    def retrieve(self, query):
        return tuple(self.items)


def test_a_message_with_both_sections_fits_its_budget():
    """The memory path, which the factory test does not reach: memories and
    passages share one budget and one message, joined as ContextBuilder joins
    them. Tight enough that something is left out."""
    builder = ContextBuilder(
        retriever=Returns(ENGLISH[:6]),
        assembler=HybridContextAssembler(
            ESTIMATOR, rendered_cost=RenderedEvidenceCost(ESTIMATOR)
        ),
        budget_policy=ReserveBasedBudgetPolicy(),
        estimator=ESTIMATOR,
        memory_retriever=Returns(MEMORIES[:6]),
        limit=6,
    )
    grounding = builder.build(
        session_id="s1", query="style", language="en", model=Spec(1900), history=[]
    )
    assert grounding.message is not None
    assert grounding.memories_used >= 1 and grounding.used >= 1
    assert grounding.memories_dropped + grounding.dropped >= 1

    budget = grounding.context.budget.available_tokens
    assert ESTIMATOR.estimate(grounding.message.content) <= budget
