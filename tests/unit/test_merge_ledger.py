"""The POST-PHASE-4 MERGES ledger is checked against git, not trusted.

`PROJECT_STATE.md` lists every merge into main since Phase 4, each with its
merge SHA. That list is a claim about the repository, and the reason it exists
is that the previous claim -- "main branch head: bbbf4c3" -- had gone thirteen
merges stale with nothing to notice.

Writing the correct list down does not fix that. Only something that rereads
git does, which is what this file is.

Two failures are possible and both are checked:

  the ledger says something untrue    a wrong SHA, or a PR that never merged
  the ledger has fallen behind        merges exist that it does not record

The second is the original defect. A check that verified only the first would
have passed happily throughout the period this file exists to prevent.

Shallow history
---------------

`actions/checkout@v4` clones with `fetch-depth: 1` by default. In that clone
`git log --merges` returns nothing, and a comparison against nothing passes.
That is the exact shape of guard this repository has now found four times, so
here it FAILS instead -- loudly, naming the fix. `.github/workflows/tests.yml`
sets `fetch-depth: 0` on the job that runs pytest; if that is ever removed,
this test says so rather than going quiet.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "PROJECT_STATE.md"

# `  #12 9341e6e  feat(packaging): ...`
LEDGER_ROW = re.compile(r"^  #(\d+)\s+([0-9a-f]{7,40})\s", re.MULTILINE)
MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")

# The ledger starts after Phase 4; #1 and #2 are the phase merges themselves
# and are recorded in TRACEABILITY instead.
FIRST_LEDGER_PR = 3

# A PR cannot record its own merge -- the merge does not exist when the diff
# is written -- so the ledger is one behind the moment any PR lands. Allowing
# a small lag keeps that from making main red on every merge. It is a lag
# budget, not a licence: the defect this file exists for was thirteen.
MAX_UNRECORDED_MERGES = 3


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def merges_in_git() -> dict[int, str]:
    """PR number -> short merge SHA, from git itself."""
    if _git("rev-parse", "--is-shallow-repository").strip() == "true":
        pytest.fail(
            "the repository is a shallow clone, so `git log --merges` sees no "
            "history and this check would pass without comparing anything. "
            "Set `fetch-depth: 0` on the checkout step of the job that runs "
            "pytest in .github/workflows/tests.yml."
        )

    found: dict[int, str] = {}
    for line in _git("log", "--merges", "--format=%h%x00%s").splitlines():
        short, _, subject = line.partition("\x00")
        match = MERGE_SUBJECT.match(subject)
        if match:
            found[int(match.group(1))] = short
    if not found:
        pytest.fail(
            "no `Merge pull request #N` commits found in git history at all. "
            "Either the history is unavailable or the merge convention "
            "changed; in both cases this check is not comparing anything."
        )
    return found


@pytest.fixture(scope="module")
def ledger() -> dict[int, str]:
    """PR number -> short SHA, as PROJECT_STATE.md claims."""
    rows = LEDGER_ROW.findall(STATE.read_text(encoding="utf-8"))
    if not rows:
        pytest.fail(
            "PROJECT_STATE.md records no merges. The POST-PHASE-4 MERGES "
            "section is the thing this test checks; if it is gone, the record "
            "is back to where it was before it existed."
        )
    return {int(num): sha for num, sha in rows}


def test_every_recorded_sha_is_the_real_merge_commit(ledger, merges_in_git):
    """A wrong SHA is a lie in the record, and it has happened.

    The first draft of the ledger gave PR #4 its branch commit (fc9718c) where
    every other row has a merge commit (4e3074b). A mechanical check caught it;
    reading did not.
    """
    wrong = {
        num: (claimed, merges_in_git[num])
        for num, claimed in ledger.items()
        if num in merges_in_git and not merges_in_git[num].startswith(claimed[:7])
    }
    assert not wrong, (
        "PROJECT_STATE.md records merge SHAs that do not match git "
        f"(pr: recorded -> actual): {wrong}"
    )


def test_no_recorded_merge_is_invented(ledger, merges_in_git):
    """The ledger must not name a PR that never merged."""
    phantom = sorted(set(ledger) - set(merges_in_git))
    assert not phantom, (
        f"PROJECT_STATE.md records merges that git has no record of: {phantom}"
    )


def test_the_ledger_has_not_fallen_behind(ledger, merges_in_git):
    """The original defect: the record stopped while main moved on.

    This is the assertion that would have failed during the period the ledger
    was thirteen merges stale, and the reason a correctness-only check would
    not have been worth writing.
    """
    unrecorded = sorted(
        num for num in merges_in_git if num >= FIRST_LEDGER_PR and num not in ledger
    )
    assert len(unrecorded) <= MAX_UNRECORDED_MERGES, (
        f"PROJECT_STATE.md is {len(unrecorded)} merges behind git: {unrecorded}. "
        "Add them to POST-PHASE-4 MERGES. A ledger that drifts is the defect "
        "that section was written to end."
    )
