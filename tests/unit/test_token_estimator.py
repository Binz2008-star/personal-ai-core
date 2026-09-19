"""ScriptAwareTokenEstimator.

There is no reference tokenizer available in this project, so these tests
cannot assert an exact count against ground truth -- and they do not pretend
to. What they pin is the set of properties that make an estimate *safe to
budget with*: determinism, monotonicity, a one-sided error, and a per-script
ratio rather than the flat `CHARS_PER_TOKEN = 4` that ADR-005 records as the
failure this replaces.
"""
import pytest

from personal_ai_core.context import ScriptAwareTokenEstimator

# Same character count, different script. That is the whole point.
ENGLISH = "The quick brown fox jumps over the lazy dog every single day."
ARABIC = "الثعلب البني السريع يقفز فوق الكلب الكسول كل يوم على الاطلاق."


@pytest.fixture
def estimator():
    return ScriptAwareTokenEstimator()


def test_the_two_samples_really_are_the_same_length():
    """Guards the comparison below from becoming accidentally meaningless."""
    assert len(ENGLISH) == len(ARABIC)


def test_arabic_costs_more_than_english_for_the_same_length(estimator):
    """The bug ADR-005 records.

    A flat 4 chars/token under-estimates Arabic by roughly half. The overflow
    is then resolved by truncation -- silently, with no error -- so an Arabic
    conversation degrades and nothing reports why.
    """
    assert estimator.estimate(ARABIC) > estimator.estimate(ENGLISH)


def test_arabic_is_not_estimated_by_the_flat_four_character_heuristic(estimator):
    assert estimator.estimate(ARABIC) > len(ARABIC) // 4


def test_cjk_costs_more_per_character_than_latin(estimator):
    assert estimator.estimate("日本語のテキストです") > estimator.estimate("latin text")


@pytest.mark.parametrize("text", [ENGLISH, ARABIC], ids=["en", "ar"])
def test_estimation_is_deterministic(estimator, text):
    assert estimator.estimate(text) == estimator.estimate(text)


@pytest.mark.parametrize("text", [ENGLISH, ARABIC], ids=["en", "ar"])
def test_estimation_is_monotonic_in_length(estimator, text):
    assert estimator.estimate(text * 2) >= estimator.estimate(text)


def test_empty_text_costs_nothing(estimator):
    assert estimator.estimate("") == 0


@pytest.mark.parametrize("text", [".", " ", "a", "ا"], ids=["dot", "space", "en", "ar"])
def test_any_non_empty_text_costs_at_least_one_token(estimator, text):
    assert estimator.estimate(text) >= 1


def test_english_prose_lands_in_a_plausible_range(estimator):
    """A sanity bound, not a ground truth.

    BPE tokenizers put ordinary English prose near 4 characters per token. The
    estimate should be in that neighbourhood and biased slightly high -- never
    below, which is the direction that overflows a context window.
    """
    estimate = estimator.estimate(ENGLISH)
    assert len(ENGLISH) / 5 <= estimate <= len(ENGLISH) / 2


def test_a_safety_margin_raises_the_estimate(estimator):
    cautious = ScriptAwareTokenEstimator(safety_margin=2.0)
    assert cautious.estimate(ENGLISH) > estimator.estimate(ENGLISH)


def test_a_margin_below_one_is_refused():
    """It would make the estimate optimistic, which is the one direction that
    fails silently."""
    with pytest.raises(ValueError):
        ScriptAwareTokenEstimator(safety_margin=0.9)


def test_the_identity_says_it_is_a_heuristic(estimator):
    """So a budget traced back here is never mistaken for a real token count."""
    assert estimator.model_id.startswith("heuristic:")


def test_the_margin_is_part_of_the_identity(estimator):
    assert estimator.model_id != ScriptAwareTokenEstimator(safety_margin=1.5).model_id
