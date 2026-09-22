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
from personal_ai_core.context.budget import DEFAULT_IDENTITY_RESERVE
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
    "kwargs",
    [{"generation_reserve": -1}, {"overhead": -1}, {"identity_reserve": -1}],
)
def test_negative_reserves_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ReserveBasedBudgetPolicy(**kwargs)


def test_negative_history_is_rejected(policy):
    with pytest.raises(ValueError):
        policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=-1)


@pytest.mark.parametrize(
    "field",
    ["context_window", "generation_reserve", "overhead", "history", "identity"],
)
def test_a_negative_allocation_field_is_rejected(field):
    kwargs = dict(
        context_window=100, generation_reserve=0, overhead=0, history=0, identity=0
    )
    kwargs[field] = -1
    with pytest.raises(ValueError):
        ContextAllocation(**kwargs)


# --- ADR-011 prerequisite A: the identity share ----------------------------


def test_identity_defaults_to_zero_because_identity_is_not_built():
    """The honest default.

    A reserve invented for a component that does not exist would shrink
    `evidence` today for no benefit, and the number would be fabricated.
    """
    policy = ReserveBasedBudgetPolicy()
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=0)
    assert allocation.identity == DEFAULT_IDENTITY_RESERVE == 0


def test_a_funded_identity_share_reaches_the_allocation():
    """The field must be settable, or it is decoration rather than a budget line."""
    policy = ReserveBasedBudgetPolicy(identity_reserve=300)
    allocation = policy.allocate(model=Spec(BOSS_WINDOW), history_tokens=0)
    assert allocation.identity == 300


def test_identity_is_counted_in_spoken_for():
    funded = ContextAllocation(
        context_window=1000, generation_reserve=10, overhead=5, history=20, identity=300
    )
    assert funded.spoken_for == 10 + 5 + 20 + 300


def test_evidence_shrinks_by_exactly_the_identity_share():
    """ADR-011 Rule 4's whole point: a stated amount, not an unexplained one."""
    unfunded = ReserveBasedBudgetPolicy(identity_reserve=0).allocate(
        model=Spec(BOSS_WINDOW), history_tokens=500
    )
    funded = ReserveBasedBudgetPolicy(identity_reserve=300).allocate(
        model=Spec(BOSS_WINDOW), history_tokens=500
    )
    assert unfunded.evidence - funded.evidence == 300


def test_identity_can_overcommit_the_window_visibly():
    """Identity is a real share: enough of it exhausts the turn like any other."""
    allocation = ContextAllocation(
        context_window=100, generation_reserve=0, overhead=0, history=0, identity=101
    )
    assert allocation.overcommitted
    assert allocation.evidence == 0


def test_the_source_string_names_the_identity_share():
    """ADR-005: a budget must be traceable to what produced it.

    An identity share absent from `source` would be a share nobody could see
    in the record -- the untraceable-budget failure in miniature.
    """
    source = ReserveBasedBudgetPolicy(identity_reserve=300).source
    assert "identity=300" in source


def test_every_allocation_share_is_counted_in_spoken_for():
    """A field added and left out of SHARES is silently free.

    That is the ADR-005 failure exactly: a share that exists, is recorded, and
    does not affect the arithmetic. This walks the dataclass rather than a
    hand-written list, so a future `tool_observations` cannot slip past.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ContextAllocation)}
    uncounted = fields - {"context_window"} - set(ContextAllocation.SHARES)
    assert uncounted == set(), (
        f"{uncounted} are allocation fields that spoken_for ignores; add them "
        "to ContextAllocation.SHARES or they cost nothing"
    )
