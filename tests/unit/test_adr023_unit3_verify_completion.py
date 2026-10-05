"""ADR-023 unit 3 (§2.3, `tests_passed`): no accepted completion without a passing test.

Under a contract that requires action and names a test command, with the check
on, an answer is accepted only once that exact command has run, after the last
change to the workspace, and passed. A refused answer costs one failure from the
one global budget (§2.4). Off by default everywhere; off, nothing changes.
"""
from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import (
    ACTION_REQUIRED_MESSAGE,
    COMPLETION_CHECK_NOTICE,
    EDITING_TOOLS,
    VERIFICATION_REQUIRED_MESSAGE,
    AgentLoop,
)
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import WriteFile
from personal_ai_core.app.bench import checks
from personal_ai_core.app.bench.runner import agent_test_command, main as bench_main, run_agent_task
from personal_ai_core.app.bench.tasks import load
from personal_ai_core.conversation.factory import build_agent
from personal_ai_core.core.agent import AgentTaskContract, RiskLevel, ToolResult, ToolSpec
from personal_ai_core.core.domain import EventType, ModelResponse, Role
from personal_ai_core.persistence.in_memory import InMemoryEventRepository

TESTS = "pytest -q -p no:cacheprovider"
BENCH = Path(__file__).resolve().parents[2] / "evals" / "bench"


class Script:
    name = "script"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[Any]] = []

    def generate(self, *, model, messages, options=None):
        self.calls.append(list(messages))
        return ModelResponse(text=self.replies.pop(0), model=model)


class Command:
    """`run_command` or `shell`, played from a table: command -> exit statuses, in order."""

    def __init__(self, name: str, exits: dict[str, list[int]]) -> None:
        self.exits = exits
        self.spec = ToolSpec(name=name, description="scripted", risk_level=RiskLevel.LOW,
                             input_schema={"type": "object",
                                           "properties": {"command": {"type": "string"}},
                                           "required": ["command"]},
                             timeout_seconds=5, idempotent=False)

    def run(self, arguments):
        queue = self.exits.get(arguments["command"])
        code = queue.pop(0) if queue else 0
        return ToolResult(ok=code == 0, output=f"exit {code}",
                          error=None if code == 0 else f"exit code {code}")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def _loop(ws, script, *, exits=None, verify=True, events=None, **kwargs):
    checkpoints = Checkpoints(ws)
    exits = exits or {}
    tools = [WriteFile(ws, checkpoints), Command("run_command", exits), Command("shell", exits)]
    executor = ToolExecutor(tools, RiskPolicy())
    return AgentLoop(provider=script, model="boss", executor=executor, context_window=8192, budget_policy=ReserveBasedBudgetPolicy(),
                     estimator=ScriptAwareTokenEstimator(),
                     checkpoints=checkpoints, events=events, verify_completion=verify,
                     session_exists=(lambda s: True) if events is not None else None, **kwargs)


def _contract(test_command: str | None = TESTS, action_required: bool = True) -> AgentTaskContract:
    return AgentTaskContract(task_text="fix calc.py", action_required=action_required,
                             test_command=test_command)


def tool(name: str, **arguments: Any) -> str:
    return json.dumps({"tool": name, "arguments": arguments})


def answer(text: str = "done") -> str:
    return json.dumps({"answer": text})


WRITE = tool("write_file", path="calc.py", content="x = 1\n")
RUN_TESTS = tool("run_command", command=TESTS)


# --- off by default, and nothing changes when off --------------------------------------


def test_off_by_default_everywhere():
    for function in (AgentLoop.__init__, build_agent, run_agent_task):
        assert inspect.signature(function).parameters["verify_completion"].default is False
    assert AgentTaskContract(task_text="t", action_required=True).test_command is None


def test_off_an_untested_change_is_accepted_and_nothing_is_said(ws):
    script = Script(WRITE, answer())
    outcome = _loop(ws, script, verify=False).run(_contract(), session_id="s")
    assert outcome.answer == "done" and outcome.verification_rejections == 0
    assert not any("Completion check" in m.content for m in script.calls[0])


@pytest.mark.parametrize("contract", [_contract(test_command=None),
                                      _contract(action_required=False), "a plain task"])
def test_on_without_a_test_command_or_a_contract_requiring_action_nothing_changes(ws, contract):
    script = Script(WRITE, answer())
    outcome = _loop(ws, script).run(contract, session_id="s")
    assert outcome.answer == "done" and outcome.verification_rejections == 0
    assert not any("Completion check" in m.content for m in script.calls[0])


# --- the check ------------------------------------------------------------------------------


def test_the_command_is_given_before_the_task(ws):
    script = Script(WRITE, RUN_TESTS, answer())
    _loop(ws, script).run(_contract(), session_id="s")
    opening = script.calls[0]
    notice = COMPLETION_CHECK_NOTICE.format(command=TESTS)
    assert [m.content for m in opening if m.role is Role.SYSTEM][-1] == notice
    assert opening[-1].role is Role.USER and opening[-1].content == "fix calc.py"


