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
            "Install `transformers` (for the Boss model's own tokenizer) or "
            "`tiktoken` to run this."
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
