"""VERIFY and RECOVER -- AGENT_ARCHITECTURE.md sections 5 and 6."""
from __future__ import annotations

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import ActionBudget, Checkpoints
from personal_ai_core.agent.sandbox import SandboxError, Workspace
from personal_ai_core.agent.tools import DeleteFile, WriteFile, default_tools
from personal_ai_core.agent.verifier import Expectation, Verifier, find_secrets
from personal_ai_core.core.agent import (
    AuditRecord,
    Decision,
    PolicyDecision,
    RiskLevel,
    ToolRequest,
    ToolResult,
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "keep.md").write_text("original\n", encoding="utf-8")
    return Workspace(root)


DONE = ToolResult(ok=True, output="done")


def record(
    decision: Decision = Decision.ALLOW,
    result: ToolResult | None = DONE,
    confirmed: bool = False,
) -> AuditRecord:
    return AuditRecord(
        request=ToolRequest("t", {}),
        risk_level=RiskLevel.LOW,
        decision=PolicyDecision(decision, "reason"),
        result=result,
        confirmed_by_user=confirmed,
    )


# --- VERIFY ------------------------------------------------------------------------


def test_a_clean_successful_step_passes():
    assert Verifier().verify(record()).passed


def test_a_denied_step_fails_verification():
    result = Verifier().verify(record(Decision.DENY, result=None))
    assert not result.passed and result.checks[0].reason.startswith("denied")


def test_an_unconfirmed_ask_fails_verification():
    unconfirmed = record(Decision.ASK, ToolResult(ok=False, error="not confirmed"))
    assert not Verifier().verify(unconfirmed).passed


def ran(result):
    return next(check for check in result.checks if check.name == "ran")


def test_an_unconfirmed_ask_did_not_run_even_if_the_result_says_ok():
    """The `ran` check alone. A result claiming success for an ASK nobody
    confirmed is a contradiction, and the verifier must not believe it."""
    check = ran(Verifier().verify(record(Decision.ASK, DONE, confirmed=False)))
    assert not check.passed and check.reason == "not confirmed by the user"


def test_an_allowed_step_with_no_result_did_not_run():
    check = ran(Verifier().verify(record(Decision.ALLOW, result=None)))
    assert not check.passed and check.reason == "no result was recorded"


def test_a_confirmed_ask_that_succeeded_passes():
    assert Verifier().verify(record(Decision.ASK, confirmed=True)).passed


def test_a_tool_failure_fails_verification():
    failed = record(result=ToolResult(ok=False, error="exit code 1"))
    names = {c.name: c.passed for c in Verifier().verify(failed).checks}
    assert names["succeeded"] is False


def test_a_secret_in_the_output_fails_verification():
    leaked = record(result=ToolResult(ok=True, output="token = ghp_" + "a" * 36))
    result = Verifier().verify(leaked)
    assert not result.passed
    assert "GitHub token" in [c for c in result.checks if c.name == "no secret in output"][0].reason


def test_an_unmet_expectation_fails_verification():
    result = Verifier().verify(record(), Expectation(output_contains="3 passed"))
    assert not result.passed


def test_a_met_expectation_passes():
    assert Verifier().verify(record(), Expectation(output_contains="done")).passed


def test_the_response_is_checked_for_secrets():
    assert not Verifier().verify_response("the key is AKIA" + "A" * 16).passed
    assert Verifier().verify_response("all tests passed").passed


@pytest.mark.parametrize(
    "text, kind",
    [
        ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
        ("AKIA" + "B" * 16, "AWS access key"),
        ("ghp_" + "c" * 36, "GitHub token"),
        ("sk-" + "d" * 24, "OpenAI-style key"),
        ("hf_" + "e" * 32, "Hugging Face token"),
        ("xoxb-" + "1" * 12, "Slack token"),
        ("password = hunter2hunter2", "credential assignment"),
        ("postgres://user:pw@db.example.com/x", "connection string"),
    ],
)
def test_each_secret_shape_is_recognised(text, kind):
    assert kind in find_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        "the password field is required",
        "ask for a token",
        "sk-short",
        "AKIA123",
        "https://example.com/path",
        "api_key: <redacted>",
        "API_KEY=${API_KEY}",
        "token: {{ secrets.token }}",
        "password = ********",
    ],
)
def test_near_misses_are_not_secrets(text):
    """High precision: a check that fires on ordinary text gets ignored."""
    assert find_secrets(text) == []


