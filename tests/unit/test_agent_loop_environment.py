"""The environment context in the agent loop (ADR-023 §2.1): off is unchanged, on adds one block."""
from __future__ import annotations

import pytest

from personal_ai_core.agent.environment import EnvironmentContext
from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import AgentLoop
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.core.agent import AgentTaskContract
from personal_ai_core.core.domain import EventType, ModelResponse, Role
from personal_ai_core.persistence.in_memory import InMemoryEventRepository

ESTIMATE = ScriptAwareTokenEstimator().estimate


class Script:
    name = "script"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, *, model, messages, options=None):
        self.calls.append(list(messages))
        return ModelResponse(text=self.replies.pop(0), model=model)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    (path / "notes.md").write_text("the answer is 42\n", encoding="utf-8")
    return path


def make(root, script, *, environment=None, events=None):
    ws = Workspace(root)
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(default_tools(ws, checkpoints), RiskPolicy())
    return AgentLoop(provider=script, model="boss", executor=executor, context_window=8192,
                     checkpoints=checkpoints, events=events, environment=environment)


def record_of(outcome) -> dict:
    """The environment record of a run that had the context on; a missing one fails the test."""
    assert outcome.environment is not None, "the run recorded no environment"
    return dict(outcome.environment)


def context(root, **kw):
    return EnvironmentContext(root, estimate=ESTIMATE, system="Linux", **kw)


def test_with_the_context_off_the_opening_is_what_it_always_was(root):
    script = Script('{"answer": "done"}')
    outcome = make(root, script).run("explain", session_id="s")
    assert [m.role for m in script.calls[0]] == [Role.SYSTEM, Role.USER]
    assert outcome.environment is None


def test_with_it_on_one_system_block_sits_between_the_protocol_and_the_task(root):
    script = Script('{"answer": "done"}')
    make(root, script, environment=context(root)).run("explain", session_id="s")
    first = script.calls[0]
    assert [m.role for m in first] == [Role.SYSTEM, Role.SYSTEM, Role.USER]
    assert first[0].content.startswith("You are working as an agent")
    assert first[1].content.startswith("Environment (facts gathered by the program")
    assert first[2].content == "explain"


def test_the_block_is_sent_on_the_first_call_only_as_part_of_the_opening(root):
    script = Script('{"tool": "list_directory", "arguments": {}}', '{"answer": "done"}')
    make(root, script, environment=context(root)).run("look", session_id="s")
    assert sum(m.content.startswith("Environment (") for m in script.calls[0]) == 1
    assert sum(m.content.startswith("Environment (") for m in script.calls[1]) == 1, (
        "later calls carry the same opening, once, and no second copy")


def test_the_outcome_and_the_finish_event_carry_the_record_only_when_it_is_on(root):
    off_events, on_events = InMemoryEventRepository(), InMemoryEventRepository()
    make(root, Script('{"answer": "a"}'), events=off_events).run("t", session_id="s1")
    on = make(root, Script('{"answer": "a"}'), environment=context(root), events=on_events)
    outcome = on.run("t", session_id="s1")
    off_finish = off_events.list_for_session("s1")[-1]
    on_finish = on_events.list_for_session("s1")[-1]
    assert off_finish.type is EventType.AGENT_FINISHED and "environment" not in off_finish.payload
    assert on_finish.payload["environment"] == record_of(outcome)
    assert {k: v for k, v in on_finish.payload.items() if k != "environment"} == dict(
        off_finish.payload), "nothing else in the event changed"
    assert record_of(outcome)["tokens"] > 0 and record_of(outcome)["system"] == "Linux"


def test_a_run_that_stops_on_the_budget_still_records_it(root):
    script = Script("not json", "still not json", "no")
    outcome = make(root, script, environment=context(root)).run("t", session_id="s")
    assert not outcome.finished and outcome.environment is not None


def test_each_run_reads_the_workspace_again(root):
    loop = make(root, Script('{"answer": "a"}', '{"answer": "b"}'), environment=context(root))
    first = loop.run("t", session_id="s")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    second = loop.run("t", session_id="s")
    assert record_of(first)["test_command"] is None
    assert record_of(second)["test_command"] == "pytest"


def test_it_does_not_disturb_the_action_gate(root):
    script = Script('{"answer": "no"}', '{"tool": "list_directory", "arguments": {}}',
                    '{"answer": "yes"}')
    outcome = make(root, script, environment=context(root)).run(
        AgentTaskContract(task_text="look", action_required=True), session_id="s")
    assert outcome.finished and outcome.answer == "yes" and outcome.action_rejections == 1
    assert outcome.environment is not None
