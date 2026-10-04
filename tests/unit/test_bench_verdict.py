"""The verdict of ADR-023 amendment 1, applied by code (verdict.py).

The rule was accepted on 2026-10-03 (#191) before any comparison it governs was
run. These tests hold the code to the amendment's text and numbers -- R0's
conditions, R1's declared targets and sign test, R2's threshold of 16 -- on
synthetic result files whose verdict is known by construction, and show that
the 5-run files already on disk get no verdict at all.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from personal_ai_core.app.bench.compare import LABELS, Side, main
from personal_ai_core.app.bench.runner import _read, _task_digest
from personal_ai_core.app.bench.tasks import load
from personal_ai_core.app.bench.verdict import (
    EXIT_CODES,
    REGRESSION_DELTA,
    TARGETS,
    decide,
    report,
    sign_test,
    unreadable,
)
from personal_ai_core.app.gate_calibration import family_false_rejection

REPO = Path(__file__).resolve().parents[2]
AMENDMENT = REPO / "docs" / "ADR" / "ADR-023-plan-execute-verify.md"
RESULTS = REPO / "evals" / "results" / "bench"
AGENT = [f"agent-{i:02}" for i in range(12)]
KNOWLEDGE = [f"kb-{i:02}" for i in range(12)]

Outcome = Callable[[str, str, int], str]


def _all_fail(task: str, language: str, run: int) -> str:
    return "class1" if task in AGENT else "fail"


def _lines(outcome: Outcome = _all_fail, *, runs: int = 10, weights: Any = "sha256:w",
           num_ctx: int | None = 8192, loaded: int = 8192, mismatch: Any = False,
           end: bool = True, digest: str = "d") -> list[dict[str, Any]]:
    header = {"kind": "header", "model": "m", "scorer": "s", "runs": runs,
              "languages": ["en", "ar"], "num_ctx_measured_by_owner": num_ctx, "commit": "c",
              "tasks": {t: {"track": "agent" if t in AGENT else "knowledge", "digest": digest}
                        for t in AGENT + KNOWLEDGE}}
    lines: list[dict[str, Any]] = [header]
    for task in AGENT + KNOWLEDGE:
        for language in ("en", "ar"):
            for run in range(1, runs + 1):
                kind = outcome(task, language, run)
                lines.append({
                    "kind": "run", "task": task, "language": language, "run": run,
                    "track": "agent" if task in AGENT else "knowledge",
                    "success": kind == "success", "stop": "answered",
                    "steps": [{"executed": True, "decision": "allow"}] if kind == "class2" else [],
                    "action_rejections": 0, "protocol_errors": 0, "refused_replies": []})
    if end:
        lines.append({"kind": "end", "weights": weights, "context_mismatch": mismatch,
                      "ollama_loaded": {"context_length": loaded}})
    return lines


def _side(lines: list[dict[str, Any]]) -> Side:
    return Side("f.jsonl", lines)


def _moved(count: int, to: str, *, track_tasks: list[str] = AGENT, language: str = "en",
           otherwise: Outcome = _all_fail) -> Outcome:
    """The first `count` attempts of `track_tasks` in `language` become `to`."""
    chosen = {(t, r) for t in track_tasks for r in range(1, 11)}
    chosen = set(sorted(chosen)[:count])

    def outcome(task: str, lang: str, run: int) -> str:
        return to if lang == language and (task, run) in chosen else otherwise(task, lang, run)
    return outcome


# --- the numbers are the amendment's ----------------------------------------------


def test_the_regression_threshold_is_re_derived_not_copied():
    """D1 (b): 120 attempts per group, 4 groups, family-wise at most 10%."""
    assert family_false_rejection([(120, REGRESSION_DELTA, 4)]) <= 0.10
    assert family_false_rejection([(120, REGRESSION_DELTA - 1, 4)]) > 0.10
    assert round(family_false_rejection([(120, REGRESSION_DELTA, 4)]), 4) == 0.0873


@pytest.mark.parametrize("leaving, entering, p", [
    (0, 0, 1.0), (5, 0, 1 / 32), (9, 1, 11 / 1024), (8, 2, 56 / 1024), (0, 4, 1.0)])
def test_the_sign_test_is_one_sided_and_exact(leaving, entering, p):
    assert sign_test(leaving, entering) == pytest.approx(p)


def test_the_targets_are_the_ones_the_amendment_declared():
    text = " ".join(AMENDMENT.read_text(encoding="utf-8").split())
    assert "unit 1 → class 1, unit 2 → class 3, unit 3 → class 2" in text
    assert {unit: LABELS[cls].split(".")[0] for unit, cls in TARGETS.items()} == {
        "1": "1", "2": "3", "3": "2"}


# --- R4: the three verdicts ----------------------------------------------------------


def test_pass_when_the_target_class_empties_and_nothing_regresses():
    base = _side(_lines())
    cand = _side(_lines(_moved(10, "success")))
    verdict = decide(base, cand, "1", {})
    assert (verdict.leaving, verdict.entering) == (10, 0)
    assert verdict.outcome == "PASS"
    assert "VERDICT: PASS" in report(verdict)


def test_fail_when_the_sign_test_does_not_reach_five_percent():
    """8 leave and 2 enter: p = 0.0547."""
    base = _side(_lines(_moved(2, "success", language="ar")))
    cand = _side(_lines(_moved(8, "success")))
    verdict = decide(base, cand, "1", {})
    assert (verdict.leaving, verdict.entering) == (8, 2)
    assert verdict.outcome == "FAIL" and not verdict.target_holds and not verdict.regressions


@pytest.mark.parametrize("rise, outcome", [(REGRESSION_DELTA, "FAIL"),
                                           (REGRESSION_DELTA - 1, "PASS")])
def test_a_rise_of_sixteen_failures_in_any_group_fails_and_fifteen_does_not(rise, outcome):
    """The knowledge track counts too, and the agent's gain does not buy it back."""
    def knowledge_passes(task: str, language: str, run: int) -> str:
        return "success" if task in KNOWLEDGE else "class1"

    base = _side(_lines(knowledge_passes))
    improved = _moved(20, "success", otherwise=knowledge_passes)
    cand = _side(_lines(_moved(rise, "fail", track_tasks=KNOWLEDGE, language="ar",
                               otherwise=improved)))
    verdict = decide(base, cand, "1", {})
    assert verdict.target_holds
    assert verdict.failures[("knowledge", "ar")] == (0, rise)
    assert verdict.outcome == outcome


