"""The NEXT SESSION HANDOFF is checked against git, like the merge ledger.

The handoff is what a new session reads first. On 2026-10-01 it still described
main at 5d2b2a3, eleven merges after it was written: nothing read it, so
nothing noticed. Its header names the commit it was written at,

    NEXT SESSION HANDOFF (updated 2026-10-01, main at f363df1)

and this test fails when that commit is not in main's history, or when more
than MAX_HANDOFF_LAG merges have landed since. As with the ledger, a PR cannot
name its own merge, so a small lag is allowed; the next PR updates the header.

What changes on every merge is not written in the handoff at all:
`tools/session_state.py` computes it from git, and `.claude/settings.json`
runs it at the start of every session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from support.lag_limit import effective_limit

REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "PROJECT_STATE.md"
HANDOFF = re.compile(r"^NEXT SESSION HANDOFF \(updated (\S+), main at ([0-9a-f]{7,40})\)",
                     re.MULTILINE)
MAX_HANDOFF_LAG = 3


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def _stated_commit() -> str:
    match = HANDOFF.search(STATE.read_text(encoding="utf-8"))
    if match is None:
        pytest.fail("PROJECT_STATE.md has no 'NEXT SESSION HANDOFF (updated DATE, main at "
                    "SHA)' header; the handoff cannot be checked without one")
    assert len(HANDOFF.findall(STATE.read_text(encoding="utf-8"))) == 1, (
        "more than one handoff header; a session would not know which to read")
    return match.group(2)


def test_the_handoff_names_a_commit_in_this_history():
    if _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        pytest.fail("shallow clone: the handoff's commit cannot be looked up "
                    "(the workflow sets fetch-depth: 0)")
    stated = _stated_commit()
    assert _git("merge-base", "--is-ancestor", stated, "HEAD").returncode == 0, (
        f"the handoff says it was written at {stated}, which is not an ancestor of HEAD")


def _lag_failure(behind: Sequence[str], stated: str, environ: Mapping[str, str]) -> str | None:
    """Why the handoff is too far behind, or None. The limit is one lower on a pull request
    (tests/support/lag_limit.py): its own merge will count once it lands."""
    limit = effective_limit(MAX_HANDOFF_LAG, environ)
    if len(behind) <= limit:
        return None
    return (f"the handoff was written at {stated} and {len(behind)} merges have landed since "
            f"(limit {limit}"
            + (f": {MAX_HANDOFF_LAG} on main, one fewer on a pull request, because this PR's own "
               "merge will count" if limit != MAX_HANDOFF_LAG else "")
            + "). Update NEXT SESSION HANDOFF and its header in this PR; "
            "`python tools/session_state.py` prints what changed.")


def test_the_handoff_is_at_most_a_few_merges_behind_main():
    stated = _stated_commit()
    # Only PR merges count: merging main into a branch is not a landing.
    behind = [line for line in _git("log", "--merges", "--first-parent", "--format=%s",
                                    f"{stated}..HEAD").stdout.splitlines()
              if line.startswith("Merge pull request #")]
    failure = _lag_failure(behind, stated, os.environ)
    assert failure is None, failure


def test_a_pull_request_is_held_one_merge_below_the_limit():
    """The incident of 2026-10-02: at the limit, a PR is green in CI and turns main red when it
    lands. On main the same state is allowed; on a pull request it is not."""
    at_limit = [f"Merge pull request #{n}" for n in range(MAX_HANDOFF_LAG)]
    assert _lag_failure(at_limit, "abc1234", {"GITHUB_EVENT_NAME": "push"}) is None
    assert _lag_failure(at_limit, "abc1234", {}) is None
    failure = _lag_failure(at_limit, "abc1234", {"GITHUB_EVENT_NAME": "pull_request"})
    assert failure is not None and "one fewer on a pull request" in failure
    below = at_limit[:-1]
    assert _lag_failure(below, "abc1234", {"GITHUB_EVENT_NAME": "pull_request"}) is None
    over = [*at_limit, "Merge pull request #99"]
    assert _lag_failure(over, "abc1234", {"GITHUB_EVENT_NAME": "push"}) is not None


def test_every_session_starts_by_computing_the_state():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [hook["command"] for entry in settings["hooks"]["SessionStart"]
                for hook in entry["hooks"]]
    assert any("tools/session_state.py" in c for c in commands)


def test_the_state_script_runs_and_never_fails():
    import os
    import sys

    # Both ends in UTF-8. With PYTHONIOENCODING=utf-8 inherited and a cp1252 locale
    # (the rig's Claude Code shell), the child wrote UTF-8, the parent read cp1252,
    # and the failed decode left stdout None.
    result = subprocess.run([sys.executable, str(REPO / "tools" / "session_state.py")],
                            cwd=REPO, capture_output=True, encoding="utf-8", timeout=60,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0
    assert "handoff:" in result.stdout and "ledger:" in result.stdout