def test_an_untested_change_is_refused_then_accepted_once_the_tests_pass(ws):
    script = Script(WRITE, answer("early"), RUN_TESTS, answer("tested"))
    outcome = _loop(ws, script).run(_contract(), session_id="s")
    assert outcome.answer == "tested"
    assert outcome.verification_rejections == 1
    refused = outcome.refused_replies
    assert [(r.call, r.kind) for r in refused] == [(2, "verification_required")]
    assert '"early"' in refused[0].text
    # The model was told what is missing, and the command.
    assert script.calls[2][-1].content == VERIFICATION_REQUIRED_MESSAGE.format(command=TESTS)


def test_a_refusal_costs_one_failure_and_three_stop_the_run(ws):
    script = Script(WRITE, answer(), answer(), answer(), answer())
    outcome = _loop(ws, script).run(_contract(), session_id="s")
    assert outcome.answer is None and outcome.verification_rejections == 3
    assert "3 failed actions" in (outcome.stopped_reason or "")
    assert len(script.calls) == 4


def test_unit_1_answers_first_an_answer_before_any_action(ws):
    script = Script(answer(), WRITE, RUN_TESTS, answer())
    outcome = _loop(ws, script).run(_contract(), session_id="s")
    assert [r.kind for r in outcome.refused_replies] == ["action_required"]
    assert script.calls[1][-1].content == ACTION_REQUIRED_MESSAGE
    assert outcome.answer == "done" and outcome.verification_rejections == 0


@pytest.mark.parametrize("command, counts", [
    (TESTS, True),
    ("pytest   -q  -p no:cacheprovider", True),      # the same tokens
    ("pytest -q", False),                             # not the contract's command
    ("python -m pytest -q -p no:cacheprovider", False),
    ("pytest -q -p no:cacheprovider test_calc.py", False),  # a subset is not the suite
])
def test_only_the_contracts_command_counts(ws, command, counts):
    script = Script(WRITE, tool("run_command", command=command), answer(), answer(), answer())
    outcome = _loop(ws, script).run(_contract(), session_id="s")
    assert (outcome.answer == "done") is counts


def test_the_test_command_through_shell_counts_too(ws):
    script = Script(WRITE, tool("shell", command=TESTS), answer())
    assert _loop(ws, script).run(_contract(), session_id="s").answer == "done"


@pytest.mark.parametrize("statuses, accepted", [
    ([1], False),        # a failing run is not evidence
    ([0, 1], False),     # passed, then failed: the later failure stands
    ([1, 0], True),      # failed, then passed
])
def test_the_last_run_of_the_command_decides(ws, statuses, accepted):
    runs = [RUN_TESTS] * len(statuses)
    script = Script(WRITE, *runs, answer(), answer(), answer())
    outcome = _loop(ws, script, exits={TESTS: list(statuses)}).run(_contract(), session_id="s")
    assert (outcome.answer == "done") is accepted


def test_a_change_after_the_passing_run_voids_it(ws):
    script = Script(WRITE, RUN_TESTS, WRITE, answer(), RUN_TESTS, answer())
    outcome = _loop(ws, script).run(_contract(), session_id="s")
    assert outcome.verification_rejections == 1 and outcome.answer == "done"


def test_a_shell_command_after_the_run_counts_as_a_change_a_checking_command_does_not(ws):
    script = Script(WRITE, RUN_TESTS, tool("shell", command="echo hi"), answer(), answer(),
                    answer())
    assert _loop(ws, script).run(_contract(), session_id="s").answer is None
    script = Script(WRITE, RUN_TESTS, tool("run_command", command="git status"), answer())
    assert _loop(ws, script).run(_contract(), session_id="s").answer == "done"


def test_with_no_change_a_passing_run_is_enough(ws):
    script = Script(RUN_TESTS, answer("they pass"))
    assert _loop(ws, script).run(_contract(), session_id="s").answer == "they pass"


def test_the_editing_tools_are_the_benchmarks():
    assert EDITING_TOOLS == checks.EDITING_TOOLS


# --- what is recorded -------------------------------------------------------------------


def test_events_name_the_reason_and_the_count_only_when_on(ws):
    events = InMemoryEventRepository()
    _loop(ws, Script(WRITE, answer(), RUN_TESTS, answer()), events=events).run(
        _contract(), session_id="s")
    rejected = [e.payload for e in events.list_for_session("s")
                if e.type is EventType.AGENT_ANSWER_REJECTED]
    assert rejected == [{"reason": "verification_required", "rejection": 1,
                         "action_required": True}]
    finished = [e.payload for e in events.list_for_session("s")
                if e.type is EventType.AGENT_FINISHED]
    assert finished[0]["verification_rejections"] == 1

    events = InMemoryEventRepository()
    _loop(ws, Script(WRITE, answer()), events=events, verify=False).run(
        _contract(), session_id="s")
    finished = [e.payload for e in events.list_for_session("s")
                if e.type is EventType.AGENT_FINISHED]
    assert "verification_rejections" not in finished[0]


