"""The policy gate and the agent types -- AGENT_ARCHITECTURE.md sections 2-3."""
from __future__ import annotations

import pytest

from personal_ai_core.agent.policy import DEFAULT_DECISIONS, RiskPolicy
from personal_ai_core.core.agent import (
    Decision,
    RiskLevel,
    ToolRequest,
    ToolSpec,
    VerificationCheck,
    VerificationResult,
)
from personal_ai_core.core.contracts import ToolPolicy


def spec(name="read_file", risk=RiskLevel.LOW):
    return ToolSpec(
        name=name,
        description="test tool",
        risk_level=risk,
        input_schema={"type": "object"},
        timeout_seconds=5,
        idempotent=True,
    )


def test_the_policy_satisfies_the_contract():
    assert isinstance(RiskPolicy(), ToolPolicy)


# --- the table is the design's table ---------------------------------------


def test_the_default_table_is_section_3():
    assert DEFAULT_DECISIONS == {
        RiskLevel.LOW: Decision.ALLOW,
        RiskLevel.MEDIUM: Decision.ALLOW,
        RiskLevel.HIGH: Decision.ASK,
        RiskLevel.CRITICAL: Decision.ASK,
    }


@pytest.mark.parametrize("risk", list(RiskLevel))
def test_every_risk_level_has_a_decision(risk):
    """A new level added without a row would otherwise KeyError at run time."""
    assert risk in DEFAULT_DECISIONS


@pytest.mark.parametrize(
    "risk, expected",
    [(RiskLevel.LOW, Decision.ALLOW), (RiskLevel.MEDIUM, Decision.ALLOW),
     (RiskLevel.HIGH, Decision.ASK), (RiskLevel.CRITICAL, Decision.ASK)],
)
def test_decisions_follow_the_declared_risk(risk, expected):
    tool = spec("t", risk)
    assert RiskPolicy().decide(ToolRequest("t", {}), tool).decision is expected


# --- the gate is not advisory -------------------------------------------------


def test_an_unknown_tool_is_denied():
    decision = RiskPolicy().decide(ToolRequest("format_disk", {}), None)
    assert decision.decision is Decision.DENY
    assert "no such tool" in decision.reason


def test_a_request_matched_to_the_wrong_spec_is_denied():
    decision = RiskPolicy().decide(ToolRequest("write_file", {}), spec("read_file"))
    assert decision.decision is Decision.DENY


def test_an_override_may_tighten():
    policy = RiskPolicy({"read_file": Decision.ASK})
    policy.check_overrides({"read_file": spec()})
    assert policy.decide(ToolRequest("read_file", {}), spec()).decision is Decision.ASK


def test_an_override_may_deny_outright():
    policy = RiskPolicy({"run_command": Decision.DENY})
    tool = spec("run_command", RiskLevel.HIGH)
    policy.check_overrides({"run_command": tool})
    assert policy.decide(ToolRequest("run_command", {}), tool).decision is Decision.DENY


def test_an_override_that_loosens_is_refused_before_anything_runs():
    policy = RiskPolicy({"run_command": Decision.ALLOW})
    with pytest.raises(ValueError, match="only tighten"):
        policy.check_overrides({"run_command": spec("run_command", RiskLevel.HIGH)})


def test_an_override_that_loosens_is_ignored_even_if_never_checked():
    """Belt and braces: `decide` never applies a looser override either."""
    policy = RiskPolicy({"run_command": Decision.ALLOW})
    tool = spec("run_command", RiskLevel.CRITICAL)
    assert policy.decide(ToolRequest("run_command", {}), tool).decision is Decision.ASK


def test_an_override_for_a_missing_tool_is_refused():
    with pytest.raises(ValueError, match="does not exist"):
        RiskPolicy({"ghost": Decision.DENY}).check_overrides({})


# --- the types enforce the design ------------------------------------------


def test_a_tool_without_a_risk_level_cannot_be_declared():
    with pytest.raises(TypeError, match="does not execute"):
        ToolSpec(
            name="t", description="", risk_level="low",  # type: ignore[arg-type]
            input_schema={}, timeout_seconds=1, idempotent=True,
        )


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_tool_needs_a_positive_timeout(timeout):
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="", risk_level=RiskLevel.LOW,
                 input_schema={}, timeout_seconds=timeout, idempotent=True)


def test_a_request_cannot_be_changed_after_the_policy_saw_it():
    request = ToolRequest("read_file", {"path": "a.txt"})
    with pytest.raises(TypeError):
        request.arguments["path"] = "/etc/passwd"  # type: ignore[index]


def test_verification_fails_if_any_check_fails():
    result = VerificationResult(
        (VerificationCheck("schema", True, "ok"), VerificationCheck("policy", False, "leak"))
    )
    assert not result.passed
    assert VerificationResult((VerificationCheck("schema", True, "ok"),)).passed
