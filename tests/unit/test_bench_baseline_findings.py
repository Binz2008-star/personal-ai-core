"""What the baseline's agent failures say about where a control can and cannot help.

ADR-023 section 1.2 partitions the 111 failed agent runs into four classes and
`test_bench_compare.py` pins that partition. This file pins a second reading of
the same records, the one that decides the ORDER of the controls: a large share
of runs spent their whole failure budget on shell calls the benchmark policy
refused, before any question of verification arose.

Everything here is derived from the immutable rescored baseline, and the
numbers are the ones the handoff quotes. Written because a figure in a
document with nothing re-deriving it is how a guard quietly stops guarding.

These are counts of what was recorded. They do not say what the model would
have done if told otherwise, and so they are not a forecast of any control.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from personal_ai_core.agent.environment import resolve_test_command
from personal_ai_core.app.bench import checks as bench_checks
from personal_ai_core.app.bench.checks import RunEvidence, ToolCall
from personal_ai_core.app.bench.compare import classify
from personal_ai_core.app.bench.tasks import load

REPO = Path(__file__).resolve().parents[2]
RESCORED = (
    REPO / "evals" / "results" / "bench"
    / "bench-20261002T081707Z.rescored-bench-checks-v2.jsonl"
)
TASKS = REPO / "evals" / "bench"

# A shell call the benchmark's containment policy did not approve: the tool
# asked for confirmation, nobody gave it, nothing ran. Each is one failure
# against the budget of three (ADR-023 section 2.4).
OTHER_RUNNER = re.compile(r"unittest|dotnet|mstest|debugpy|debugtools|debug\.py|nose|tox")


def _agent_runs() -> list[dict]:
    lines = [json.loads(x) for x in RESCORED.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in lines if r.get("kind") == "run" and r["track"] == "agent"]


def _refused_shell(run: dict) -> list[dict]:
    return [s for s in run["steps"] if s["tool"] == "shell" and not s.get("executed")]


def test_there_are_120_agent_runs_and_111_failures():
    runs = _agent_runs()
    assert len(runs) == 120
    assert sum(not r["success"] for r in runs) == 111


def test_a_quarter_of_the_agent_runs_were_refused_a_shell_call_and_none_passed():
    """73 refused calls in 31 runs; every one of the 31 failed."""
    runs = _agent_runs()
    affected = [r for r in runs if _refused_shell(r)]
    assert sum(len(_refused_shell(r)) for r in runs) == 73
    assert len(affected) == 31
    assert sum(r["success"] for r in affected) == 0


def test_most_budget_stops_were_refused_shell_calls():
    """26 of the 33 runs the budget stopped had at least one; 19 spent all three."""
    runs = _agent_runs()
    stopped = [r for r in runs if r["stop"] == "budget"]
    assert len(stopped) == 33
    assert sum(1 for r in stopped if _refused_shell(r)) == 26
    assert sum(1 for r in runs if len(_refused_shell(r)) == 3) == 19


def test_two_thirds_of_the_refused_calls_named_a_runner_other_than_pytest():
    """50 of 73: unittest, dotnet, mstest, a debugger. The thing section 2.1
    tells the model ("the supported test command is pytest; do not choose
    another"); whether the model heeds it is what the measurement is for."""
    commands = [
        s["arguments"].get("command", "")
        for r in _agent_runs()
        for s in _refused_shell(r)
    ]
    assert len(commands) == 73
    assert sum(1 for c in commands if OTHER_RUNNER.search(c)) == 50


def test_class_two_is_not_one_problem():
    """43 runs 'executed without adequate verification': 17 stopped on the
    budget (so never reached a final answer) and 26 answered. Of the 17, 10
    neither edited a file nor ran one successful tool call: that is not a
    verification failure.

    The not_tested_after_edit signal is on 27 of the 43, and 27 is NOT the reach
    of a 'tested after the last edit' gate: the signal also fires for a run that
    only ran a shell command (a test run counts as 'editing' to the signal) and
    for a task whose workspace has no test command at all. The next test counts
    what such a gate could actually touch."""
    class_two = [r for r in _agent_runs() if classify(r) == "executed_unverified"]
    assert len(class_two) == 43
    assert Counter(r["stop"] for r in class_two) == {"answered": 26, "budget": 17}

    def edited(run: dict) -> bool:
        writes = ("write_file", "edit_file", "append_file", "delete_file", "move_file")
        return any(s["tool"] in writes and s.get("ok") for s in run["steps"])

    def any_ok(run: dict) -> bool:
        return any(s.get("executed") and s.get("ok") for s in run["steps"])

    budget = [r for r in class_two if r["stop"] == "budget"]
    assert sum(1 for r in budget if not edited(r) and not any_ok(r)) == 10
    assert sum("not_tested_after_edit" in r["signals"] for r in class_two) == 27


def _evidence(run: dict) -> RunEvidence:
    calls = tuple(
        ToolCall(
            tool=s["tool"],
            arguments=s["arguments"],
            decision=s.get("decision", ""),
            executed=bool(s.get("executed")),
            ok=bool(s.get("ok")),
        )
        for s in run["steps"]
    )
    return RunEvidence(workspace=Path("."), answer=run.get("answer"), calls=calls)


def _tasks_with_a_supported_test_command() -> set[str]:
    """Agent tasks whose starting workspace has a test command the program
    would offer (unit 2's resolver, which reads files and runs nothing)."""
    found: set[str] = set()
    for task in load(TASKS):
        if task.track != "agent":
            continue
        scratch = Path(tempfile.mkdtemp(prefix="pac-findings-"))
        try:
            workspace = scratch
            if task.fixture is not None and Path(task.fixture).exists():
                workspace = scratch / "w"
                shutil.copytree(task.fixture, workspace)
            if resolve_test_command(workspace) is not None:
                found.add(task.id)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return found


def test_a_tested_after_edit_gate_would_reach_seven_failures_not_twenty_seven():
    """Of 120 agent runs, 27 made a real edit (one that is not a test run). A
    gate that refuses an answer until a test has run after the last edit can act
    only where (a) the run answered, (b) it edited, (c) no test ran after the
    last edit, and (d) the workspace has a test command to run. That is 6 runs,
    5 of them failures; a seventh failure answered over a test that had run and
    failed, which needs a rule about outcomes, not just about having run one.
    So at most 7 of the 111 failures are within reach: 91 never edited anything
    (units 1 and 2), and 9 edited in a task with no test command (they need
    evidence of the file or commit, which is the structured claim)."""
    has_command = _tasks_with_a_supported_test_command()
    assert has_command == {
        "codegen-slugify", "debug-word-count", "modify-discount-cap",
        "tests-count-failures", "verify-off-by-one", "verify-sales-total",
    }
    runs = _agent_runs()
    detail = {id(r): bench_checks.tested_after_last_edit(_evidence(r))[1] for r in runs}
    real_edit = [r for r in runs if detail[id(r)] != "no edit was made"]
    assert len(real_edit) == 27
    assert sum(1 for r in real_edit if r["stop"] == "budget") == 4

    untested = [r for r in real_edit
                if r["stop"] == "answered"
                and detail[id(r)] == "no test run after the last edit"]
    assert len(untested) == 21
    reachable = [r for r in untested if r["task"] in has_command]
    assert len(reachable) == 6
    assert sum(1 for r in reachable if not r["success"]) == 5
    over_a_failing_test = [
        r for r in real_edit
        if detail[id(r)] == "the last test run after the edit failed"
    ]
    assert len(over_a_failing_test) == 2 and not any(r["success"] for r in over_a_failing_test)

    never_edited_failures = [
        r for r in runs
        if detail[id(r)] == "no edit was made" and not r["success"]
    ]
    assert len(never_edited_failures) == 91
    no_command_failures = [
        r for r in untested if r["task"] not in has_command and not r["success"]
    ]
    assert len(no_command_failures) == 9
