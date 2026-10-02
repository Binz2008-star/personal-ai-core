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
from collections import Counter
from pathlib import Path

from personal_ai_core.app.bench.compare import classify

REPO = Path(__file__).resolve().parents[2]
RESCORED = (
    REPO / "evals" / "results" / "bench"
    / "bench-20261002T081707Z.rescored-bench-checks-v2.jsonl"
)

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
    verification failure. A 'tested after the last edit' gate can touch at most
    the 27 that carry the not_tested_after_edit signal."""
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
