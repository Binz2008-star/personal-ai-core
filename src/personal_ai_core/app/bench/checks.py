"""Mechanical checks for a finished benchmark run (ADR-022 §3.3). No model.

A check reads the evidence a run leaves behind:

- the workspace as the agent left it;
- the answer;
- the tool calls in order, from the audit log.

Commands a check runs (`command_passes`, the git checks) run after the agent has
stopped, outside its control, without a shell and with a timeout.

Every check returns (verdict, detail), with verdict PASS or FAIL. A task succeeds
only when every one of its checks passes.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..evaluate import FAIL, PASS, _fold, check_declines

COMMAND_TIMEOUT_SECONDS = 120
# Tools that change files; a test run must come after the last of them.
EDITING_TOOLS = frozenset({"write_file", "delete_file", "shell"})
TESTING = re.compile(r"(^|\s)(pytest|python\s+-m\s+pytest)(\s|$)")


@dataclass(frozen=True)
class ToolCall:
    """One audited tool call, reduced to what the checks and the record need."""

    tool: str
    arguments: Mapping[str, Any]
    decision: str  # allow / deny / ask
    executed: bool
    ok: bool


@dataclass(frozen=True)
class RunEvidence:
    workspace: Path
    answer: str | None
    calls: Sequence[ToolCall] = field(default_factory=tuple)


Check = Callable[..., tuple[str, str]]


def _inside(workspace: Path, relative: str) -> Path:
    target = (workspace / relative).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ValueError(f"check path escapes the workspace: {relative}")
    return target


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _normal(text: str) -> str:
    """Line endings and trailing whitespace do not decide a file's content."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


def file_exists(ev: RunEvidence, *, path: str) -> tuple[str, str]:
    return (PASS, "exists") if _inside(ev.workspace, path).is_file() else (FAIL, f"no {path}")


def file_absent(ev: RunEvidence, *, path: str) -> tuple[str, str]:
    return (FAIL, f"{path} still exists") if _inside(ev.workspace, path).exists() else (
        PASS, "absent")


def file_contains(ev: RunEvidence, *, path: str, text: str) -> tuple[str, str]:
    content = _read(_inside(ev.workspace, path))
    if content is None:
        return FAIL, f"cannot read {path}"
    return (PASS, "contains") if text in content else (FAIL, f"{path} lacks {text!r}")


def file_equals(ev: RunEvidence, *, path: str, text: str) -> tuple[str, str]:
    content = _read(_inside(ev.workspace, path))
    if content is None:
        return FAIL, f"cannot read {path}"
    return (PASS, "equal") if _normal(content) == _normal(text) else (FAIL, f"{path} differs")


def interpreter(argv: Sequence[str]) -> list[str]:
    """`python` in a task means the interpreter running the benchmark, on every OS."""
    argv = list(argv)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv


def _run(ev: RunEvidence, argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(interpreter(argv), cwd=ev.workspace, capture_output=True, text=True,
                              timeout=COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def command_passes(ev: RunEvidence, *, argv: Sequence[str]) -> tuple[str, str]:
    """The fixture's own check, run after the agent stopped: exit status 0."""
    done = _run(ev, argv)
    if done is None:
        return FAIL, f"{' '.join(argv)}: could not run or timed out"
    tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
    return (PASS, tail[0][:200]) if done.returncode == 0 else (
        FAIL, f"exit {done.returncode}: {tail[0][:200]}")


def git_log_contains(ev: RunEvidence, *, text: str) -> tuple[str, str]:
    done = _run(ev, ["git", "log", "-n", "20", "--format=%s"])
    if done is None or done.returncode != 0:
        return FAIL, "no git history"
    return (PASS, "in the log") if text in done.stdout else (FAIL, f"no commit with {text!r}")


def git_clean(ev: RunEvidence) -> tuple[str, str]:
    done = _run(ev, ["git", "status", "--porcelain"])
    if done is None or done.returncode != 0:
        return FAIL, "not a git repository"
    return (PASS, "clean") if not done.stdout.strip() else (FAIL, "uncommitted changes")


_TASHKEEL = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_ARABIC_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})


def fold_answer(text: str) -> str:
    """Case, Unicode form, and the Arabic spellings a correct answer varies in:
    diacritics and tatweel dropped, hamza-on-alef, alef maqsura and ta marbuta
    unified. A fact is matched, not a spelling."""
    return _TASHKEEL.sub("", _fold(text)).translate(_ARABIC_FORMS)


