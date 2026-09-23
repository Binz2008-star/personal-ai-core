"""The agent loop -- AGENT_ARCHITECTURE.md section 1, driven by a scripted model."""
from __future__ import annotations

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import AgentLoop, fence, parse_reply
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.core.domain import EventType, ModelResponse, Role
from personal_ai_core.persistence.in_memory import InMemoryEventRepository


class Script:
    """A ModelProvider that replies from a list and remembers what it was sent."""

    name = "script"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, *, model, messages, options=None):
        self.calls.append({"model": model, "messages": list(messages), "options": options})
        return ModelResponse(text=self.replies.pop(0), model=model)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("the answer is 42\n", encoding="utf-8")
    return Workspace(root)


def loop(ws, script, *, confirm=None, events=None, max_failures=3, max_actions=12):
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(default_tools(ws, checkpoints), RiskPolicy(), confirm=confirm)
    return AgentLoop(
        provider=script,
        model="boss",
        executor=executor,
        checkpoints=checkpoints,
        events=events,
        max_failures=max_failures,
        max_actions=max_actions,
    )


# --- the protocol -----------------------------------------------------------------


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"answer": "done"}', {"answer": "done"}),
        ('Sure!\n```json\n{"tool": "read_file", "arguments": {"path": "a"}}\n```',
         {"tool": "read_file", "arguments": {"path": "a"}}),
        ('{"tool": "list_directory"}', {"tool": "list_directory", "arguments": {}}),
        ('{"tool": "write_file", "arguments": {"path": "a", "content": "x\ny"}}',
         {"tool": "write_file", "arguments": {"path": "a", "content": "x\ny"}}),
    ],
)
def test_replies_that_parse(reply, expected):
    assert parse_reply(reply) == expected


@pytest.mark.parametrize(
    "reply, problem",
    [
        ("no json here", "no JSON object"),
        ("{not json}", "not valid JSON"),
        ('{"tool": 3}', '"tool" must be a string'),
        ('{"tool": "x", "arguments": [1]}', '"arguments" must be an object'),
        ('{"answer": 5}', '"answer" must be a string'),
        ('{"thought": "hmm"}', 'neither "tool" nor "answer"'),
    ],
)
def test_replies_that_do_not(reply, problem):
    with pytest.raises(ValueError, match=problem):
        parse_reply(reply)


# --- the loop ------------------------------------------------------------------------


def test_a_task_runs_tools_then_answers(ws):
    script = Script(
        '{"tool": "read_file", "arguments": {"path": "notes.md"}}',
        '{"answer": "It says 42."}',
    )
    outcome = loop(ws, script).run("what do my notes say?", session_id="s1")
    assert outcome.finished and outcome.answer == "It says 42."
    assert [s.record.request.tool for s in outcome.steps] == ["read_file"]
    # The model saw the file's content, fenced as data.
    assert "the answer is 42" in script.calls[1]["messages"][-1].content
    assert script.calls[1]["messages"][-1].content.startswith("<<<result ")


def test_the_model_is_told_the_protocol_and_the_tools(ws):
    script = Script('{"answer": "ok"}')
    loop(ws, script).run("hi", session_id="s1")
    system = script.calls[0]["messages"][0]
    assert system.role is Role.SYSTEM
    for name in ("read_file", "write_file", "run_command", "delete_file"):
        assert name in system.content
    assert "critical risk" in system.content
    assert script.calls[0]["messages"][-1].content == "hi"


def test_the_generation_limit_is_sent(ws):
    script = Script('{"answer": "ok"}')
    loop(ws, script).run("hi", session_id="s1")
    assert script.calls[0]["options"] == {"num_predict": 1024}


def test_a_protocol_error_is_explained_and_charged(ws):
    script = Script("I think I should read it", '{"answer": "ok"}')
    errors = []
    outcome = loop(ws, script).run("x", session_id="s1", on_protocol_error=errors.append)
    assert outcome.finished and outcome.protocol_errors == 1
    assert errors == ["the reply contains no JSON object"]
    assert script.calls[1]["messages"][-1].content.startswith("Protocol error:")


def test_a_model_that_cannot_follow_the_protocol_is_stopped(ws):
    script = Script("a", "b", "c", "d")
    outcome = loop(ws, script, max_failures=3).run("x", session_id="s1")
    assert not outcome.finished
    assert outcome.stopped_reason == "stopped: 3 failed actions reached the limit of 3"
    assert len(script.calls) == 3


def test_repeated_tool_failures_stop_the_model(ws):
    """Failed steps spend the failure budget, not only protocol errors: a
    model retrying a read that cannot succeed is stopped, not indulged."""
    script = Script(*['{"tool": "read_file", "arguments": {"path": "missing.md"}}'] * 5)
    outcome = loop(ws, script, max_failures=2).run("x", session_id="s1")
    assert not outcome.finished and len(outcome.steps) == 2
    assert outcome.stopped_reason == "stopped: 2 failed actions reached the limit of 2"


def test_the_action_limit_stops_a_busy_model(ws):
    script = Script(*['{"tool": "list_directory"}'] * 5)
    outcome = loop(ws, script, max_actions=2).run("x", session_id="s1")
    assert not outcome.finished and len(outcome.steps) == 2