def test_an_attempt_moving_from_class_1_to_class_2_has_left_class_1():
    verdict = decide(_side(_lines()), _side(_lines(_moved(10, "class2"))), "1", {})
    assert (verdict.leaving, verdict.entering, verdict.outcome) == (10, 0, "PASS")
    # The same files, read for unit 3 (target class 2): ten entered it. Unit 3
    # is a flag, so its candidate is the run made with it on.
    cand = _lines(_moved(10, "class2"))
    cand[0]["verify_completion"] = True
    verdict = decide(_side(_lines()), _side(cand), "3", {})
    assert (verdict.leaving, verdict.entering, verdict.outcome) == (0, 10, "FAIL")


def test_unit_3_is_read_only_with_verify_completion_off_then_on_and_nothing_else_changed():
    def pair(base_flags: dict[str, bool], cand_flags: dict[str, bool]) -> list[str]:
        base, cand = _lines(), _lines(_moved(10, "success"))
        base[0].update(base_flags)
        cand[0].update(cand_flags)
        return decide(_side(base), _side(cand), "3", {}).unreadable

    assert pair({}, {"verify_completion": True}) == []
    assert pair({}, {}) == ["verify_completion must be off in the baseline and on in the candidate"]
    assert pair({"verify_completion": True}, {"verify_completion": True}) == [
        "verify_completion must be off in the baseline and on in the candidate"]
    for flag in ("environment_context", "lenient_protocol", "native_tools"):
        assert pair({}, {"verify_completion": True, flag: True}) == [f"{flag} differs"]
    # Both arms native, for example, is one condition held equal: readable.
    assert pair({"native_tools": True}, {"verify_completion": True, "native_tools": True}) == []
    # Units 1 and 2 are not flag-gated by this rule.
    base, cand = _lines(), _lines(_moved(10, "success"))
    assert decide(_side(base), _side(cand), "1", {}).unreadable == []


def test_the_unverified_completion_cost_is_reported_only_for_a_run_with_the_check():
    base, cand = _lines(), _lines(_moved(10, "success"))
    labels = [label for label, _, _ in decide(_side(base), _side(cand), "1", {}).costs]
    assert "answers rejected for an unverified completion" not in labels
    cand[0]["verify_completion"] = True
    for line in cand[1:-1]:
        line["verification_rejections"] = 1
    costs = {label: (a, b) for label, a, b in decide(_side(base), _side(cand), "3", {}).costs}
    assert costs["answers rejected for an unverified completion"] == (
        "0 (0.00 per attempt)", "240 (1.00 per attempt)")


# --- R0: what makes a comparison unreadable ----------------------------------------


def _drop_one(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for i, x in enumerate(lines) if i != 1]


def _repeat_one(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*lines[:2], lines[1], *lines[2:]]


