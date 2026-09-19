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


# --- the calibration is a decision, pinned (audit finding 3) ---------------


def test_the_per_script_costs_are_the_stated_calibration():
    """Every one of these changes every budget, so none may drift silently.

    These are a calibration, not a measurement. Nothing here claims they match
    a real tokenizer -- that is what
    `tests/integration/test_token_estimator_validation.py` exists to settle,
    and it skips until a reference tokenizer is installed.
    """
    from personal_ai_core.context import token_estimator as te

    assert (
        te._LATIN_COST,
        te._ARABIC_COST,
        te._CJK_COST,
        te._DIGIT_COST,
        te._OTHER_COST,
        te._WHITESPACE_COST,
    ) == (0.27, 0.55, 1.0, 0.5, 0.5, 0.15)


def test_the_arabic_to_latin_ratio_is_about_two():
    """Stated as what it is: a property of this calibration.

    It is *not* a claim about how real tokenizers behave. If the validation
    harness ever runs and disagrees, this constant moves and this test moves
    with it.
    """
    from personal_ai_core.context import token_estimator as te

    assert te._ARABIC_COST / te._LATIN_COST == pytest.approx(2.0, abs=0.1)


def test_every_script_costs_at_least_as_much_per_character_as_latin():
    """The direction the calibration commits to.

    A BPE vocabulary trained mostly on English packs English densely and
    everything else less so. Any script priced *below* Latin would be a
    calibration bug, not a tuning choice.
    """
    from personal_ai_core.context import token_estimator as te

    for name in ("_ARABIC_COST", "_CJK_COST", "_DIGIT_COST", "_OTHER_COST"):
        assert getattr(te, name) >= te._LATIN_COST, name


def test_the_validation_harness_exists_and_is_reachable():
    """The claim must stay falsifiable.

    Deleting the harness would quietly turn "unverified, with a way to verify"
    back into "unverified" -- which is where audit finding 3 started.
    """
    from pathlib import Path

    harness = (
        Path(__file__).resolve().parents[1]
        / "integration"
        / "test_token_estimator_validation.py"
    )
    assert harness.exists(), "the token estimator validation harness is gone"
    body = harness.read_text(encoding="utf-8")
    assert "estimated >= actual" in body, "the harness no longer checks the claim"


def test_a_fractional_cost_rounds_up_never_down(estimator):
    """Rounding down is under-estimating, by a whole token, every time.

    The one-sided-error policy was enforced on `safety_margin` and nowhere
    else; truncating here would have slipped past every other test in this
    file because each one only compares estimates against each other.
    """
    import math

    from personal_ai_core.context import token_estimator as te

    for text in ("abcdefgh", "hello there", "الذكاء", "mixed نص here", "12345"):
        exact = sum(te._character_cost(character) for character in text)
        assert estimator.estimate(text) == math.ceil(exact), text
        # The property that matters, stated directly rather than implied.
        assert estimator.estimate(text) >= exact, text


def test_rounding_up_survives_the_safety_margin(estimator):
    import math

    from personal_ai_core.context import token_estimator as te

    cautious = ScriptAwareTokenEstimator(safety_margin=1.3)
    for text in ("abcdefgh", "الذكاء الاصطناعي"):
        exact = sum(te._character_cost(c) for c in text) * 1.3
        assert cautious.estimate(text) == math.ceil(exact), text
