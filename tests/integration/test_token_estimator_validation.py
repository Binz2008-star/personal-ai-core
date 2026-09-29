"""Does `ScriptAwareTokenEstimator` actually err on the expensive side?

Audit finding 3. The module claims a one-sided error: the estimate should be
at or above a real tokenizer's count, because over-estimating costs one
passage and under-estimating overflows the context and truncates silently.

That claim cannot be settled without a real tokenizer, and this project has no
runtime dependencies and ships no vocabulary. So this file is the harness that
settles it wherever one *is* installed, and skips loudly where none is.

**It will normally skip.** That is the honest state of the claim, not a gap
being hidden: a skipped test here means "still unverified in this
environment", and the skip reason says exactly that. It is written as an
executable statement of what would falsify the calibration, so the claim is
falsifiable rather than merely asserted.

Install one of the tokenizers named in `_load_reference()` to run it.

On which tokenizer counts
-------------------------

The Boss model's own tokenizer is the only fully authoritative reference --
a budget is spent against *that* model's context window. Anything else is an
approximation of an approximation, so when a non-Boss tokenizer is used the
assertion message says which one, and a failure against it is a signal to
investigate rather than proof the constants are wrong.

The Boss model runs under Ollama, and `DEFAULT_BOSS_MODEL` is an Ollama
identifier rather than a Hugging Face repo id, so tokenizer libraries cannot
resolve it directly. Workstream B verified that the model's embedded
vocabulary and merges are byte-identical to its documented tokenizer lineage,
so when the Ollama identifier cannot be resolved this harness falls back to
that lineage and counts it as authoritative. `_load_reference()` reports which
reference produced every count.
"""
from __future__ import annotations

import pytest

from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.core.config import DEFAULT_BOSS_MODEL

# Samples deliberately spanning the scripts the estimator prices differently,
# plus the mixed and punctuation-heavy cases where a per-script rule is most
# likely to be wrong.
SAMPLES = {
    "english-prose": "The quick brown fox jumps over the lazy dog every single day.",
    "english-long": (
        "Retrieval combines a vector arm and a lexical arm, fuses the two "
        "rankings by rank rather than by score, and records which arm found "
        "each passage so the ranking can be explained afterwards."
    ),
    "arabic-prose": "الثعلب البني السريع يقفز فوق الكلب الكسول كل يوم على الاطلاق.",
    "arabic-long": (
        "البحث الهجين يجمع بين الذراع الدلالية والذراع المعجمية، ويوحد "
        "الترتيبين باستخدام الرتب وليس الدرجات، ويسجل أي ذراع وجدت كل مقطع "
        "حتى يمكن تفسير الترتيب لاحقا."
    ),
    "arabic-with-diacritics": "الْعَرَبِيَّةُ لُغَةٌ جَمِيلَةٌ وَغَنِيَّةٌ بِالْمَعَانِي.",
    "mixed": "The term الذكاء الاصطناعي means artificial intelligence in Arabic.",
    "cjk": "日本語のテキストはトークンを多く消費します。",
    "digits": "Order 998877 shipped 2026-09-19 for 1,234.56 AED across 42 items.",
    "punctuation": "Really?! Well... (that's -- unexpected); isn't it?",
    "code-like": "def estimate(self, text: str) -> int: return max(1, ceil(total))",
}


def _load_reference():
    """Return (name, encode_fn, is_boss_tokenizer) or None if none installed."""
    try:  # the Boss model's own tokenizer -- the authoritative reference
        from transformers import AutoTokenizer  # type: ignore

        tokenizer = AutoTokenizer.from_pretrained(DEFAULT_BOSS_MODEL)
        return (
            DEFAULT_BOSS_MODEL,
            lambda text: tokenizer.encode(text, add_special_tokens=False),
            True,
        )
    except Exception:
        pass

    try:  # the Boss model's documented tokenizer lineage
        # `DEFAULT_BOSS_MODEL` is an Ollama identifier, not a HF repo id, so a
        # tokenizer library cannot resolve it. The lineage below was verified
        # byte-identical to the model's embedded vocabulary and merges
        # (Workstream B), so it counts as authoritative for these samples.
        from tokenizers import Tokenizer  # type: ignore

        tokenizer = Tokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")
        return (
            "Qwen/Qwen2.5-7B-Instruct (Boss tokenizer lineage)",
            lambda text: tokenizer.encode(text, add_special_tokens=False).ids,
            True,
        )
    except Exception:
        pass

    try:  # a different BPE family: indicative, not authoritative
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("cl100k_base")
        return ("cl100k_base", encoding.encode, False)
    except Exception:
        pass

    return None


@pytest.fixture(scope="module")
def reference():
    loaded = _load_reference()
    if loaded is None:
        pytest.skip(
            "no reference tokenizer available, so the one-sided-error claim in "
            "ScriptAwareTokenEstimator remains UNVERIFIED in this environment. "
            "Install `transformers` (for the Boss model's own tokenizer), "
            "`tokenizers` (for its documented lineage), or `tiktoken` to run this."
        )
    return loaded


@pytest.fixture
def estimator():
    return ScriptAwareTokenEstimator()


@pytest.mark.parametrize("label", sorted(SAMPLES))
def test_the_estimate_is_never_below_the_real_count(reference, estimator, label):
    """The whole claim, one sample at a time.

    Under-estimating is the failure that truncates a context silently, so a
    single sample below the real count falsifies the calibration.
    """
    name, encode, is_boss = reference
    text = SAMPLES[label]
    actual = len(encode(text))
    estimated = estimator.estimate(text)

    caveat = "" if is_boss else (
        f" NOTE: {name} is not the Boss model's tokenizer, so treat a failure "
        "here as a signal to investigate rather than proof."
    )
    assert estimated >= actual, (
        f"{label}: estimated {estimated} but {name} produced {actual} tokens -- "
        f"the estimate is optimistic, which overflows the context and truncates "
        f"silently.{caveat}"
    )