@pytest.mark.parametrize("candidate, reason", [
    (_lines(runs=5), "the rule was fixed for 10 per side"),
    (_lines(weights="sha256:other"), "weights digest differs"),
    (_lines(mismatch=True), "num_ctx not confirmed"),
    (_lines(loaded=4096), "num_ctx not confirmed"),
    (_lines(num_ctx=None), "num_ctx_measured_by_owner differs"),
    (_lines(end=False), "no end record"),
    (_drop_one(_lines()), "attempts missing 1"),
    (_repeat_one(_lines()), "extra or repeated 1"),
    (_lines(digest="changed"), "digests differ and the contract does not explain it"),
])
def test_not_readable_and_then_nothing_else_is_computed(candidate, reason):
    verdict = decide(_side(_lines()), _side(candidate), "1", {})
    assert verdict.outcome == "NOT READABLE"
    assert any(reason in problem for problem in verdict.unreadable), verdict.unreadable
    text = report(verdict)
    assert "R1-R3 not computed" in text and "R1 target:" not in text
    assert verdict.failures == {} and verdict.costs == []


def test_unreadable_when_the_baseline_is_the_side_at_fault():
    verdict = decide(_side(_lines(end=False)), _side(_lines()), "1", {})
    # Without its end record the baseline also names no weights.
    assert verdict.unreadable == ["weights digest differs or is not recorded",
                                  "baseline: no end record: the run did not finish "
                                  "(a hang is stopped, never continued by hand)"]


def test_the_five_run_files_on_disk_get_no_verdict():
    """#198's pair, at 5 runs, is NOT READABLE under D1: the rule reads 10-run files."""
    base = Side("u1", _read(RESULTS / "bench-20261003T161348Z.jsonl"))
    cand = Side("u12", _read(RESULTS / "bench-20261003T164631Z.jsonl"))
    verdict = decide(base, cand, "2", None)
    assert verdict.outcome == "NOT READABLE"
    assert sum("the rule was fixed for 10 per side" in p for p in verdict.unreadable) == 2


# --- R3: costs are reported ---------------------------------------------------------


def test_costs_are_reported_and_false_rejections_need_the_recorded_text():
    old = _lines()
    for line in old[1:-1]:
        line.pop("refused_replies")
    verdict = decide(_side(old), _side(_lines(_moved(10, "success"))), "1", {})
    costs = {label: (a, b) for label, a, b in verdict.costs}
    assert costs["false rejections (of rejected answers)"] == (
        "not countable (a file from before #193)", "0 of 0")
    assert costs["agent attempts stopped on the budget"] == ("0", "0")
    assert verdict.outcome == "PASS", "costs never gate"


# --- the command ---------------------------------------------------------------------


def _write(tmp_path: Path, name: str, lines: list[dict[str, Any]]) -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


@pytest.mark.parametrize("candidate, outcome", [
    (_lines(_moved(10, "success")), "PASS"),
    (_lines(), "FAIL"),
    (_lines(runs=5), "NOT READABLE"),
])
def test_the_command_prints_the_verdict_after_the_report_and_exits_by_it(
        tmp_path, candidate, outcome):
    base, cand = _write(tmp_path, "a.jsonl", _lines()), _write(tmp_path, "b.jsonl", candidate)
    out = io.StringIO()
    code = main([str(base), str(cand), "--unit", "1", "--tasks", str(tmp_path)], stdout=out)
    assert code == EXIT_CODES[outcome]
    text = out.getvalue()
    assert text.index("Instrument (baseline | candidate)") < text.index("Verdict by ADR-023")
    assert re.search(rf"VERDICT: {outcome}$", text, re.MULTILINE)


def test_without_unit_the_command_decides_nothing(tmp_path):
    base, cand = _write(tmp_path, "a.jsonl", _lines()), _write(tmp_path, "b.jsonl", _lines())
    out = io.StringIO()
    assert main([str(base), str(cand)], stdout=out) == 0
    assert "VERDICT" not in out.getvalue()


def test_a_unit_without_a_declared_target_has_no_verdict(tmp_path):
    """ADR-024's units have none: a target chosen after the data is not R1, and only a
    separately authorized amendment, before the runs, could declare one."""
    base, cand = _write(tmp_path, "a.jsonl", _lines()), _write(tmp_path, "b.jsonl", _lines())
    with pytest.raises(SystemExit):
        main([str(base), str(cand), "--unit", "A"], stdout=io.StringIO())


def test_a_digest_that_differs_only_by_the_contract_is_readable_and_any_other_is_not():
    """ADR-023 §8.3: a task file that gained only its action_required line."""
    tasks = {t.id: t for t in load(REPO / "evals" / "bench")}
    task = tasks["git-commit-release"]
    assert task.action_required is not None
    before, after = _task_digest(task, with_contract=False), _task_digest(task)

    def with_task(digest: str) -> Side:
        lines = _lines()
        lines[0]["tasks"][task.id] = {"track": "agent", "digest": digest}
        return _side(lines)

    def about_it(base: Side, cand: Side) -> list[str]:
        return [p for p in unreadable(base, cand, tasks) if task.id in p]

    assert about_it(with_task(before), with_task(after)) == []
    assert about_it(with_task(before), with_task("0" * 16)) != []
    assert about_it(with_task("0" * 16), with_task(after)) != []
    assert [p for p in unreadable(with_task(before), with_task(after), None)
            if task.id in p] != [], "unverifiable without the task files"