# --- RECOVER: the budget ------------------------------------------------------------------


def test_the_budget_allows_until_the_action_limit():
    budget = ActionBudget(max_actions=2, max_failures=5)
    budget.record(ok=True)
    assert budget.allowed()
    budget.record(ok=True)
    assert not budget.allowed()
    assert budget.summary() == "stopped: 2 actions reached the limit of 2"


def test_the_budget_stops_after_repeated_failure():
    budget = ActionBudget(max_actions=10, max_failures=2)
    budget.record(ok=False)
    budget.record(ok=False)
    assert not budget.allowed()
    assert budget.summary().startswith("stopped: 2 failed actions")


def test_the_budget_reports_progress():
    budget = ActionBudget(max_actions=5, max_failures=2)
    budget.record(ok=False)
    assert budget.summary() == "1/5 actions, 1 failed"


def test_a_budget_must_allow_something():
    with pytest.raises(ValueError):
        ActionBudget(max_actions=0)


# --- RECOVER: rollback points ----------------------------------------------------------------


def test_a_write_can_be_rolled_back(ws):
    checkpoints = Checkpoints(ws)
    tool = WriteFile(ws, checkpoints)
    tool.run({"path": "keep.md", "content": "changed"})
    tool.run({"path": "keep.md", "content": "changed again"})
    tool.run({"path": "new/file.md", "content": "new"})
    assert checkpoints.touched() == ("keep.md", "new/file.md")

    assert checkpoints.rollback() == ("new/file.md", "keep.md")
    assert (ws.root / "keep.md").read_text(encoding="utf-8") == "original\n"
    assert not (ws.root / "new" / "file.md").exists()


def test_a_delete_can_be_rolled_back(ws):
    checkpoints = Checkpoints(ws)
    result = DeleteFile(ws, checkpoints).run({"path": "keep.md"})
    assert result.ok and not (ws.root / "keep.md").exists()
    checkpoints.rollback()
    assert (ws.root / "keep.md").read_text(encoding="utf-8") == "original\n"


def test_delete_cannot_exist_without_a_rollback_point(ws):
    """CRITICAL: "rollback point first" is a constructor argument."""
    with pytest.raises(TypeError, match="rollback point"):
        DeleteFile(ws, None)  # type: ignore[arg-type]


def test_delete_is_critical_and_asked(ws):
    tool = DeleteFile(ws, Checkpoints(ws))
    assert tool.spec.risk_level is RiskLevel.CRITICAL
    ex = ToolExecutor([tool], RiskPolicy())
    outcome = ex.execute(ToolRequest("delete_file", {"path": "keep.md"}))
    assert outcome.decision.decision is Decision.ASK
    assert (ws.root / "keep.md").exists()


def test_delete_refuses_secrets_and_directories(ws):
    (ws.root / ".env").write_text("X=1", encoding="utf-8")
    (ws.root / "dir").mkdir()
    tool = DeleteFile(ws, Checkpoints(ws))
    with pytest.raises(SandboxError):
        tool.run({"path": ".env"})
    assert not tool.run({"path": "dir"}).ok


def test_delete_is_offered_only_with_checkpoints(ws):
    assert "delete_file" not in {t.spec.name for t in default_tools(ws)}
    assert "delete_file" in {t.spec.name for t in default_tools(ws, Checkpoints(ws))}


def test_write_refuses_a_directory(ws):
    (ws.root / "dir").mkdir()
    assert not WriteFile(ws).run({"path": "dir", "content": "x"}).ok
