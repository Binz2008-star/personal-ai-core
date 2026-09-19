"""ReserveBasedBudgetPolicy and ContextAllocation.

ADR-005's failure was not a wrong number, it was an untraceable one: a
hard-coded 24000 for a 32K model, with nothing recorded that could contradict
it. So these tests check the arithmetic *and* that every input to it survives
on the allocation.
"""
import pytest

from personal_ai_core.context import (
    DEFAULT_GENERATION_RESERVE,
    DEFAULT_OVERHEAD,
    ReserveBasedBudgetPolicy,
)
from personal_ai_core.core.context import ContextAllocation


class Spec:
    """A minimal ModelSpecLike."""

    def __init__(self, context_window, name="boss", provider="ollama"):
        self.context_window = context_window
        self.name = name
        self.provider = provider


BOSS_WINDOW = 8192


@pytest.fixture
def policy():
    return ReserveBasedBudgetPolicy(generation_reserve=1024, overhead=256)


# --- the arithmetic --------------------------------------------------------


def test_evidence_is_the_window_minus_every_other_share(policy):
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=500)
    assert allocation.evidence == BOSS_WINDOW - 1024 - 256 - 500


def test_every_input_is_recorded_on_the_allocation(policy):
    """So a budget can be argued with, not just accepted."""
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=500)
    assert allocation.context_window == BOSS_WINDOW
    assert allocation.generation_reserve == 1024
    assert allocation.overhead == 256
    assert allocation.history == 500
    assert allocation.spoken_for == 1780


def test_the_budget_never_goes_negative(policy):
    """A negative budget that wrapped into a large positive one is exactly the
    class of silent failure ADR-005 records."""
    allocation = policy.allocate(model=Spec(1000), history_tokens=100_000)
    assert allocation.evidence == 0
    assert allocation.budget(source="test").available_tokens == 0


def test_overcommitment_is_visible_rather_than_swallowed(policy):
    """The turn already exceeds the window before any evidence is added."""
    tight = policy.allocate(model=Spec(1000), history_tokens=100_000)
    roomy = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=100)
    assert tight.overcommitted is True
    assert roomy.overcommitted is False


def test_the_budget_shrinks_as_the_conversation_grows(policy):
    """A budget computed once from the window is right on turn one and wrong
    by turn twenty. This is why history_tokens is a parameter."""
    early = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=100)
    later = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=4000)
    assert later.evidence < early.evidence


def test_the_budget_follows_the_active_model(policy):
    """Changing the Boss model must change the budget with it."""
    small = policy.allocate(model=Spec(4096), history_tokens=0)
    large = policy.allocate(model=Spec(32768), history_tokens=0)
    assert large.evidence - small.evidence == 32768 - 4096


def test_the_generation_reserve_does_not_scale_with_the_window(policy):
    """An answer does not get longer because the window did."""
    small = policy.allocate(model=Spec(4096), history_tokens=0)
    large = policy.allocate(model=Spec(32768), history_tokens=0)
    assert small.generation_reserve == large.generation_reserve


# --- provenance of the number ---------------------------------------------


def test_the_budget_names_the_policy_that_produced_it(policy):
    budget = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=0).budget(
        source=policy.source
    )
    assert "reserve-based" in budget.source
    assert "generation=1024" in budget.source
    assert "overhead=256" in budget.source


def test_the_source_string_changes_with_the_settings():
    a = ReserveBasedBudgetPolicy(generation_reserve=1024).source
    b = ReserveBasedBudgetPolicy(generation_reserve=2048).source
    assert a != b


def test_a_budget_is_not_a_context_window(policy):
    """The single sentence ADR-005 exists to enforce."""
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=0)
    assert allocation.evidence < BOSS_WINDOW


# --- defaults and validation ----------------------------------------------


def test_defaults_are_named_constants_not_literals():
    policy = ReserveBasedBudgetPolicy()
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=0)
    assert allocation.generation_reserve == DEFAULT_GENERATION_RESERVE
    assert allocation.overhead == DEFAULT_OVERHEAD


@pytest.mark.parametrize(
    "kwargs", [{"generation_reserve": -1}, {"overhead": -1}]
)
def test_negative_reserves_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ReserveBasedBudgetPolicy(**kwargs)


def test_negative_history_is_rejected(policy):
    with pytest.raises(ValueError):
        policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=-1)


@pytest.mark.parametrize(
    "field", ["context_window", "generation_reserve", "overhead", "history"]
)
def test_a_negative_allocation_field_is_rejected(field):
    kwargs = dict(context_window=100, generation_reserve=0, overhead=0, history=0)
    kwargs[field] = -1
    with pytest.raises(ValueError):
        ContextAllocation(**kwargs)
