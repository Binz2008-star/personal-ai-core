"""GreedyContextAssembler.

Half of what is asserted here is about the *excluded* list. A context
assembler that reports only what it kept makes a bad answer unexplainable:
there is no way to distinguish "the evidence was never retrieved" from "the
evidence was retrieved and then dropped four hundred tokens short".
"""
import pytest

from personal_ai_core.context import GreedyContextAssembler, ScriptAwareTokenEstimator
from personal_ai_core.core.context import ContextBudget, ExclusionReason
from personal_ai_core.core.knowledge import (
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)


class FixedCostEstimator:
    """Every text costs its own length in tokens. Makes budgets exact."""

    model_id = "test:one-token-per-character"

    def estimate(self, text: str) -> int:
        return len(text)


@pytest.fixture
def assembler():
    return GreedyContextAssembler(FixedCostEstimator())


def result(chunk):
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            chunk_id=chunk.id,
            start=chunk.start,
            end=chunk.end,
            methods=(RetrievalMethod.VECTOR,),
        ),
    )


def test_everything_that_fits_is_selected(assembler, make_chunk):
    results = [result(make_chunk("aaaa", chunk_id="a")), result(make_chunk("bbb", chunk_id="b"))]
    assembled = assembler.assemble(results, budget=ContextBudget(100))

    assert [r.chunk.id for r in assembled.selected] == ["a", "b"]
    assert assembled.excluded == ()
    assert assembled.token_estimate == 7
    assert assembled.within_budget


def test_rank_order_is_preserved_not_re_optimized(assembler, make_chunk):
    """Packing the budget optimally would prefer short passages over relevant
    ones. Retrieval already ranked these."""
    results = [result(make_chunk(text, chunk_id=text)) for text in ("aaaaa", "b", "cc")]
    assembled = assembler.assemble(results, budget=ContextBudget(100))
    assert [r.chunk.id for r in assembled.selected] == ["aaaaa", "b", "cc"]


def test_what_does_not_fit_is_excluded_with_a_reason_and_a_cost(assembler, make_chunk):
    results = [result(make_chunk("a" * 10, chunk_id="big"))]
    assembled = assembler.assemble(results, budget=ContextBudget(5))

    assert assembled.selected == ()
    assert len(assembled.excluded) == 1
    assert assembled.excluded[0].reason is ExclusionReason.BUDGET_EXHAUSTED
    assert assembled.excluded[0].token_cost == 10
    assert assembled.excluded[0].result.chunk.id == "big"


def test_one_oversized_result_does_not_discard_everything_behind_it(assembler, make_chunk):
    """Scanning continues past a result that will not fit."""
    results = [
        result(make_chunk("a" * 50, chunk_id="big")),
        result(make_chunk("bb", chunk_id="small")),
    ]
    assembled = assembler.assemble(results, budget=ContextBudget(10))

    assert [r.chunk.id for r in assembled.selected] == ["small"]
    assert [e.result.chunk.id for e in assembled.excluded] == ["big"]


def test_a_repeated_chunk_is_excluded_as_a_duplicate(assembler, make_chunk):
    chunk = make_chunk("shared text", chunk_id="same")
    assembled = assembler.assemble(
        [result(chunk), result(chunk)], budget=ContextBudget(100)
    )
    assert len(assembled.selected) == 1
    assert assembled.excluded[0].reason is ExclusionReason.DUPLICATE


def test_identical_text_under_different_ids_is_still_a_duplicate(assembler, make_chunk):
    """Overlapping chunks routinely repeat a passage. Paying twice halves the
    budget for no additional evidence."""
    assembled = assembler.assemble(
        [
            result(make_chunk("the same passage", chunk_id="a")),
            result(make_chunk("the   same\npassage", chunk_id="b")),
        ],
        budget=ContextBudget(100),
    )
    assert [r.chunk.id for r in assembled.selected] == ["a"]
    assert assembled.excluded[0].reason is ExclusionReason.DUPLICATE


def test_a_duplicate_costs_nothing(assembler, make_chunk):
    chunk = make_chunk("abcd", chunk_id="same")
    assembled = assembler.assemble(
        [result(chunk), result(chunk)], budget=ContextBudget(100)
    )
    assert assembled.token_estimate == 4


def test_a_zero_budget_selects_nothing_and_explains_why(assembler, make_chunk):
    assembled = assembler.assemble(
        [result(make_chunk("a", chunk_id="a"))], budget=ContextBudget(0)
    )
    assert assembled.selected == ()
    assert assembled.excluded[0].reason is ExclusionReason.BUDGET_EXHAUSTED
    assert assembled.within_budget


def test_no_results_produces_an_empty_context(assembler):
    assembled = assembler.assemble([], budget=ContextBudget(100))
    assert assembled.selected == ()
    assert assembled.excluded == ()
    assert assembled.token_estimate == 0


def test_the_budget_is_never_exceeded(assembler, make_chunk):
    results = [result(make_chunk("x" * n, chunk_id=str(n))) for n in range(1, 20)]
    assembled = assembler.assemble(results, budget=ContextBudget(30))
    assert assembled.token_estimate <= 30
    assert assembled.within_budget


def test_the_budget_travels_with_the_result(assembler, make_chunk):
    budget = ContextBudget(42, source="model-registry")
    assembled = assembler.assemble([], budget=budget)
    assert assembled.budget is budget


def test_arabic_is_budgeted_more_expensively_than_english(make_chunk):
    """With the real estimator: same character count, different token cost.

    An assembler paired with a flat 4-chars-per-token estimate would fit both
    of these and overflow on the Arabic one.
    """
    assembler = GreedyContextAssembler(ScriptAwareTokenEstimator())
    english = "The quick brown fox jumps over the lazy dog every single day."
    arabic = "الثعلب البني السريع يقفز فوق الكلب الكسول كل يوم على الاطلاق."

    english_cost = assembler.assemble(
        [result(make_chunk(english, chunk_id="en"))], budget=ContextBudget(1000)
    ).token_estimate
    arabic_cost = assembler.assemble(
        [result(make_chunk(arabic, chunk_id="ar"))], budget=ContextBudget(1000)
    ).token_estimate

    assert arabic_cost > english_cost


def test_dropped_count_reports_the_exclusions(assembler, make_chunk):
    results = [result(make_chunk("x" * 50, chunk_id=str(n))) for n in range(3)]
    assembled = assembler.assemble(results, budget=ContextBudget(10))
    assert assembled.dropped_count == 3
