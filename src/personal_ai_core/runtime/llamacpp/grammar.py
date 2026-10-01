"""GBNF grammars for llama-server.

`NO_FOREIGN_SCRIPT` forbids the scripts every rig failure of 2026-10-01 was
made of: CJK punctuation, Kana, CJK ideographs, fullwidth forms, Hangul and
Cyrillic. Everything else -- Arabic, Latin, digits, punctuation, newlines --
is allowed, so it cannot change what a correct Arabic or English reply says.
It would also forbid a reply the user asked for in Chinese: an experiment
grammar, not a product rule.
"""
from __future__ import annotations

NO_FOREIGN_SCRIPT = (
    r"root ::= [^\u3000-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\uFF00-\uFFEF\uAC00-\uD7AF\u1100-\u11FF\u0400-\u04FF]*"
)

GRAMMARS: dict[str, str | None] = {
    "none": None,
    "no-foreign-script": NO_FOREIGN_SCRIPT,
}
