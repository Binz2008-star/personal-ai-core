"""run_command says what a check runs.

Gap analysis P1-10. The allowlist admits pytest, ruff and mypy, and pytest
runs the project's own test code, which in a workspace the owner did not
write is anyone's code. The tool called itself "read-only or checking" and the
README "an allowlist of read-only and checking commands": both read as safe,
while the gate in front of that code is the owner's yes. The description the
model reads and the table the owner reads now say it, and the yes they rely on
is asked for every run.
"""
from __future__ import annotations

from pathlib import Path

from personal_ai_core.agent.commands import ALLOWED_EXECUTABLES
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import RunCommand
from personal_ai_core.core.agent import Decision, ToolRequest

README = Path(__file__).resolve().parents[2] / "README.md"
CHECKS = ("pytest", "ruff", "mypy")


def _row(name: str) -> str:
    [row] = [line for line in README.read_text(encoding="utf-8").splitlines()
             if line.startswith(f"| `{name}` |")]
    return row


def test_the_checks_are_still_on_the_allowlist():
    # What the sentences below are about. If a check leaves, they can go too.
    assert set(CHECKS) <= ALLOWED_EXECUTABLES


def test_the_description_says_a_check_runs_the_projects_code(tmp_path):
    description = RunCommand(Workspace(tmp_path)).spec.description
    assert "A check is not read-only" in description
    assert "pytest runs the project's own code" in description
    assert "configuration can make any check write files" in description
    assert "the owner is asked before every run" in description
    assert "read-only or checking" not in description


def test_the_readme_says_it_too():
    row = _row("run_command")
    assert "**asks you**, every time" in row
    assert "A check is not read-only: `pytest` runs the project's own code" in row


def test_every_run_is_asked_as_both_say(tmp_path):
    spec = RunCommand(Workspace(tmp_path)).spec
    for command in ("pytest", "ruff check .", "mypy ."):
        decision = RiskPolicy().decide(ToolRequest(tool="run_command",
                                                   arguments={"command": command}), spec)
        assert decision.decision is Decision.ASK
