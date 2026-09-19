"""Context budget contract.

The budget is explicit, and is not the model's context window (ADR-005).
"""
import pytest

from personal_ai_core.core.config import Settings
from personal_ai_core.core.context import (
    BudgetedContext,
    ContextBudget,
    ExcludedResult,
    ExclusionReason,
)
from personal_ai_core.core.knowledge import Chunk, RetrievalProvenance, RetrievalResult


def a_result(text="hello"):
    chunk = Chunk(
        document_id="d", version_id="v", text=text, ordinal=0, start=0, end=len(text)
    )
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id="d", version_id="v", chunk_id=chunk.id, start=0, end=len(text)
        ),
    )


def test_budget_is_explicit_and_records_its_source():
    budget = ContextBudget(available_tokens=4000, source="registry:model-001")
    assert budget.available_tokens == 4000
    assert budget.source == "registry:model-001"


def test_budget_is_not_the_model_context_window():
    """8192 is a model property, not automatically a budget (ADR-005)."""
    window = Settings.from_env({}).boss_context_window
    assert window == 8192
    # nothing derives a budget from it; a budget must be constructed
    budget = ContextBudget(available_tokens=3000, source="policy")
    assert budget.available_tokens != window


def test_budget_rejects_a_negative_allowance():
    with pytest.raises(ValueError, match="non-negative"):
        ContextBudget(available_tokens=-1)


def test_context_records_what_was_excluded_and_why():
    kept, dropped = a_result("kept"), a_result("dropped")
    ctx = BudgetedContext(
        selected=(kept,),
        excluded=(
            ExcludedResult(
                result=dropped, reason=ExclusionReason.BUDGET_EXHAUSTED, token_cost=50
            ),
        ),
        token_estimate=10,
        budget=ContextBudget(available_tokens=20, source="test"),
    )
    assert ctx.dropped_count == 1
    assert ctx.excluded[0].reason is ExclusionReason.BUDGET_EXHAUSTED
    assert ctx.excluded[0].token_cost == 50
    assert ctx.within_budget


def test_exceeding_the_budget_is_visible_not_silent():
    ctx = BudgetedContext(
        token_estimate=500, budget=ContextBudget(available_tokens=100, source="test")
    )
    assert not ctx.within_budget


def test_every_exclusion_reason_is_representable():
    for reason in ExclusionReason:
        assert ExcludedResult(result=a_result(), reason=reason).reason is reason