@pytest.mark.parametrize("value, error", [("  ", ValueError), (3, TypeError)])
def test_the_contract_refuses_an_empty_or_non_text_command(value, error):
    with pytest.raises(error):
        AgentTaskContract(task_text="t", action_required=True, test_command=value)


def test_the_contract_strips_the_command():
    assert AgentTaskContract(task_text="t", action_required=True,
                             test_command="  pytest -q ").test_command == "pytest -q"


# --- the benchmark --------------------------------------------------------------------------


def test_the_test_command_comes_from_each_tasks_own_check():
    commands = {t.id: agent_test_command(t) for t in load(BENCH) if t.track == "agent"}
    assert {k for k, v in commands.items() if v} == {
        "codegen-slugify", "debug-word-count", "modify-discount-cap", "verify-off-by-one",
        "verify-sales-total"}
    assert set(commands.values()) == {TESTS, None}


def _bench(tmp_path, scripts, *argv):
    from .test_bench_runner import BENCH as TASKS, FIXED, ScriptedModel  # noqa: PLC0415
    out = io.StringIO()
    code = bench_main(["--tasks", str(TASKS), "--out", str(tmp_path), "--runs", "1",
                       "--only", "verify-off-by-one", "--languages", "en", *argv],
                      transport=ScriptedModel(scripts), stdout=out, env={},
                      now=lambda: FIXED, commit="abc1234",
                      probe=lambda url, body=None: {"models": []})
    files = sorted(tmp_path.glob("bench-*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    return code, out.getvalue(), lines


def test_the_benchmark_records_the_flag_off_by_default_and_the_count_only_when_on(tmp_path):
    from .test_bench_runner import SOLVES  # noqa: PLC0415
    code, output, lines = _bench(tmp_path / "off", SOLVES)
    assert code == 0, output
    run = next(x for x in lines if x["kind"] == "run")
    assert lines[0]["verify_completion"] is False and "verification_rejections" not in run
    assert run["success"]


def test_with_the_flag_a_test_run_through_another_command_is_refused(tmp_path):
    # The existing solve tests with `python -m pytest` through shell: not the
    # contract's command, and a shell command counts as a change.
    from .test_bench_runner import SOLVES  # noqa: PLC0415
    code, output, lines = _bench(tmp_path, SOLVES, "--verify-completion")
    assert code == 0, output
    run = next(x for x in lines if x["kind"] == "run")
    assert lines[0]["verify_completion"] is True
    assert run["verification_rejections"] == 3 and run["stop"] == "budget"
    assert [r["kind"] for r in run["refused_replies"]] == ["verification_required"] * 3


def test_with_the_flag_the_task_is_solved_through_the_contracts_command(tmp_path):
    from .test_bench_runner import FIXED_CALC  # noqa: PLC0415
    scripts = {"test_calc.py": [
        tool("read_file", path="calc.py"),
        tool("write_file", path="calc.py", content=FIXED_CALC),
        tool("run_command", command=TESTS),
        answer("Fixed; the tests pass.")]}
    code, output, lines = _bench(tmp_path, scripts, "--verify-completion")
    assert code == 0, output
    run = next(x for x in lines if x["kind"] == "run")
    assert run["success"] and run["verification_rejections"] == 0, run["steps"]
    assert run["steps"][-1]["executed"] and run["steps"][-1]["ok"]


# --- what the check would have met in #208's text arm (read after the data) ------------


def test_in_208s_text_arm_most_answers_on_test_command_tasks_came_untested():
    """The handoff's figures. Of 31 answered attempts on the five tasks with a test
    command, 29 failed and 2 succeeded; only 5 ran pytest (in any form) after the last
    change, and all 5 failed. A count of what was recorded, not a prediction."""
    tasks = {t.id for t in load(BENCH) if t.track == "agent" and agent_test_command(t)}
    path = BENCH.parent / "results" / "bench" / "bench-20261004T004107Z.jsonl"
    runs = [r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if r.get("kind") == "run" and r["task"] in tasks and r["stop"] == "answered"]

    def tested_after_last_change(run: dict) -> bool:
        executed = [s for s in run["steps"] if s["executed"]]

        def is_test(step: dict) -> bool:
            command = str(step["arguments"].get("command", ""))
            return step["tool"] in ("run_command", "shell") and bool(checks.TESTING.search(command))

        last = max((i for i, s in enumerate(executed)
                    if s["tool"] in checks.EDITING_TOOLS and not is_test(s)), default=-1)
        return any(is_test(s) for s in executed[last + 1:])

    assert (len(runs), sum(r["success"] for r in runs)) == (31, 2)
    tested = [r for r in runs if tested_after_last_change(r)]
    assert len(tested) == 5 and not any(r["success"] for r in tested)
