"""The executor -- request -> policy -> arguments -> confirmation -> tool -> audit."""
from __future__ import annotations

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.schema import SchemaError
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.core.agent import (
    Decision,
    RiskLevel,
    ToolRequest,
    ToolResult,
    ToolSpec,
)


class Spy:
    """A tool that records whether it ran."""

    def __init__(self, name="spy", risk=RiskLevel.LOW, raises=None):
        self.calls = []
        self.raises = raises
        self.spec = ToolSpec(
            name=name,
            description="",
            risk_level=risk,
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
                "additionalProperties": False,
            },
            timeout_seconds=1,
            idempotent=True,
        )

    def run(self, arguments):
        self.calls.append(dict(arguments))
        if self.raises:
            raise self.raises
        return ToolResult(ok=True, output=f"ran {arguments['n']}")


def executor(*tools, confirm=None, overrides=None):
    return ToolExecutor(tools, RiskPolicy(overrides), confirm=confirm)


# --- ALLOW -----------------------------------------------------------------------


def test_an_allowed_request_runs_and_is_audited():
    spy = Spy()
    ex = executor(spy)
    record = ex.execute(ToolRequest("spy", {"n": 1}))
    assert spy.calls == [{"n": 1}]
    assert record.result is not None and record.result.output == "ran 1"
    assert record.decision.decision is Decision.ALLOW
    assert ex.audit.records() == (record,)


# --- DENY --------------------------------------------------------------------------


def test_an_unknown_tool_runs_nothing_and_is_still_audited():
    ex = executor(Spy())
    record = ex.execute(ToolRequest("format_disk", {}))
    assert record.decision.decision is Decision.DENY and record.result is None
    assert ex.audit.records() == (record,)


def test_a_denied_tool_does_not_run():
    spy = Spy()
    ex = executor(spy, overrides={"spy": Decision.DENY})
    ex.execute(ToolRequest("spy", {"n": 1}))
    assert spy.calls == []


# --- ASK -----------------------------------------------------------------------------


def test_ask_without_a_confirmer_means_no():
    spy = Spy(risk=RiskLevel.HIGH)
    record = executor(spy).execute(ToolRequest("spy", {"n": 1}))
    assert spy.calls == []
    assert record.result is not None and "not confirmed" in (record.result.error or "")
    assert not record.confirmed_by_user


def test_ask_runs_only_when_the_user_says_yes():
    spy = Spy(risk=RiskLevel.HIGH)
    asked = []

    def confirm(request, spec):
        asked.append((request.tool, spec.risk_level))
        return True

    record = executor(spy, confirm=confirm).execute(ToolRequest("spy", {"n": 7}))
    assert asked == [("spy", RiskLevel.HIGH)]
    assert spy.calls == [{"n": 7}] and record.confirmed_by_user


def test_a_refusal_runs_nothing():
    spy = Spy(risk=RiskLevel.CRITICAL)
    executor(spy, confirm=lambda r, s: False).execute(ToolRequest("spy", {"n": 1}))
    assert spy.calls == []


def test_a_confirmation_is_per_request_not_remembered():
    spy = Spy(risk=RiskLevel.HIGH)
    answers = iter([True, False])
    ex = executor(spy, confirm=lambda r, s: next(answers))
    ex.execute(ToolRequest("spy", {"n": 1}))
    ex.execute(ToolRequest("spy", {"n": 2}))
    assert spy.calls == [{"n": 1}]


def test_low_risk_is_never_asked():
    asked = []
    executor(Spy(), confirm=lambda r, s: asked.append(r) or True).execute(
        ToolRequest("spy", {"n": 1})
    )
    assert asked == []


# --- arguments are untrusted -----------------------------------------------------------


@pytest.mark.parametrize(
    "arguments, problem",
    [({}, "missing required argument: n"),
     ({"n": "1"}, "n must be integer"),
     ({"n": True}, "n must be integer"),
     ({"n": 1, "extra": 2}, "unexpected argument: extra")],
)
def test_invalid_arguments_run_nothing(arguments, problem):
    spy = Spy()
    record = executor(spy).execute(ToolRequest("spy", arguments))
    assert spy.calls == []
    assert record.result is not None and problem in (record.result.error or "")


def test_invalid_arguments_are_refused_before_the_user_is_asked():
    asked = []
    spy = Spy(risk=RiskLevel.HIGH)
    executor(spy, confirm=lambda r, s: asked.append(r) or True).execute(
        ToolRequest("spy", {"n": "x"})
    )
    assert asked == [] and spy.calls == []


# --- failures are contained ---------------------------------------------------------------


def test_a_tool_that_raises_becomes_a_failed_result():
    record = executor(Spy(raises=RuntimeError("disk on fire"))).execute(
        ToolRequest("spy", {"n": 1})
    )
    assert record.result is not None and not record.result.ok
    assert record.result.error == "RuntimeError: disk on fire"


# --- registration -----------------------------------------------------------------------


def test_two_tools_with_one_name_are_refused():
    with pytest.raises(ValueError, match="two tools"):
        executor(Spy(), Spy())


def test_a_schema_the_validator_cannot_enforce_is_refused_at_registration():
    spy = Spy()
    object.__setattr__(spy, "spec", ToolSpec(
        name="spy", description="", risk_level=RiskLevel.LOW,
        input_schema={"type": "object", "properties": {"n": {"type": "number", "minimum": 0}}},
        timeout_seconds=1, idempotent=True,
    ))
    with pytest.raises(SchemaError):
        executor(spy)


def test_a_loosening_override_is_refused_at_registration():
    with pytest.raises(ValueError, match="only tighten"):
        executor(Spy(risk=RiskLevel.HIGH), overrides={"spy": Decision.ALLOW})


# --- the real tools, end to end ----------------------------------------------------------


def test_the_real_tools_through_the_executor(tmp_path):
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    ex = ToolExecutor(default_tools(Workspace(tmp_path)), RiskPolicy())
    read = ex.execute(ToolRequest("read_file", {"path": "a.md"}))
    write = ex.execute(ToolRequest("write_file", {"path": "b.md", "content": "x"}))
    command = ex.execute(ToolRequest("run_command", {"command": "git status"}))
    escape = ex.execute(ToolRequest("read_file", {"path": "../etc/passwd"}))

    assert read.result is not None and read.result.output == "hello\n"
    assert write.result is not None and write.result.ok
    assert command.decision.decision is Decision.ASK
    assert command.result is not None and "not confirmed" in (command.result.error or "")
    assert escape.result is not None and "traversal" in (escape.result.error or "")
    assert len(ex.audit.records()) == 4
