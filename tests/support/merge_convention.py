"""Which commits are PR merges into main, read the way the ledger checks read them.

A PR merge's subject is `Merge pull request #N from ...`, GitHub's default.
One merge breaks it: #217 was merged on 2026-10-05 with a custom title, the
lead's mistake. Main's history is not rewritten for it, so that one merge is
named here by its full SHA -- exactly, so no other commit can pass through
the exception. `tools/session_state.py` carries the same table, and
test_merge_ledger.py holds the two equal. Every later merge keeps the
convention.
"""
from __future__ import annotations

import re

MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")
UNCONVENTIONAL_MERGES = {"72ec675a7cf8ec589674c70e7d5c9e53bd3c63e2": 217}


def pr_number(full_sha: str, subject: str) -> int | None:
    """The PR a merge commit landed, or None when it landed no PR."""
    match = MERGE_SUBJECT.match(subject)
    if match:
        return int(match.group(1))
    return UNCONVENTIONAL_MERGES.get(full_sha)
