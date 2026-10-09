"""P0-4: run_command arguments respect file protections.

`validate_command` is workspace-agnostic and stays that way: it still allows
`cat .env` (it cannot know `.env` is protected). The workspace-aware refusal
lives in `RunCommand.run`, after validation and before execution, so a
refused command never runs. Nothing that `test_agent_commands.py` pins as
allowed may start failing here.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.commands import CommandRejected, validate_command
from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import RunCommand, _refuse_protected_arguments
from personal_ai_core.core.agent import ToolRequest


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("hello\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x\n", encoding="utf-8")
    return Workspace(root)


def check(ws, args):
    _refuse_protected_arguments(ws, args)


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "grep needle .env",
        "grep --file=.env x",
        "cat .git/config",
        "git show HEAD:.env",
        "find . -name .env",
    ],
)
def test_protected_names_are_refused(ws, command):
    with pytest.raises(CommandRejected, match="may not use"):
        check(ws, validate_command(command))


def test_a_reserved_database_is_refused(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    database = root / "core.db"
    database.write_bytes(b"db")
    sandbox = Workspace(root, reserved=(database,))
    with pytest.raises(CommandRejected, match="may not use"):
        check(sandbox, ["cat", "core.db"])


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git log --oneline",
        "git diff HEAD~1",
        "git show HEAD",
        "pytest -q",
        # A colon is a rev:path or an option value, not a file name. On
        # Windows the whole token would read as an alternate data stream.
        "pytest -q -p no:cacheprovider",
        "git show HEAD:notes.md",
        "ruff check .",
        "grep -n needle notes.md",
        "find . -name '*.py'",
        "wc -l README.md",
        "cat notes.md",
    ],
)
def test_ordinary_inspection_still_passes(ws, command):
    check(ws, validate_command(command))


def test_refusal_happens_before_execution(ws):
    with pytest.raises(CommandRejected, match="may not use"):
        RunCommand(ws).run({"command": "cat .env"})


def test_refusal_reaches_the_audit_as_a_failed_result(ws):
    # ADR-025 §4.6 semantics: a tool that ran and reported an error counts as
    # executed; what matters is the run failed closed with no subprocess.
    executor = ToolExecutor(
        [RunCommand(ws)], RiskPolicy(), confirm=lambda request, spec: True
    )
    record = executor.execute(ToolRequest("run_command", {"command": "cat .env"}))
    assert record.executed
    assert record.result is not None and not record.result.ok
    assert "may not use" in (record.result.error or "")
    assert "API_KEY" not in (record.result.output or "")
    assert executor.audit.records() == (record,)


@pytest.mark.parametrize("command", ["pytest -q -p no:cacheprovider", "git show HEAD:notes.md"])
def test_with_windows_name_rules_a_colon_is_still_not_a_file_name(ws, monkeypatch, command):
    """On Windows the sandbox reads `name:stream` as an NTFS stream and refuses it;
    a colon in a command argument is a rev:path or an option value. Switched on
    here, the Windows rules must not refuse the ordinary commands (they did, on
    the Windows runner) -- and must still refuse `git show HEAD:.env`."""
    from personal_ai_core.agent import sandbox

    monkeypatch.setattr(sandbox, "_windows_names_apply", lambda: True)
    check(ws, validate_command(command))
    with pytest.raises(CommandRejected, match="may not use"):
        check(ws, validate_command("git show HEAD:.env"))
