"""Token estimation.

Implements `core.contracts.TokenEstimator`.

**This is an estimate, and the name of the class says so.** There is no
tokenizer here. This project has no runtime dependencies, and a tokenizer means
either a downloaded vocabulary or a third-party package. Rather than pretend,
this implementation is explicit about being a heuristic and is built so that
its error falls on the safe side.

Why not the obvious `len(text) // 4`:

ADR-005 records the audited system using a flat `CHARS_PER_TOKEN = 4`. A single
ratio cannot be right for every script: a BPE vocabulary trained mostly on
English packs English densely and everything else less so, so one constant is
necessarily generous to one script and mean to the others.

Under-estimating is the dangerous direction. Over-estimating wastes budget and
includes one fewer passage. Under-estimating overflows the model's context and
the overflow is resolved by truncation -- silently, at the wrong end, with no
error anywhere. A conversation degrades and nothing reports why.

So the ratio is per script, and each is set toward the expensive end.


What is decided, and what is actually evidenced
-----------------------------------------------

These are different things and the distinction is the honest part of this
module.

**Decided.** The per-script costs below, and the rule that the error should
fall on the expensive side. `safety_margin` refuses any value under 1.0, so the
*policy* of a one-sided error is enforced at the boundary and tested.

**Not evidenced.** That the constants actually achieve it. Validating "this
estimate is never below the real count" requires a real tokenizer to compare
against, and this project has no runtime dependencies and no vocabulary. The
numbers below are a calibration, chosen to be cautious; they have not been
measured against the Boss model's tokenizer or any other.

`tests/integration/test_token_estimator_validation.py` is the harness that
settles it. It skips when no reference tokenizer is installed -- which is the
normal case here -- and checks the one-sided-error claim directly wherever one
is. Until it has run somewhere, treat the ratios as a deliberate guess with a
known direction, not as a measurement.

A caller needing an exact count needs a real tokenizer behind this same
contract. That is the point of it being a contract.
"""
from __future__ import annotations

import math
import unicodedata

# Token cost per character, by script. Higher means the script packs fewer
# characters into a token.
#
# A calibration, not a measurement. Chosen toward the expensive end so the
# error falls on the safe side -- see "What is decided, and what is actually
# evidenced" above. Changing one of these changes every budget, so they are
# named constants and pinned by a test.
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
    """Per-script token estimate, calibrated to err on the expensive side.

    Deterministic and allocation-light. "Errs on the expensive side" is the
    intent behind the constants and is not yet verified against a real
    tokenizer -- see the module docstring. `model_id` begins with `heuristic:`
    so a budget traced back here can never be mistaken for a token count.
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
