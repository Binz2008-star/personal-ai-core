"""The ADR-023 unit 3 measurement (#217), re-derived from its files.

Two arms on the rig at b10613d, 10 runs each, the same model and the same
loaded context; the only difference is `--verify-completion`. ADR-023 §8.6
and the handoff quote the figures below; each is re-derived here from the
committed records.

Three kinds of figure, kept apart:

1. The instrument: what each file says it measured.
2. The verdict, by the rule fixed before the runs (amendment 1, unit 3 ->
   target class 2): FAIL. A FAIL is not re-run until it passes (R5).
3. Descriptive figures, by reading rules fixed at 2026-10-04T21:44:49Z, before
   arm B's file existed (D1-D4 in ADR-023 §8.6). They describe; they decide
   nothing, and no per-task conclusion is drawn from 10 runs.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from personal_ai_core.app.bench.checks import EDITING_TOOLS
from personal_ai_core.app.bench.compare import Side
from personal_ai_core.app.bench.runner import agent_test_command
from personal_ai_core.app.bench.tasks import load
from personal_ai_core.app.bench.verdict import decide

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
DEFAULT = RESULTS / "bench-20261004T183911Z.jsonl"
VERIFY = RESULTS / "bench-20261004T211958Z.jsonl"
MEASURED_AT = "b10613d726c00ab8983637fd66e3b35fa086de82"

BOSS = "huihui_ai/qwen2.5-abliterate:7b"
WEIGHTS = "sha256:212345411ab817a8d7669ed63f24a30f2e2a2cd297455a6aad1de50c82f7691c"
MANIFEST = "sha256:103482475c9b4d4032999dd5d7383478cad8b966ae6ae984d525daf7352c2219"

TASKS = {task.id: task for task in load(REPO / "evals" / "bench")}
# The tasks a contract test command can be derived for: the only ones the check acts on.
COMMAND = {task_id: agent_test_command(task) for task_id, task in TASKS.items()
           if task.track == "agent"}
FIVE = {task_id for task_id, command in COMMAND.items() if command}


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _runs(path: Path) -> list[dict]:
    return [r for r in _lines(path) if r.get("kind") == "run"]


def _agent(path: Path) -> list[dict]:
    return [r for r in _runs(path) if r["track"] == "agent"]


def _header_and_end(path: Path) -> tuple[dict, dict]:
    lines = _lines(path)
    return lines[0], next(r for r in lines if r.get("kind") == "end")


# --- 1. The instrument ----------------------------------------------------------


def test_both_arms_are_the_instrument_they_claim_to_be():
    for path, verify in ((DEFAULT, False), (VERIFY, True)):
        header, end = _header_and_end(path)
        assert header["commit"] == MEASURED_AT
        assert header["model"] == BOSS and header["role"] == "boss"
        assert header["verify_completion"] is verify
        assert header["native_tools"] is False
        assert header["environment_context"] is False
        assert header["lenient_protocol"] is False
        assert header["num_ctx_measured_by_owner"] == 8192
        assert header["runs"] == 10
        assert end["context_mismatch"] is False
        assert len(_runs(path)) == 480 and len(_agent(path)) == 240
        assert not any(r["stop"] == "error" for r in _runs(path))
        for weights in (end["weights"], end["ollama_loaded"]["weights"]):
            assert weights["digest"] == WEIGHTS and weights["manifest_digest"] == MANIFEST
            assert weights["verified"] is True and weights["adapters"] == []
        assert end["ollama_loaded"]["probed"] is True
        assert end["ollama_loaded"]["context_length"] == 8192


def test_the_arms_differ_only_by_the_verify_flag():
    (a_header, a_end), (b_header, b_end) = map(_header_and_end, (DEFAULT, VERIFY))
    differ = {key for key in a_header.keys() | b_header.keys()
              if a_header.get(key) != b_header.get(key)}
    assert differ == {"verify_completion", "started_at"}
    assert a_end == b_end
    assert len(a_header["tasks"]) == 24


def test_the_check_acts_on_five_agent_tasks_and_seven_are_beyond_it():
    assert sorted(FIVE) == ["codegen-slugify", "debug-word-count", "modify-discount-cap",
                            "verify-off-by-one", "verify-sales-total"]
    assert len(COMMAND) - len(FIVE) == 7


# --- 2. The verdict, by the rule fixed before the runs ----------------------------


def test_the_verdict_is_fail_with_the_figures_recorded():
    """Every task digest is identical across the arms, so the task files change
    nothing in R0 to R2; without them only the false-rejection cost is left
    uncounted. #217 records that line verbatim: 2 of 114 -> 1 of 108."""
    verdict = decide(Side(DEFAULT, _lines(DEFAULT)), Side(VERIFY, _lines(VERIFY)), "3", None)
    assert verdict.outcome == "FAIL"
    assert not verdict.unreadable and not verdict.dropped
    assert (verdict.leaving, verdict.entering) == (48, 40)
    assert round(verdict.p_value, 4) == 0.2279
    assert verdict.failures == {("agent", "en"): (98, 92), ("agent", "ar"): (97, 94),
                                ("knowledge", "en"): (0, 0), ("knowledge", "ar"): (12, 16)}
    assert not verdict.regressions
    costs = {label: (a, b) for label, a, b in verdict.costs}
    assert costs["agent attempts stopped on the budget"] == ("119", "145")
    assert costs["answers rejected for not having acted"] == (
        "114 (0.47 per attempt)", "108 (0.45 per attempt)")
    assert costs["protocol errors"] == ("88 (0.37 per attempt)", "87 (0.36 per attempt)")
    assert costs["answers rejected for an unverified completion"] == (
        "0 (0.00 per attempt)", "30 (0.12 per attempt)")


