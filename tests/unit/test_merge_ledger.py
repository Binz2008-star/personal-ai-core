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
#
# Deliberately permissive about the SHA token: it captures whatever follows
# the PR number and `_valid_sha` judges it. Matching `[0-9a-f]{7,40}` here
# instead meant a row with a malformed SHA matched nothing and was dropped
# from the ledger entirely -- silently, so a corrupt row read as an absent
# one. Found by a mutation that wrote a 41-character SHA and watched the
# check pass.
LEDGER_ROW = re.compile(r"^  #(\d+)\s+(\S+)", re.MULTILINE)
VALID_SHA = re.compile(r"^[0-9a-f]{7,40}$")
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
    # %H, not %h. An abbreviation cannot be compared exactly against a claim
    # of unknown length, and git chooses the abbreviation length itself.
    for line in _git("log", "--merges", "--format=%H%x00%s").splitlines():
        full, _, subject = line.partition("\x00")
        match = MERGE_SUBJECT.match(subject)
        if match:
            found[int(match.group(1))] = full
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
    malformed = {num: sha for num, sha in rows if not VALID_SHA.match(sha)}
    if malformed:
        pytest.fail(
            "PROJECT_STATE.md has ledger rows whose SHA is not 7-40 hex "
            f"characters: {malformed}. Reported rather than skipped -- a row "
            "that cannot be parsed is a row that is not being checked."
        )
    return {int(num): sha for num, sha in rows}


def mismatched(
    ledger: dict[int, str], merges: dict[int, str]
) -> dict[int, tuple[str, str]]:
    """Ledger rows whose SHA is not a true prefix of the real merge commit.

    Exact to the length of the claim. `merges` holds FULL SHAs and a claim of
    any accepted length must prefix one of them, so a seven-character row is
    validated on all seven and a forty-character row on all forty.

    An earlier version compared `merges[num].startswith(claimed[:7])` against
    an abbreviated SHA, which checked seven characters and no more. A wrong
    forty-character SHA sharing its first seven with the real one passed.
    Review caught it; the mutation round did not, because every mutation it
    tried differed inside those seven.

    A pure function so the cases below can drive it without a repository.
    """
    return {
        num: (claimed, merges[num])
        for num, claimed in ledger.items()
        if num in merges and not merges[num].startswith(claimed)
    }


def test_every_recorded_sha_is_the_real_merge_commit(ledger, merges_in_git):
    """A wrong SHA is a lie in the record, and it has happened.

    The first draft of the ledger gave PR #4 its branch commit (fc9718c) where
    every other row has a merge commit (4e3074b). A mechanical check caught it;
    reading did not.
    """
    wrong = mismatched(ledger, merges_in_git)
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


# --- The comparison itself, driven without a repository -------------------

REAL = "0af4a9442d369b1f422d07daa9754dd780ace3b3"


def test_a_wrong_sha_sharing_the_first_seven_characters_is_caught():
    """The case review found, and the reason %H replaced %h.

    Seven characters of agreement is not agreement. Against an abbreviated
    SHA there was nothing left to disagree with; against the full one there
    are thirty-three more characters, and they are checked.
    """
    liar = "0af4a94" + "d" * 33
    assert liar[:7] == REAL[:7] and liar != REAL
    assert mismatched({17: liar}, {17: REAL}) == {17: (liar, REAL)}


def test_an_honest_abbreviation_still_passes():
    """Seven characters that are true remain true. The fix is not a tightening
    of what the ledger must record, only of how it is compared."""
    assert mismatched({17: REAL[:7]}, {17: REAL}) == {}
    assert mismatched({17: REAL[:12]}, {17: REAL}) == {}
    assert mismatched({17: REAL}, {17: REAL}) == {}


def test_a_sha_differing_in_the_first_seven_is_still_caught():
    """The case the original comparison did catch, kept so the fix is not a
    trade of one blind spot for another."""
    assert mismatched({17: "deadbee"}, {17: REAL}) == {17: ("deadbee", REAL)}


def test_a_pr_git_has_no_merge_for_is_not_reported_here():
    """`mismatched` compares; it does not decide what is missing. That is
    `test_no_recorded_merge_is_invented`, and keeping the two apart means
    neither failure is reported as the other."""
    assert mismatched({99: "abc1234"}, {17: REAL}) == {}


def test_a_malformed_sha_is_reported_rather_than_skipped():
    """A row that cannot be parsed is a row that is not being checked.

    The first version of this file matched the SHA with `[0-9a-f]{7,40}`
    inside the row pattern. A 41-character SHA matched neither that nor the
    surrounding row, so the entry vanished from the ledger and the check
    passed -- and `the_ledger_has_not_fallen_behind` then counted the vanished
    row as merely unrecorded, inside its lag budget. Two assertions agreed
    that nothing was wrong.

    Found by a mutation of this file's own subject, not by review.
    """
    assert VALID_SHA.match("9341e6e")
    assert VALID_SHA.match("9341e6e220b6eaefca395cdb8f6896c76b97856d")
    assert not VALID_SHA.match("9341e6e2" + "d" * 33)   # 41 characters
    assert not VALID_SHA.match("9341e6")                # 6
    assert not VALID_SHA.match("9341g6e")               # not hex

    # And the row pattern now captures such a token rather than ignoring it,
    # which is what lets the fixture fail on it.
    row = "  #12 " + "9341e6e2" + "d" * 33 + "  subject\n"
    assert LEDGER_ROW.findall(row) == [("12", "9341e6e2" + "d" * 33)]