def answer_contains(ev: RunEvidence, *, any_of: Sequence[str]) -> tuple[str, str]:
    """One of the accepted spellings is in the answer (see `fold_answer`)."""
    if not ev.answer:
        return FAIL, "no answer"
    folded = fold_answer(ev.answer)
    hit = next((t for t in any_of if fold_answer(t) in folded), None)
    return (PASS, f"contains {hit!r}") if hit else (FAIL, f"none of {list(any_of)}")


_NUMBER = re.compile(r"(?<![\d.])\d+(?![\d.])")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def answer_number(ev: RunEvidence, *, value: int) -> tuple[str, str]:
    """The answer states this number (Arabic-Indic digits count)."""
    if not ev.answer:
        return FAIL, "no answer"
    numbers = {int(n) for n in _NUMBER.findall(ev.answer.translate(_ARABIC_DIGITS))}
    return (PASS, f"states {value}") if value in numbers else (
        FAIL, f"states {sorted(numbers)[:6]}, not {value}")


def cites(ev: RunEvidence, *, document: str) -> tuple[str, str]:
    """The answer names the corpus document it drew on, by file name or stem."""
    if not ev.answer:
        return FAIL, "no answer"
    folded = _fold(ev.answer)
    names = {document, Path(document).stem}
    return (PASS, f"cites {document}") if any(_fold(n) in folded for n in names) else (
        FAIL, f"does not name {document}")


def declines(ev: RunEvidence, *, forbid: str | None = None) -> tuple[str, str]:
    """The evidence does not hold the answer and the reply says so (ADR-013 rule 4).

    The harness's own check, where REVIEW (a decline in words it does not know) is
    not a pass.
    """
    if not ev.answer:
        return FAIL, "no answer"
    verdict, detail = check_declines(ev.answer, forbid=forbid)
    return (PASS, detail) if verdict == PASS else (FAIL, detail)


def tool_used(ev: RunEvidence, *, tool: str) -> tuple[str, str]:
    used = any(c.tool == tool and c.executed for c in ev.calls)
    return (PASS, f"{tool} ran") if used else (FAIL, f"{tool} never ran")


def tool_not_used(ev: RunEvidence, *, tool: str) -> tuple[str, str]:
    used = any(c.tool == tool and c.executed for c in ev.calls)
    return (FAIL, f"{tool} ran") if used else (PASS, f"{tool} not used")


def _is_test_run(call: ToolCall) -> bool:
    command = str(call.arguments.get("command", ""))
    return call.tool in ("run_command", "shell") and bool(TESTING.search(command))


def tested_after_last_edit(ev: RunEvidence) -> tuple[str, str]:
    """A test run, executed and successful, after the last change to a file."""
    executed = [c for c in ev.calls if c.executed]
    last_edit = max((i for i, c in enumerate(executed)
                     if c.tool in EDITING_TOOLS and not _is_test_run(c)), default=-1)
    if last_edit < 0:
        return FAIL, "no edit was made"
    after = [c for c in executed[last_edit + 1:] if _is_test_run(c)]
    if not after:
        return FAIL, "no test run after the last edit"
    return (PASS, "tested after the last edit") if after[-1].ok else (
        FAIL, "the last test run after the edit failed")


CHECKS: Mapping[str, Check] = {
    "file_exists": file_exists,
    "file_absent": file_absent,
    "file_contains": file_contains,
    "file_equals": file_equals,
    "command_passes": command_passes,
    "git_log_contains": git_log_contains,
    "git_clean": git_clean,
    "answer_contains": answer_contains,
    "answer_number": answer_number,
    "cites": cites,
    "declines": declines,
    "tool_used": tool_used,
    "tool_not_used": tool_not_used,
    "tested_after_last_edit": tested_after_last_edit,
}


def judge(ev: RunEvidence, checks: Sequence[Mapping[str, Any]]) -> tuple[bool, list[dict[str, str]]]:
    """Every check's verdict, and whether the task succeeded (all PASS)."""
    results = []
    for spec in checks:
        params = {k: v for k, v in spec.items() if k != "type"}
        try:
            verdict, detail = CHECKS[spec["type"]](ev, **params)
        except ValueError as exc:
            verdict, detail = FAIL, str(exc)
        results.append({"check": spec["type"], "verdict": verdict, "detail": detail})
    return all(r["verdict"] == PASS for r in results), results