# --- 3. Descriptive, by the reading rules fixed before arm B was seen -------------


def _five(path: Path) -> list[dict]:
    return [r for r in _agent(path) if r["task"] in FIVE]


def _seven(path: Path) -> list[dict]:
    return [r for r in _agent(path) if r["task"] not in FIVE]


def test_d1_success_on_the_five_and_on_the_seven_the_check_cannot_reach():
    """The five: 3/100 -> 2/100. The seven, untouched by the check: 42/140 ->
    52/140. The noise beside the check is larger than anything it moved."""
    assert [sum(r["success"] for r in _five(p)) for p in (DEFAULT, VERIFY)] == [3, 2]
    assert [len(_five(p)) for p in (DEFAULT, VERIFY)] == [100, 100]
    assert [sum(r["success"] for r in _seven(p)) for p in (DEFAULT, VERIFY)] == [42, 52]


def _passed_after_last_change(run: dict) -> bool:
    """The contract's own condition: the last exact run of the test command,
    after the last change, passed."""
    command = shlex.split(COMMAND[run["task"]])
    executed = [s for s in run["steps"] if s["executed"]]

    def is_test(step: dict) -> bool:
        arguments = step["arguments"] if isinstance(step["arguments"], dict) else {}
        text = arguments.get("command")
        return (step["tool"] in ("run_command", "shell") and isinstance(text, str)
                and shlex.split(text) == command)

    last = max((i for i, s in enumerate(executed)
                if s["tool"] in EDITING_TOOLS and not is_test(s)), default=-1)
    tests = [s for s in executed[last + 1:] if is_test(s)]
    return bool(tests) and tests[-1]["ok"]


def test_d2_the_check_refused_thirty_answers_and_let_through_only_verified_ones():
    five = _five(VERIFY)
    assert sum(r.get("verification_rejections", 0) for r in five) == 30
    answered = [r for r in five if r["stop"] == "answered"]
    assert len(answered) == 2
    assert all(_passed_after_last_change(r) for r in answered)


def test_d3_an_attempt_the_check_refused_rarely_recovered():
    refused = [r for r in _five(VERIFY) if r.get("verification_rejections", 0) >= 1]
    assert len(refused) == 29
    assert sum(r["success"] for r in refused) == 2


def test_d4_on_the_five_the_refusals_became_budget_stops():
    """65 -> 98: an answer refused for want of a passing test was followed,
    mostly, by more attempts that ran out of budget, not by a passing test."""
    assert [sum(r["stop"] == "budget" for r in _five(p)) for p in (DEFAULT, VERIFY)] == [65, 98]
