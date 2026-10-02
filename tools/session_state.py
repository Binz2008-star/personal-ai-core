"""What a new session needs to know that git can tell it. Read-only.

    python tools/session_state.py

Runs at the start of every Claude Code session (`.claude/settings.json`,
SessionStart), so a session starts from the repository's real state rather
than from the last thing someone wrote down. It prints:

- the current commit and branch, and whether the tree is clean;
- the latest merges into main;
- the merges the PROJECT_STATE.md ledger does not record yet, against the
  limit `tests/unit/test_merge_ledger.py` enforces;
- how far the NEXT SESSION HANDOFF is behind main, against the limit
  `tests/unit/test_handoff_freshness.py` enforces.

Everything that goes stale is computed here, never written by hand. The
handoff keeps only what git cannot know: decisions, constraints, the next step.

It never fails: a session must start even when git is unavailable or the
clone is shallow. Problems are printed, and the exit code is always 0.
Standard library only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "PROJECT_STATE.md"
LEDGER_ROW = re.compile(r"^  #(\d+)\s+(\S+)", re.MULTILINE)
MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")
HANDOFF = re.compile(r"^NEXT SESSION HANDOFF \(updated (\S+), main at ([0-9a-f]{7,40})\)",
                     re.MULTILINE)
MAX_UNRECORDED_MERGES = 3  # tests/unit/test_merge_ledger.py
MAX_HANDOFF_LAG = 3  # tests/unit/test_handoff_freshness.py
RECENT = 8


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          check=True).stdout.strip()


def merges(rev: str = "HEAD") -> list[tuple[int, str, str]]:
    """(PR number, short SHA, PR title) for each merge of a PR, newest first."""
    out = []
    log = git("log", "--merges", "--first-parent", "--format=%h%x1f%s%x1f%b%x1e", rev)
    for record in log.split("\x1e"):
        parts = record.strip().split("\x1f")
        if len(parts) < 2:
            continue
        match = MERGE_SUBJECT.match(parts[1])
        if match:
            body = parts[2].strip().splitlines() if len(parts) > 2 else []
            out.append((int(match.group(1)), parts[0], body[0] if body else ""))
    return out


def handoff_lag(stated: str) -> int | None:
    """Merges into main since the commit the handoff says it was written at."""
    try:
        git("merge-base", "--is-ancestor", stated, "HEAD")
    except subprocess.CalledProcessError:
        return None
    subjects = git("log", "--merges", "--first-parent", "--format=%s",
                   f"{stated}..HEAD").splitlines()
    # Only PR merges count: merging main into a branch is not a landing.
    return sum(1 for subject in subjects if MERGE_SUBJECT.match(subject))


def report() -> list[str]:
    lines = ["== personal-ai-core: state computed from git at session start =="]
    head = git("log", "-1", "--format=%h %s")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = git("status", "--porcelain")
    lines.append(f"HEAD {head}  (branch {branch}; tree {'DIRTY' if dirty else 'clean'})")
    if git("rev-parse", "--is-shallow-repository") == "true":
        lines.append("note: shallow clone; older merges are not visible "
                     "(git fetch --unshallow for the full ledger check)")

    seen = merges()
    lines.append("latest merges:")
    lines += [f"  #{n} {sha}  {title}" for n, sha, title in seen[:RECENT]]

    text = STATE.read_text(encoding="utf-8")
    recorded = {int(n) for n, _ in LEDGER_ROW.findall(text)}
    unrecorded = sorted(n for n, _, _ in seen if n not in recorded and n >= 3)
    flag = "OVER THE LIMIT, CI fails" if len(unrecorded) > MAX_UNRECORDED_MERGES else "ok"
    lines.append(f"ledger: {len(unrecorded)} merge(s) not recorded "
                 f"{['#%d' % n for n in unrecorded]} (limit {MAX_UNRECORDED_MERGES} on main, "
                 f"{MAX_UNRECORDED_MERGES - 1} for a PR: {flag}); the next PR adds their rows")

    match = HANDOFF.search(text)
    if match is None:
        lines.append("handoff: no 'NEXT SESSION HANDOFF (updated DATE, main at SHA)' header")
    else:
        lag = handoff_lag(match.group(2))
        if lag is None:
            lines.append(f"handoff: written at {match.group(2)}, which is not in this "
                         "history (fetch main, or the header is wrong)")
        else:
            state = "STALE, CI fails" if lag > MAX_HANDOFF_LAG else "ok"
            lines.append(f"handoff: written {match.group(1)} at {match.group(2)}, "
                         f"{lag} merge(s) behind (limit {MAX_HANDOFF_LAG} on main, "
                         f"{MAX_HANDOFF_LAG - 1} for a PR: {state})")
    lines.append("read next: PROJECT_STATE.md, section NEXT SESSION HANDOFF")
    return lines


def main() -> int:
    try:
        print("\n".join(report()))
    except Exception as exc:  # noqa: BLE001 -- a session must start regardless
        print(f"session_state: could not compute the state ({type(exc).__name__}: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
