"""Comparing two benchmark result files (ADR-023 §5): what moved, never a verdict.

    python -m personal_ai_core.app.bench.compare BASELINE CANDIDATE [--tasks evals/bench]
    python -m personal_ai_core.app.bench.compare BASELINE CANDIDATE --unit 1

With `--unit`, the descriptive report is followed by the verdict of ADR-023
amendment 1 for that unit (verdict.py); this module itself still decides nothing.

Descriptive on purpose. ADR-023 §5 sets no target score and requires the rule that
decides "better" to be written down before a comparison is read. This prints the
measures that rule will use and decides nothing.

A failed agent attempt is placed in one class, mechanically from its record
(ADR-023 §1.2). Run on the first baseline it reproduces that table exactly
(52, 43, 14 and 2 of 111, by language); a test pins it. One shape the baseline
could not have is added: an attempt that never acted and had its answers
rejected until the failure budget ended (unit 1).

Transitions pair attempts by (task, language, run number). The run number labels
an attempt; it is not a paired random draw, so a movement is a description and
not a test.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .runner import DEFAULT_TASKS, _read, _task_digest
from .tasks import LANGUAGES, Task, TaskError, load

SUCCESS = "success"
# Order is the order of the ADR-023 §1.2 table; the last three are not in it.
CLASSES: tuple[tuple[str, str], ...] = (
    ("answered_without_executing", "1. answers without executing"),
    ("executed_unverified", "2. executes without adequate verification"),
    ("refused_commands", "3. wrong or unknown environment commands"),
    ("rejected_to_budget", "   rejected until the budget ended, never acted"),
    ("protocol_only", "   protocol only"),
    ("provider_error", "   provider error"),
)
LABELS = dict(CLASSES) | {SUCCESS: "success"}


def classify(run: Mapping[str, Any]) -> str:
    """The class of one agent attempt, from its record alone."""
    if run["success"]:
        return SUCCESS
    if run["stop"] == "error":
        return "provider_error"
    steps = run.get("steps", [])
    executed = any(step["executed"] for step in steps)
    if run["stop"] == "answered" and not executed:
        return "answered_without_executing"
    if executed:
        return "executed_unverified"
    if any(step["decision"] in ("deny", "ask") for step in steps):
        return "refused_commands"
    if run.get("action_rejections"):
        return "rejected_to_budget"
    return "protocol_only"


class Side:
    """One result file, read once."""

    def __init__(self, path: Path | str, lines: Sequence[Mapping[str, Any]]) -> None:
        if not lines or lines[0].get("kind") != "header":
            raise ValueError(f"{path} has no header")
        self.name = str(path)
        self.header: Mapping[str, Any] = lines[0]
        self.runs = [r for r in lines if r.get("kind") == "run"]
        self.end: Mapping[str, Any] = next((r for r in lines if r.get("kind") == "end"), {})
        self.agent = [r for r in self.runs if r["track"] == "agent"]
        if not self.runs:
            raise ValueError(f"{path} has no runs")

    @property
    def contract(self) -> bool:
        return bool(self.header.get("contract"))

    @property
    def weights(self) -> str:
        recorded = self.end.get("weights")
        if isinstance(recorded, Mapping):
            recorded = recorded.get("digest")
        return str(recorded) if recorded else "not recorded"


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "0/0"


def _languages(sides: Sequence[Side]) -> list[str]:
    found = {r["language"] for s in sides for r in s.runs}
    return [*(lang for lang in LANGUAGES if lang in found), *sorted(found - set(LANGUAGES))]


def _pair(label: str, a: Any, b: Any, width: int = 36) -> str:
    return f"  {label:10} {str(a)[:width]:{width}} | {str(b)[:width]}"


def _instrument(base: Side, cand: Side, tasks: Mapping[str, Task] | None) -> list[str]:
    """What has to be the same for a comparison to mean something (ADR-023 §5)."""
    out = ["Instrument (baseline | candidate)"]
    warnings: list[str] = []
    for key in ("model", "scorer", "runs", "languages", "num_ctx_measured_by_owner"):
        a, b = base.header.get(key), cand.header.get(key)
        out.append(_pair(key.replace("_measured_by_owner", ""), a, b))
        if a != b:
            warnings.append(f"{key.replace('_measured_by_owner', '')} differs")
    out.append(_pair("commit", str(base.header.get("commit"))[:12],
                     str(cand.header.get("commit"))[:12]))
    out.append(_pair("contract", "passed" if base.contract else "none",
                     "passed" if cand.contract else "none"))
    out.append(_pair("weights", base.weights, cand.weights))
    if base.weights != cand.weights or base.weights == "not recorded":
        warnings.append("weights differ or are not recorded")

    a_tasks, b_tasks = base.header.get("tasks", {}), cand.header.get("tasks", {})
    if set(a_tasks) != set(b_tasks):
        warnings.append(f"task sets differ: only in baseline {sorted(set(a_tasks) - set(b_tasks))}, "
                        f"only in candidate {sorted(set(b_tasks) - set(a_tasks))}")
    same = explained = 0
    unexplained: list[str] = []
    for task_id in sorted(set(a_tasks) & set(b_tasks)):
        if a_tasks[task_id]["digest"] == b_tasks[task_id]["digest"]:
            same += 1
            continue
        task = tasks.get(task_id) if tasks is not None else None
        if (task is not None and task.action_required is not None
                and a_tasks[task_id]["digest"] == _task_digest(task, with_contract=False)
                and b_tasks[task_id]["digest"] == _task_digest(task)):
            explained += 1
        else:
            unexplained.append(task_id)
    out.append(f"  task digests: {same} identical, {explained} differ only by the contract "
               f"(ADR-023 §8.3), {len(unexplained)} differ and the contract does not explain it")
    out.extend(f"    not explained: {task_id}" for task_id in unexplained)
    if tasks is None:
        out.append("  (the task files could not be loaded, so a changed digest is not verified)")
    if unexplained:
        warnings.append("some tasks are not the same tasks")
    out.extend(f"  WARNING: {w}" for w in warnings)
    return out


def _attempts(base: Side, cand: Side) -> list[str]:
    out = ["Agent attempts", f"  {'':30}{'baseline':>22}{'candidate':>22}"]
    for language in [*_languages((base, cand)), "all"]:
        stats = []
        for side in (base, cand):
            runs = [r for r in side.agent if language in ("all", r["language"])]
            acted = sum(any(s["executed"] for s in r.get("steps", [])) for r in runs)
            stats.append((runs, acted))
        for label, pick in (
                ("success", lambda runs, acted: sum(r["success"] for r in runs)),
                ("acted (>=1 tool executed)", lambda runs, acted: acted),
                ("stopped on the budget", lambda runs, acted: sum(r["stop"] == "budget"
                                                                  for r in runs))):
            out.append(f"  {language:4}{label:26}" + "".join(
                f"{_rate(pick(runs, acted), len(runs)):>22}" for runs, acted in stats))
        cells = []
        for side, (runs, _) in zip((base, cand), stats):
            rejected = sum(r.get("action_rejections") or 0 for r in runs)
            attempts = sum(bool(r.get("action_rejections")) for r in runs)
            cells.append(f"{attempts} att, {rejected} rej" if side.contract else "no contract")
        out.append(f"  {language:4}{'answers rejected':26}" + "".join(f"{c:>22}" for c in cells))
    return out


def _classes(base: Side, cand: Side) -> list[str]:
    out = ["Failed agent attempts by class (ADR-023 §1.2)",
           f"  {'':50}{'baseline':>22}{'candidate':>22}"]
    languages = _languages((base, cand))
    counts = [Counter((classify(r), r["language"]) for r in s.agent if not r["success"])
              for s in (base, cand)]
    for key, label in CLASSES:
        totals = [sum(c[(key, lang)] for lang in languages) for c in counts]
        if not any(totals):
            continue
        cells = [f"{t} (" + " ".join(f"{lang}={c[(key, lang)]}" for lang in languages) + ")"
                 for t, c in zip(totals, counts)]
        out.append(f"  {label:50}" + "".join(f"{cell:>22}" for cell in cells))
    out.append(f"  {'failed, all classes':50}" + "".join(
        f"{sum(not r['success'] for r in s.agent):>22}" for s in (base, cand)))
    return out


def _transitions(base: Side, cand: Side) -> list[str]:
    def key(run: Mapping[str, Any]) -> tuple[str, str, int]:
        return run["task"], run["language"], run["run"]

    mine, theirs = {key(r): r for r in base.agent}, {key(r): r for r in cand.agent}
    shared = sorted(set(mine) & set(theirs))
    moves = Counter((classify(mine[k]), classify(theirs[k])) for k in shared)
    moved = sum(n for (a, b), n in moves.items() if a != b)
    out = [f"Transitions, agent attempts paired by (task, language, run): {len(shared)} paired, "
           f"{moved} changed class"]
    for (a, b), n in sorted(moves.items(), key=lambda kv: (-kv[1], kv[0])):
        if a != b:
            out.append(f"  {n:4}  {LABELS[a].strip()} -> {LABELS[b].strip()}")
    if not moved:
        out.append("  (none)")
    return out


def _falls(base: Side, cand: Side) -> list[str]:
    def successes(side: Side) -> dict[tuple[str, str], tuple[int, int]]:
        table: dict[tuple[str, str], list[int]] = {}
        for run in side.runs:
            cell = table.setdefault((run["task"], run["language"]), [0, 0])
            cell[0] += bool(run["success"])
            cell[1] += 1
        return {k: (v[0], v[1]) for k, v in table.items()}

    a, b = successes(base), successes(cand)
    shared = sorted(set(a) & set(b))
    falls = [k for k in shared if b[k][0] / b[k][1] < a[k][0] / a[k][1]]
    rises = [k for k in shared if b[k][0] / b[k][1] > a[k][0] / a[k][1]]
    out = [f"Success per task and language, both tracks: {len(falls)} fell, {len(rises)} rose "
           f"(samples of {base.header.get('runs')} runs; a fall of one run is within noise "
           f"until the deciding rule says otherwise)"]
    for task, language in falls:
        (s0, n0), (s1, n1) = a[(task, language)], b[(task, language)]
        out.append(f"  fell  {task:28} {language}  {s0}/{n0} -> {s1}/{n1}")
    return out


def compare(base: Side, cand: Side, tasks: Mapping[str, Task] | None = None) -> str:
    sections = [
        [f"baseline : {base.name}", f"candidate: {cand.name}", ""],
        _instrument(base, cand, tasks), [""],
        _attempts(base, cand), [""],
        _classes(base, cand), [""],
        _transitions(base, cand), [""],
        _falls(base, cand), [""],
        ["Not computed here",
         "  false rejections: counted by `--unit` (the verdict) and by refusals.py, from the",
         "    refused-reply text a file records since #193.",
         "  evidence-backed completion and verification rate: no control builds them yet (§2.3).",
         "",
         "This prints what changed. It does not say whether the change is an improvement:",
         "ADR-023 §5 sets no target score and fixes the deciding rule before a comparison is read."],
    ]
    return "\n".join(line for section in sections for line in section)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m personal_ai_core.app.bench.compare",
                                     description="Compare two benchmark result files (ADR-023 §5).")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS,
                        help="the task files, to check that a changed digest is only the contract")
    # Imported here: verdict.py builds on this module.
    from .verdict import EXIT_CODES, TARGETS, decide, report

    parser.add_argument("--unit", choices=sorted(TARGETS),
                        help="also print the verdict of ADR-023 amendment 1 for this unit, "
                             "whose target class it declared (exit 0 PASS, 1 FAIL, "
                             "3 NOT READABLE)")
    args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        base = Side(args.baseline, _read(args.baseline))
        cand = Side(args.candidate, _read(args.candidate))
    except (OSError, ValueError) as exc:
        print(f"refusing to compare: {exc}", file=out)
        return 2
    try:
        tasks: Mapping[str, Task] | None = {t.id: t for t in load(args.tasks)}
    except (OSError, TaskError, ValueError):
        tasks = None
    print(compare(base, cand, tasks), file=out)
    if args.unit is None:
        return 0
    verdict = decide(base, cand, args.unit, tasks)
    print("\n" + report(verdict), file=out)
    return EXIT_CODES[verdict.outcome]


if __name__ == "__main__":
    raise SystemExit(main())
