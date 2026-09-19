"""Text normalization and tokenization shared by the knowledge layer.

Language-neutral by construction. ADR-006 records the audited lexical arm as
hard-coded to English (`to_tsvector('english', ...)`), which degraded silently
for Arabic: no error, no warning, just worse recall. Nothing in this module
privileges one script.

The normalization here is deliberately *light*. Aggressive folding raises
recall and lowers precision, and an irreversible transform applied at both
index and query time is invisible once it is wrong. Each rule below is listed
with what it does and why it is safe; everything more opinionated is deferred
on purpose and named at the bottom of this docstring.

Applied:
  - Unicode NFKC, so visually identical text compares equal.
  - `casefold`, which handles more than ASCII lowercasing.
  - Arabic diacritics (tashkeel) removed: they are optional in written Arabic,
    so the same word appears with and without them and must still match.
  - Tatweel (U+0640) removed: a purely typographic elongation, never semantic.
  - Whitespace collapsed.

Deliberately NOT applied (recorded, not forgotten):
  - Alef/yeh/teh-marbuta folding (أ إ آ -> ا, ى -> ي, ة -> ه). Standard in
    Arabic IR and would raise recall, but it is lossy and changes meaning for
    some pairs. It belongs behind an explicit, tested option rather than
    being switched on silently here.
  - Stemming and stop-word removal, in any language. Both are per-language
    assets, and adopting one language's list is exactly the failure ADR-006
    describes.
"""
from __future__ import annotations

import re
import unicodedata

# Arabic combining marks (tashkeel) and the tatweel elongation character.
_ARABIC_MARKS = re.compile(
    "["
    "ؐ-ؚ"
    "ً-ٟ"
    "ٰ"
    "ۖ-ۜ"
    "۟-ۨ"
    "۪-ۭ"
    "ـ"
    "]"
)

_WHITESPACE = re.compile(r"\s+")

# A token is a run of word characters excluding the underscore. `\w` is
# Unicode-aware in Python 3, so Arabic, Latin, Cyrillic and digits all tokenize
# without a per-language rule.
#
# Known limit: scripts written without spaces (Chinese, Japanese, Thai) produce
# one long token per run. That is a real gap, it is not silent -- it is written
# here -- and it needs a segmenter, not a regex.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Normalize for both indexing and querying.

    Must be applied identically on both sides. A normalization used at index
    time but not at query time removes matches that look like a ranking
    problem and are not.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _ARABIC_MARKS.sub("", text)
    text = text.casefold()
    return _WHITESPACE.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    """Split normalized text into tokens, in order."""
    return _TOKEN.findall(normalize(text))


def character_ngrams(text: str, n: int) -> list[str]:
    """Character n-grams over normalized text, spaces included.

    Character n-grams are used rather than words alone because they degrade
    gracefully across morphologically rich scripts, where a word-level feature
    misses every inflected form.
    """
    if n < 1:
        raise ValueError("n must be positive")
    normalized = normalize(text)
    if len(normalized) < n:
        return [normalized] if normalized else []
    return [normalized[i : i + n] for i in range(len(normalized) - n + 1)]
