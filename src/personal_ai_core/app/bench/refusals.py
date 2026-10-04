"""Reading the replies the agent loop refused (handoff, Next 6b2).

    python -m personal_ai_core.app.bench.refusals RESULT.jsonl [RESULT.jsonl ...]

Since #193 every agent run records `refused_replies`: each reply the loop
refused, with its text. This module reads them by rules fixed on 2026-10-03,
while the first run that records them was still on the rig -- before any
refused text had been read. A category added after the data is read is a new
rule and says so; it does not replace one of these.

Two readings, both mechanical:

1. A reply that broke the protocol is put in the FIRST category it matches:

   truncated        the model call stopped on the generation limit
                    (done_reason "length"), or the reply opens more braces than
                    it closes
   empty            nothing but whitespace
   prose            no JSON object at all: the model wrote text
   several_objects  valid JSON followed by more ("Extra data"): more than one
                    object in one reply
   malformed_json   any other JSON the parser refused (single quotes, a
                    comment, a missing comma...)
   wrong_shape      a JSON object the protocol does not accept: neither "tool"
                    nor "answer", or a field of the wrong type

   Beside the category, `names_a_tool` says whether the text names one of the
   tools the loop offers: a model that writes "I will use write_file" in prose
   meant to act and missed the format.

2. An answer rejected because no tool had run (action_required) is judged by
   the task's own checks, as if it had been accepted. Nothing had run, so the
   workspace was the task's starting fixture: it is rebuilt, and `judge` runs
   on it with the rejected text as the answer and no tool calls. A PASS is a
   FALSE rejection: the gate refused an answer the task would have accepted.
   That is what the checks say, not whether the answer was grounded -- a
   number guessed right passes `answer_number`. Only tasks whose checks can
   pass without a change to the workspace can produce one.

Read-only: it changes no result file and no score.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .checks import RunEvidence, judge
from .runner import DEFAULT_TASKS, _read
from .tasks import Task, load, materialize

PROTOCOL_CATEGORIES = (
    "truncated", "empty", "prose", "several_objects", "malformed_json", "wrong_shape",
)
# Every tool the agent can be offered (agent/tools.py, agent/web.py); a test
# holds this to their specs. A reply naming one meant to act.
TOOL_NAMES = (
    "read_file", "list_directory", "search_text", "find_files", "write_file",
    "edit_file", "delete_file", "run_command", "shell", "web_search", "fetch_url",
)


def classify_protocol_error(text: str, error: str | None, done_reason: str | None = None) -> str:
    """The first category of the module docstring's list that the reply matches."""
    if done_reason == "length" or text.count("{") > text.count("}"):
        return "truncated"
    if not text.strip():
        return "empty"
    error = error or ""
    if "contains no JSON object" in error:
        return "prose"
    if "not valid JSON" in error:
        return "several_objects" if "Extra data" in error else "malformed_json"
    return "wrong_shape"


def names_a_tool(text: str) -> bool:
    return any(name in text for name in TOOL_NAMES)


def would_have_passed(task: Task, answer: str, scratch: Path) -> bool:
    """The task's checks on its starting fixture with `answer` accepted and no
    tool run: whether rejecting it cost the task a pass."""
    workspace = Path(tempfile.mkdtemp(prefix=f"{task.id}-", dir=scratch))
    try:
        materialize(task, workspace)
        passed, _ = judge(RunEvidence(workspace=workspace, answer=answer, calls=()), task.checks)
        return passed
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _answer_text(text: str) -> str:
    """The answer inside a rejected reply: the reply passed the parser, so it is
    the "answer" field of the JSON object it holds. A reply the record clipped
    may no longer parse; then its text, as recorded, is judged."""
    # The loop's parse_reply, restated: `app` may not import `agent` (the
    # layering rule), and a test holds the two to the same answer.
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start:end + 1], strict=False) if 0 <= start < end else None
    except ValueError:
        value = None
    answer = value.get("answer") if isinstance(value, dict) else None
    return answer if isinstance(answer, str) else text


@dataclass
class Reading:
    runs_with_field: int = 0
    runs_without_field: int = 0
    protocol: Counter = field(default_factory=Counter)     # (language, category)
    names_tool: Counter = field(default_factory=Counter)   # of those, naming a tool
    first_reply: Counter = field(default_factory=Counter)  # (language, category) at call 1
    rejected: Counter = field(default_factory=Counter)     # (language, task)
    false_rejections: Counter = field(default_factory=Counter)
    withheld: int = 0                                      # withheld as secret-shaped


def read(lines: Sequence[Mapping[str, Any]], tasks: Mapping[str, Task], scratch: Path) -> Reading:
    reading = Reading()
    for run in lines:
        if run.get("kind") != "run" or run.get("track") != "agent":
            continue
        if "refused_replies" not in run:
            reading.runs_without_field += 1
            continue
        reading.runs_with_field += 1
        language = run["language"]
        calls = run.get("model_calls") or []
        for refused in run["refused_replies"]:
            text = refused["text"]
            if text.startswith("[reply withheld"):
                reading.withheld += 1
                continue
            if refused["kind"] == "protocol_error":
                index = refused["call"] - 1
                done = calls[index].get("done_reason") if 0 <= index < len(calls) else None
                category = classify_protocol_error(text, refused.get("error"), done)
                reading.protocol[(language, category)] += 1
                reading.names_tool[(language, category)] += names_a_tool(text)
                if refused["call"] == 1:
                    reading.first_reply[(language, category)] += 1
            elif refused["kind"] == "action_required":
                reading.rejected[(language, run["task"])] += 1
                if run["task"] in tasks and would_have_passed(
                        tasks[run["task"]], _answer_text(text), scratch):
                    reading.false_rejections[(language, run["task"])] += 1
    return reading


def report(name: str, reading: Reading) -> str:
    out = [f"{name}"]
    if reading.runs_without_field:
        out.append(f"  {reading.runs_without_field} agent run(s) record no refused-reply text "
                   "(a file from before #193)")
    out.append(f"  agent runs with refused-reply text: {reading.runs_with_field}")
    out.append("  protocol errors by category (first reply | all | of which name a tool)")
    for language in ("en", "ar"):
        for category in PROTOCOL_CATEGORIES:
            total = reading.protocol[(language, category)]
            if total:
                out.append(f"    {language}  {category:16} {reading.first_reply[(language, category)]:>4} | "
                           f"{total:>4} | {reading.names_tool[(language, category)]:>4}")
    out.append("  answers rejected for not having acted (rejected | would have passed)")
    by_task: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (language, task), count in sorted(reading.rejected.items()):
        out.append(f"    {language}  {task:24} {count:>4} | "
                   f"{reading.false_rejections[(language, task)]:>4}")
        by_task[language][0] += count
        by_task[language][1] += reading.false_rejections[(language, task)]
    for language, (count, false) in sorted(by_task.items()):
        out.append(f"    {language}  {'all':24} {count:>4} | {false:>4}")
    if reading.withheld:
        out.append(f"  withheld as secret-shaped (not read): {reading.withheld}")
    out.append("Categories and the false-rejection rule were fixed before the data "
               "(module docstring). This reads what was refused; it decides nothing.")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m personal_ai_core.app.bench.refusals",
        description="Read the replies the agent loop refused (handoff, Next 6b2).")
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    args = parser.parse_args(argv)
    out = stdout or __import__("sys").stdout
    tasks = {t.id: t for t in load(args.tasks)}
    with tempfile.TemporaryDirectory(prefix="pac-refusals-", ignore_cleanup_errors=True) as tmp:
        for path in args.files:
            print(report(str(path), read(_read(path), tasks, Path(tmp))), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