def test_a_command_is_not_run_without_confirmation(ws):
    script = Script(
        '{"tool": "run_command", "arguments": {"command": "git status"}}',
        '{"answer": "could not"}',
    )
    outcome = loop(ws, script).run("x", session_id="s1")
    step = outcome.steps[0]
    assert not step.record.confirmed_by_user and not step.verified
    assert "did not confirm" in script.calls[1]["messages"][-1].content


def test_an_escape_attempt_is_refused_and_reported_to_the_model(ws):
    script = Script(
        '{"tool": "read_file", "arguments": {"path": "../../etc/passwd"}}',
        '{"answer": "blocked"}',
    )
    outcome = loop(ws, script).run("x", session_id="s1")
    assert "traversal" in (outcome.steps[0].record.result.error or "")  # type: ignore[union-attr]


def test_a_secret_in_the_answer_is_withheld(ws):
    script = Script('{"answer": "your key is AKIA' + "Q" * 16 + '"}')
    outcome = loop(ws, script).run("x", session_id="s1")
    assert outcome.answer is not None and outcome.answer.startswith("[answer withheld")
    assert "AKIA" not in outcome.answer


def test_changed_files_are_reported_and_can_be_rolled_back(ws):
    script = Script(
        '{"tool": "write_file", "arguments": {"path": "notes.md", "content": "overwritten"}}',
        '{"answer": "done"}',
    )
    agent = loop(ws, script)
    outcome = agent.run("x", session_id="s1")
    assert outcome.touched_files == ("notes.md",)
    agent._checkpoints.rollback()  # type: ignore[union-attr]
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "the answer is 42\n"


def test_each_step_is_reported_as_it_happens(ws):
    script = Script('{"tool": "list_directory"}', '{"answer": "ok"}')
    seen = []
    loop(ws, script).run("x", session_id="s1", on_step=lambda s: seen.append(s.record.request.tool))
    assert seen == ["list_directory"]


# --- RECORD EVENT ----------------------------------------------------------------------


def test_every_step_and_the_finish_are_events(ws):
    events = InMemoryEventRepository()
    script = Script(
        '{"tool": "list_directory"}',
        '{"tool": "run_command", "arguments": {"command": "git status"}}',
        '{"tool": "no_such_tool"}',
        '{"answer": "ok"}',
    )
    loop(ws, script, events=events).run("x", session_id="s1")
    recorded = events.list_for_session("s1")
    assert [e.type for e in recorded] == [
        EventType.AGENT_STEP, EventType.AGENT_STEP, EventType.AGENT_STEP, EventType.AGENT_FINISHED,
    ]
    decisions = [e.payload["decision"] for e in recorded[:3]]
    assert decisions == ["allow", "ask", "deny"]
    assert recorded[1].payload["ran"] is False
    assert recorded[-1].payload["finished"] is True


def test_an_event_payload_carries_no_error_text(ws):
    """Error text can carry workspace paths and values; the audit log has it,
    the logged event does not."""
    events = InMemoryEventRepository()
    script = Script(
        '{"tool": "read_file", "arguments": {"path": "../secret/path"}}',
        '{"answer": "ok"}',
    )
    loop(ws, script, events=events).run("x", session_id="s1")
    step = events.list_for_session("s1")[0]
    assert "error" not in step.payload
    assert "secret/path" not in repr(dict(step.payload))


def test_a_stopped_run_is_recorded_as_not_finished(ws):
    events = InMemoryEventRepository()
    loop(ws, Script("a", "b", "c"), events=events).run("x", session_id="s1")
    finish = events.list_for_session("s1")[-1]
    assert finish.type is EventType.AGENT_FINISHED
    assert finish.payload["finished"] is False and finish.payload["protocol_errors"] == 3


def test_the_fence_token_depends_on_the_content():
    assert fence("read_file -> ok", "a") != fence("read_file -> ok", "b")


def test_a_step_refused_for_its_arguments_is_recorded_as_not_run(ws):
    """F-3: the tool never started, so the event must not say it ran."""
    events = InMemoryEventRepository()
    script = Script(
        '{"tool": "read_file", "arguments": {"path": 3}}',
        '{"tool": "read_file", "arguments": {"path": "notes.md"}}',
        '{"answer": "ok"}',
    )
    loop(ws, script, events=events).run("x", session_id="s1")
    refused, allowed = events.list_for_session("s1")[:2]
    assert refused.payload["decision"] == "allow"
    assert refused.payload["ran"] is False and refused.payload["ok"] is False
    assert allowed.payload["ran"] is True and allowed.payload["ok"] is True


# --- an unknown session (F-2) ------------------------------------------------------


def test_an_unknown_session_is_refused_before_anything_happens(ws):
    events = InMemoryEventRepository()
    script = Script('{"tool": "list_directory"}', '{"answer": "ok"}')
    agent = AgentLoop(
        provider=script,
        model="boss",
        executor=ToolExecutor(default_tools(ws), RiskPolicy()),
        events=events,
        session_exists=lambda session_id: session_id == "known",
    )
    with pytest.raises(KeyError, match="unknown session: nope"):
        agent.run("x", session_id="nope")
    assert script.calls == []
    assert not events.list_for_session("nope")

    agent.run("x", session_id="known")
    assert [e.type for e in events.list_for_session("known")][-1] is EventType.AGENT_FINISHED
