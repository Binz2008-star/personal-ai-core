"""Token estimation.

Implements `core.contracts.TokenEstimator`.

**This is an estimate, and the name of the class says so.** There is no
tokenizer here. This project has no runtime dependencies, and a tokenizer means
either a downloaded vocabulary or a third-party package. Rather than pretend,
this implementation is explicit about being a heuristic and is built so that
its error falls on the safe side.

Why not the obvious `len(text) // 4`:

ADR-005 records the audited system using a flat `CHARS_PER_TOKEN = 4`. That
ratio is roughly right for English prose under a BPE tokenizer and badly wrong
for Arabic, where the same tokenizers emit closer to one token every two
characters. A flat 4 therefore *under*-estimates Arabic by around half.

Under-estimating is the dangerous direction. Over-estimating wastes budget and
includes one fewer passage. Under-estimating overflows the model's context and
the overflow is resolved by truncation -- silently, at the wrong end, with no
error anywhere. An Arabic conversation degrades and nothing reports why.

So the ratio is per script, and every ratio is chosen at the pessimistic end
of what common multilingual BPE tokenizers produce. `estimate()` is intended
to be at or above the true count for ordinary text; a caller that needs an
exact count needs a real tokenizer behind this same contract, which is the
point of it being a contract.
"""
from __future__ import annotations

import math
import unicodedata

# Token cost per character, by script. Higher means the script packs fewer
# characters into a token. These are pessimistic by design: see the module
# docstring for why the error is deliberately one-sided.
_LATIN_COST = 0.27          # ~3.7 chars/token; BPE prose is nearer 4
_ARABIC_COST = 0.55         # ~1.8 chars/token
_CJK_COST = 1.0             # roughly one token per character, often more
_DIGIT_COST = 0.5           # digits are split far more finely than letters
_OTHER_COST = 0.5           # punctuation, symbols, unlisted scripts
_WHITESPACE_COST = 0.15     # usually absorbed into an adjacent token


def _character_cost(character: str) -> float:
    if character.isspace():
        return _WHITESPACE_COST
    if character.isdigit():
        return _DIGIT_COST

    code = ord(character)
    if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0xFB50 <= code <= 0xFDFF:
        return _ARABIC_COST
    if (
        0x4E00 <= code <= 0x9FFF      # CJK unified ideographs
        or 0x3040 <= code <= 0x30FF   # kana
        or 0xAC00 <= code <= 0xD7AF   # hangul syllables
    ):
        return _CJK_COST
    if character.isalpha() and code < 0x0370:
        return _LATIN_COST
    if character.isalpha():
        return _OTHER_COST
    return _OTHER_COST


class ScriptAwareTokenEstimator:
    """Per-script, deliberately pessimistic token estimate.

    Deterministic, allocation-light, and correct in the only sense that
    matters for a budget: it does not claim text is cheaper than it is.
    """

    def __init__(self, *, safety_margin: float = 1.0) -> None:
        if safety_margin < 1.0:
            raise ValueError(
                "safety_margin below 1.0 would make the estimate optimistic, "
                "which is the failure mode this estimator exists to avoid"
            )
        self._safety_margin = safety_margin

    @property
    def model_id(self) -> str:
        """Identifies the estimator, not a model.

        Named honestly so that a budget traced back to here cannot be mistaken
        for one computed by a real tokenizer.
        """
        return f"heuristic:script-aware-v1:margin={self._safety_margin:g}"

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        normalized = unicodedata.normalize("NFC", text)
        total = sum(_character_cost(character) for character in normalized)
        # Any non-empty text costs at least one token.
        return max(1, math.ceil(total * self._safety_margin))