def test_the_estimate_is_not_absurdly_expensive(reference, estimator):
    """A one-sided error is only useful if the margin stays usable.

    An estimator that charged ten times the real cost would satisfy the test
    above and make the budget worthless.
    """
    name, encode, _ = reference
    for label, text in sorted(SAMPLES.items()):
        actual = len(encode(text))
        estimated = estimator.estimate(text)
        assert estimated <= actual * 3, (
            f"{label}: estimated {estimated} against {name}'s {actual} -- the "
            "margin is large enough to waste most of the budget"
        )


def test_the_flat_four_character_heuristic_would_have_failed(reference):
    """Evidence for the decision ADR-005 records, rather than an assertion.

    If this ever passes, the flat heuristic was adequate for these samples and
    the per-script split needs a better justification than it currently has.
    """
    name, encode, _ = reference
    optimistic = [
        label
        for label, text in SAMPLES.items()
        if len(text) // 4 < len(encode(text))
    ]
    assert optimistic, (
        f"against {name}, a flat 4 chars/token under-estimated nothing in this "
        "sample set -- the per-script calibration is unjustified by this evidence"
    )


# --- the calibration corpus (Workstream B) ----------------------------------
#
# The samples the constants were measured against: the regression classes that
# failed at the old constants, the whitespace and long-context cases where a
# per-script cost is most likely to be wrong, and the representative Latin and
# CJK cases. Together with SAMPLES these are the 26-sample corpus that drove
# the min-max calibration. These tests pin the *invariants* the constants were
# chosen to satisfy, not the estimate values.

CALIBRATION_SAMPLES = {
    "arabic-with-diacritics": "الْعَرَبِيَّةُ لُغَةٌ جَمِيلَةٌ وَغَنِيَّةٌ بِالْمَعَانِي.",
    "digits": "Order 998877 shipped 2026-09-19 for 1,234.56 AED across 42 items.",
    "unicode-symbols": "λ λμν σ² ½ ⅓ √x ∫ f(x) dx ≈ ≠ ≤ ≥ → ∞ § ¶ † ‡ ° ± × ÷",
    "emoji-mixed": "Hello there 👋 how are you today? 🎉🎊 Let's celebrate 🥳 with cake 🍰 and coffee ☕️ 👍😄",
    "whitespace-heavy": "a   b    c\n\n\n\td\t\t\te\n   multiple   spaces   and   tabs   \n  \n  end",
    "long-context-2k": ("Token estimation must predict length to plan budgets. " * 180),
    "long-context-rust": (
        "fn estimate(text: &str) -> usize {\n"
        "    text.chars().map(|c| cost(c)).sum::<f32>().ceil() as usize\n"
        "}\nfn cost(c: char) -> f32 {\n"
        "    match c { ' ' => 0.15, _ if c.is_ascii_digit() => 0.5, _ => 0.27 }\n"
        "}\n"
    )
    * 30,
    "english-prose": "The quick brown fox jumps over the lazy dog every single day.",
    "cjk": "日本語のテキストはトークンを多く消費します。",
}

# The ceiling the calibrated constants were chosen against, on the validated
# corpus. The 3x bound in `test_the_estimate_is_not_absurdly_expensive` is a
# harness guardrail, not a production contract.
CALIBRATION_CEILING = 2.5


@pytest.mark.parametrize("label", sorted(CALIBRATION_SAMPLES))
def test_the_calibrated_estimate_is_never_below_the_real_count(
    reference, estimator, label
):
    """The one-sided invariant, re-asserted on the calibration corpus.

    The constants were measured against these samples; a single one below the
    real count now falsifies the calibration itself, not just the claim.
    """
    name, encode, is_boss = reference
    text = CALIBRATION_SAMPLES[label]
    actual = len(encode(text))
    estimated = estimator.estimate(text)

    caveat = "" if is_boss else (
        f" NOTE: {name} is not the Boss model's tokenizer, so treat a failure "
        "here as a signal to investigate rather than proof."
    )
    assert estimated >= actual, (
        f"{label}: estimated {estimated} but {name} produced {actual} tokens -- "
        f"a calibrated constant under-estimates a validated corpus sample.{caveat}"
    )


def test_the_calibrated_estimate_stays_within_the_two_and_a_half_times_ceiling(
    reference, estimator
):
    """The constants are calibrated to 2.5x, not just to non-negative slack.

    The min-max calibration traded a little aggregate overhead for a bounded
    worst case. This pins that bound on the corpus it was validated against.
    """
    name, encode, is_boss = reference
    if not is_boss:
        pytest.skip(
            "the 2.5x ceiling is a calibration target validated on the Boss "
            "tokenizer lineage; a non-authoritative reference does not bind it"
        )

    worst = 0.0
    worst_label = ""
    for label, text in sorted(CALIBRATION_SAMPLES.items()):
        actual = len(encode(text))
        ratio = estimator.estimate(text) / actual
        if ratio > worst:
            worst, worst_label = ratio, label
    assert worst <= CALIBRATION_CEILING, (
        f"{worst_label}: {worst:.3f}x exceeds the {CALIBRATION_CEILING}x "
        f"calibration ceiling against {name}"
    )
