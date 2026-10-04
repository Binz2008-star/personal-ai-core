"""Slice 2+3: the coding-agent loop -- read, edit, test, iterate.

The native arm is ADR-025's, not a new protocol: the same NativeScript shape
as test_adr025_native_tools, the same validation boundary, the same text
fallback. The fixture below proves the intended workspace-local sequence
against scripted providers, so no live model is needed:

    read_file -> edit_file -> run_command(pytest, fails) -> edit_file -> answer

and that a failure's text reaches the model's next turn.
"""
from __future__ import annotations

from typing import Any

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import AgentLoop, tool_declarations
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.conversation.factory import build_agent
from personal_ai_core.core.agent import AgentTaskContract
from personal_ai_core.core.domain import ModelResponse, NativeToolCall


class NativeScript:
    """A ToolCallingProvider that plays `(text, calls)` replies in order."""

    name = "native-script"

    def __init__(self, *replies: tuple[str, list[NativeToolCall]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, model, messages, options=None):
        raise AssertionError("the native arm must not call generate")

    def generate_with_tools(self, *, model, messages, tools, options=None):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        text, calls = self.replies.pop(0)
        return ModelResponse(text=text, model=model, tool_calls=tuple(calls))


class TextScript:
    """A text-protocol provider that replies from a list."""

    name = "script"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, model, messages, options=None):
        self.calls.append({"messages": list(messages)})
        return ModelResponse(text=self.replies.pop(0), model=model)


def call(name: Any, arguments: Any) -> NativeToolCall:
    return NativeToolCall(name=name, arguments=arguments)


def answer(text: str) -> tuple[str, list[NativeToolCall]]:
    return (text, [])


def _fixture(tmp_path, name="ws"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "def test_x():\n    assert False, 'not yet fixed'\n", encoding="utf-8"
    )
    return root


def _native_loop(ws, provider, *, confirm=None, **kwargs):
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(
        default_tools(ws, checkpoints), RiskPolicy(), confirm=confirm
    )
    return AgentLoop(
        provider=provider, model="boss", executor=executor,
        checkpoints=checkpoints, native_tools=True, **kwargs
    )


def test_a_native_edit_call_reaches_the_loop_through_adr025(tmp_path):
    ws = Workspace(_fixture(tmp_path))
    script = NativeScript(
        ("", [call("edit_file", {"path": "app.py", "old_string": "x = 1",
                                 "new_string": "x = 2"})]),
        answer("Fixed."),
    )
    outcome = _native_loop(ws, script).run(
        AgentTaskContract(task_text="set x to 2", action_required=True),
        session_id="s1",
    )
    assert outcome.finished and outcome.protocol_errors == 0
    assert outcome.steps[0].record.request.tool == "edit_file"
    assert outcome.steps[0].record.executed
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert script.calls[0]["tools"] == list(
        tool_declarations(_native_loop(ws, script)._executor.specs)
    )


def test_malformed_native_edit_calls_are_protocol_errors(tmp_path):
    ws = Workspace(_fixture(tmp_path))
    for bad in (
        [call("edit_file", {"path": "app.py", "old_string": "x = 1"}),
         call("edit_file", {"path": "app.py", "old_string": "x = 1"})],
        [call(None, {"path": "app.py"})],
        [call("edit_file", '{"path": "app.py"}')],
    ):
        script = NativeScript(("", bad), answer("done"))
        outcome = _native_loop(ws, script).run("x", session_id="s1")
        assert outcome.protocol_errors == 1 and outcome.steps == ()
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 1\n"


def test_the_text_protocol_still_drives_edit_file(tmp_path):
    ws = Workspace(_fixture(tmp_path))
    script = TextScript(
        '{"tool": "edit_file", "arguments": {"path": "app.py", '
        '"old_string": "x = 1", "new_string": "x = 2"}}',
        '{"answer": "Fixed."}',
    )
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(default_tools(ws, checkpoints), RiskPolicy())
    loop = AgentLoop(provider=script, model="boss", executor=executor,
                     checkpoints=checkpoints)
    outcome = loop.run(
        AgentTaskContract(task_text="set x to 2", action_required=True),
        session_id="s1",
    )
    assert outcome.finished
    assert outcome.steps[0].record.request.tool == "edit_file"
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 2\n"


def test_a_test_failure_reaches_the_next_turn_and_the_loop_iterates(tmp_path):
    """read -> edit -> run (fails) -> the failure text is the next input."""
    ws = Workspace(_fixture(tmp_path))
    script = TextScript(
        '{"tool": "read_file", "arguments": {"path": "test_app.py"}}',
        '{"tool": "edit_file", "arguments": {"path": "app.py", '
        '"old_string": "x = 1", "new_string": "x = 2"}}',
        '{"tool": "run_command", "arguments": {"command": "pytest -q"}}',
        '{"answer": "Edited x, but the test still fails as shown; needs a real fix."}',
    )
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(
        default_tools(ws, checkpoints), RiskPolicy(),
        confirm=lambda request, spec: True,
    )
    loop = AgentLoop(provider=script, model="boss", executor=executor,
                     checkpoints=checkpoints)
    outcome = loop.run(
        AgentTaskContract(task_text="fix the failing test", action_required=True),
        session_id="s1",
    )
    tools = [step.record.request.tool for step in outcome.steps]
    assert tools == ["read_file", "edit_file", "run_command"]
    run_step = outcome.steps[2]
    assert run_step.record.executed and not run_step.verified
    # The failure the tool reported is what the model saw next.
    following = script.calls[3]["messages"][-1].content
    assert "failed" in following.lower() or "exit code" in following.lower()
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert outcome.finished


def test_build_agent_wires_edit_and_environment_context(tmp_path):
    root = _fixture(tmp_path)
    agent = build_agent(workspace=root, environment_context=True)
    assert "edit_file" in agent.executor.specs
    assert agent.loop._environment is not None
    composed = agent.loop._environment.compose()
    assert "run_command" in composed.text
