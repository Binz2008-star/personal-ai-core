"""ADR-019 unit 1: does a reply keep to the language of the user's message?

    check_reply(user_text, reply_text, evidence=()) -> Verdict

Pure: no I/O, no model, no clock, no state. Letters are counted by Unicode
script; nothing here guesses a language. Unit 2 decides what to do with a
violation; nothing in this module knows a service exists.

The thresholds are ADR-019 §3.1, decision D2, and are parameters so the owner's
decision can change them without touching the logic.

Known limits (ADR-019 §4): it catches scripts, not languages -- Persian in
reply to Arabic, or French in reply to English, passes. The exemption list is
lexical.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

# ADR-019 §3.2: appended once, as a system message, when a draft fails the
# check. Names no language, as ADR-012 amendment 1 requires of the rule itself.
GUARD_NOTE = (
    "Your previous draft was not written in the language of the user's message. "
    "Write the whole reply again in the language of the user's message."
)

ARABIC = "arabic"
LATIN = "latin"
HAN = "han"
KANA = "kana"
HANGUL = "hangul"
CYRILLIC = "cyrillic"
OTHER = "other"

# Only these are expected scripts in version 1 (§3.1).
EXPECTABLE: tuple[str, ...] = (ARABIC, LATIN)

_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    (ARABIC, ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
              (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    (LATIN, ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F))),
    (HAN, ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    (KANA, ((0x3040, 0x30FF),)),
    (HANGUL, ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
    (CYRILLIC, ((0x0400, 0x04FF),)),
)

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

# A Latin run for the quoting rule below: Latin letters, digits and the
# punctuation of identifiers, URLs and config lines, joined by spaces or tabs.
# One run is the longest stretch of the reply with no other script in it.
_LATIN_RUN = re.compile(
    r"[A-Za-z0-9\u00C0-\u024F_\-:/@.=\[\]]*[A-Za-z\u00C0-\u024F][A-Za-z0-9\u00C0-\u024F_\-:/@.=\[\]]*"
    r"(?:[ \t]+[A-Za-z0-9\u00C0-\u024F_\-:/@.=\[\]]*[A-Za-z\u00C0-\u024F][A-Za-z0-9\u00C0-\u024F_\-:/@.=\[\]]*)*"
)


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


# §3.4: the user asking for another language turns the guard off for the turn.
_EXEMPT = re.compile(
    r"\btranslat\w*|\bin\s+(?:english|arabic|chinese|french|german|spanish"
    r"|japanese|korean|russian|turkish|urdu|persian|hindi)\b"
    r"|ترجم|بالإنجليزي|بالانجليزي|بالإنكليزي|بالانكليزي|بالصيني|بالفرنسي"
    r"|بالألماني|بالإسباني|بالياباني|بالكوري|بالروسي|بالتركي|بالأردي|بالفارسي"
    r"|باللغة",
    re.IGNORECASE,
)


def script_of(ch: str) -> str | None:
    """The script of one letter, or None for a non-letter."""
    if not ch.isalpha():
        return None
    code = ord(ch)
    for name, ranges in _RANGES:
        for low, high in ranges:
            if low <= code <= high:
                return name
    return OTHER


def letter_counts(text: str) -> dict[str, int]:
    """Letters per script, outside fenced code blocks."""
    counts: Counter[str] = Counter()
    for ch in _FENCE.sub(" ", text):
        script = script_of(ch)
        if script is not None:
            counts[script] += 1
    return dict(counts)


@dataclass(frozen=True)
class Verdict:
    """What the check found. `violation` is the only field unit 2 acts on;
    the rest is what ADR-019 §3.3 records, counts only."""

    violation: bool
    reason: str
    expected: str | None = None
    reply_counts: Mapping[str, int] = field(default_factory=dict)
    exempt: str | None = None


def check_reply(
    user_text: str,
    reply_text: str,
    evidence: Sequence[str] = (),
    *,
    min_user_letters: int = 8,
    dominant_share: float = 0.6,
    foreign_letters: int = 3,
    min_arabic_share: float = 0.5,
) -> Verdict:
    """ADR-019 §3.1 and §3.4.

    A script present in the user's message or in the turn's evidence is never
    foreign: the user may quote it, and a reply may cite the evidence.
    """
    reply = letter_counts(reply_text)
    if _EXEMPT.search(user_text):
        return Verdict(False, "exempt: the user asked for another language",
                       reply_counts=reply, exempt="language request")

    user = letter_counts(user_text)
    total = sum(user.values())
    expected = None
    if total >= min_user_letters:
        script, count = max(user.items(), key=lambda item: item[1])
        if script in EXPECTABLE and count / total >= dominant_share:
            expected = script
    if expected is None:
        return Verdict(False, "undetermined: no expected script", reply_counts=reply)

    present = set(user)
    for text in evidence:
        present |= set(letter_counts(text))
    # Latin is never foreign: names, code and product names appear in replies
    # in any language, and an Arabic reply's Latin is governed by its share
    # below. Arabic IS foreign in a reply to a Latin message.
    allowed = present | {expected, LATIN, OTHER}
    foreign = {s: n for s, n in reply.items() if s not in allowed}
    foreign_total = sum(foreign.values())
    if foreign_total >= foreign_letters:
        names = ", ".join(sorted(foreign))
        return Verdict(True, f"{foreign_total} letters in {names}",
                       expected=expected, reply_counts=reply)

    if expected == ARABIC:
        # A Latin run copied verbatim from the user's message or the evidence
        # is quoted, not a language switch: "copy the DATABASE_URL line as it
        # is" is answered by a line with no Arabic in it (secret-ar, every rig
        # run of 2026-10-01). Verbatim spans, not a bag of words: an English
        # sentence built from words the evidence happens to contain ("the",
        # "is", "notes") is still English the reply wrote itself.
        sources = [_normalise(text) for text in (user_text, *evidence)]
        own = letter_counts(_LATIN_RUN.sub(
            lambda m: " " if any(_normalise(m.group(0)) in src for src in sources)
            else m.group(0),
            reply_text,
        ))
        letters = sum(own.values())
        share = own.get(ARABIC, 0) / letters if letters else 1.0
        if share < min_arabic_share:
            return Verdict(True, f"arabic share {share:.2f}",
                           expected=expected, reply_counts=reply)

    return Verdict(False, "ok", expected=expected, reply_counts=reply)
