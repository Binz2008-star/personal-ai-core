"""The limit the merge-ledger and handoff-freshness gates apply, per kind of run.

Both gates allow main to be at most MAX merges behind its own record: a PR cannot
write its own merge into PROJECT_STATE.md, because that merge does not exist yet
when the diff is written, so a small lag has to be tolerated.

That allowance has a trap, and it was sprung on 2026-10-02. On a `pull_request` run
the PR's own merge is not in git either, so a PR that finds main exactly at MAX is
green in CI, and the moment it lands main is at MAX + 1 and red. #176 did exactly
that: after #174 the ledger and the handoff were 3 merges behind, #176's CI passed,
and its merge turned main red until a fix landed.

So the limit depends on what is being checked:

  push to main, or a local run   MAX      the state main is actually in
  pull_request                   MAX - 1  the state main WILL be in once this PR lands

A PR that finds the record at MAX - 1 or fewer behind is fine; one that finds it at
MAX must refresh the header and the ledger itself, which is what the repository's
own rule has always asked ("update this header in any PR that finds it 3 merges
behind"). The rule is now enforced at the one place it can be.

GitHub sets GITHUB_EVENT_NAME on every Actions run, on Linux and Windows alike.
"""
from __future__ import annotations

import os
from typing import Mapping

PULL_REQUEST = "pull_request"


def effective_limit(maximum: int, environ: Mapping[str, str] | None = None) -> int:
    """`maximum`, or one less when the run is for a pull request."""
    environment = os.environ if environ is None else environ
    return maximum - 1 if environment.get("GITHUB_EVENT_NAME") == PULL_REQUEST else maximum
