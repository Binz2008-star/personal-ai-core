"""The agent's tools, run directly -- the executor is tested separately."""
from __future__ import annotations

import subprocess

import pytest

from personal_ai_core.agent import tools as tools_module
from personal_ai_core.agent.commands import CommandRejected
from personal_ai_core.agent.sandbox import SandboxError, Workspace
from personal_ai_core.agent.tools import (
    MAX_OUTPUT_CHARS,
    ListDirectory,
    ReadFile,
    RunCommand,
    SearchText,
    WriteFile,
    default_tools,
)
from personal_ai_core.core.agent import RiskLevel
from personal_ai_core.core.contracts import Tool


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("alpha\nReciprocal Rank Fusion\nomega\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('fusion')\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret-fusion\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("fusion in git\n", encoding="utf-8")
    return Workspace(root)


def test_every_default_tool_satisfies_the_contract(ws):
    for tool in default_tools(ws):
        assert isinstance(tool, Tool)


def test_the_declared_risk_levels_are_the_designs(ws):
    """Section 3's examples: read/search/list LOW, write MEDIUM, command HIGH."""
    assert {t.spec.name: t.spec.risk_level for t in default_tools(ws)} == {
        "read_file": RiskLevel.LOW,
        "list_directory": RiskLevel.LOW,
        "search_text": RiskLevel.LOW,
        "write_file": RiskLevel.MEDIUM,
        "run_command": RiskLevel.HIGH,
    }


# --- read ---------------------------------------------------------------------


def test_read_returns_the_text(ws):
    result = ReadFile(ws).run({"path": "notes.md"})
    assert result.ok and "Reciprocal Rank Fusion" in result.output


def test_read_refuses_a_secret(ws):
    """A secret read into the model's context is a secret disclosed."""
    with pytest.raises(SandboxError, match="may not read"):
        ReadFile(ws).run({"path": ".env"})


def test_read_refuses_an_escape(ws):
    with pytest.raises(SandboxError):
        ReadFile(ws).run({"path": "../outside.txt"})


def test_read_reports_a_missing_file(ws):
    result = ReadFile(ws).run({"path": "nope.md"})
    assert not result.ok and "not a file" in (result.error or "")


def test_read_is_bounded_and_says_so(ws):
    (ws.root / "big.txt").write_text("x" * (MAX_OUTPUT_CHARS + 10), encoding="utf-8")
    result = ReadFile(ws).run({"path": "big.txt"})
    assert result.truncated and len(result.output) == MAX_OUTPUT_CHARS


def test_read_refuses_binary(ws):
    (ws.root / "blob.bin").write_bytes(b"\xff\xfe\x00")
    assert not ReadFile(ws).run({"path": "blob.bin"}).ok


# --- list and search -------------------------------------------------------------


def test_list_marks_directories_and_hides_git(ws):
    result = ListDirectory(ws).run({})
    assert result.output.splitlines() == [".env", "notes.md", "src/"]


def test_search_finds_lines_and_skips_secrets_and_git(ws):
    result = SearchText(ws).run({"text": "fusion"})
    lines = result.output.splitlines()
    assert "notes.md:2: Reciprocal Rank Fusion" in lines
    assert "src/app.py:1: print('fusion')" in lines
    assert not any(line.startswith((".env", ".git")) for line in lines)


def test_search_can_be_case_sensitive(ws):
    result = SearchText(ws).run({"text": "fusion", "case_sensitive": True})
    assert result.output == "src/app.py:1: print('fusion')"


def test_search_says_when_nothing_matched(ws):
    assert SearchText(ws).run({"text": "zzz"}).output == "no matches"


def test_search_does_not_follow_a_symlink_out(tmp_path, ws):
    """A symlinked FILE: os.walk already declines to descend into a linked
    directory, so a linked directory would not test the sandbox at all."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("fusion outside\n", encoding="utf-8")
    try:
        (ws.root / "leak.txt").symlink_to(outside / "leak.txt")
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert "outside" not in SearchText(ws).run({"text": "fusion"}).output


# --- write -----------------------------------------------------------------------


def test_write_creates_and_reports(ws):
    result = WriteFile(ws).run({"path": "out/new.md", "content": "hi"})
    assert result.ok and result.output == "created out/new.md (2 characters)"
    assert (ws.root / "out" / "new.md").read_text(encoding="utf-8") == "hi"


def test_write_says_when_it_overwrote(ws):
    assert WriteFile(ws).run({"path": "notes.md", "content": "x"}).output.startswith("overwrote")


def test_write_refuses_a_secret(ws):
    with pytest.raises(SandboxError, match="protected"):
        WriteFile(ws).run({"path": ".env", "content": "API_KEY=mine"})
    assert "secret-fusion" in (ws.root / ".env").read_text(encoding="utf-8")


def test_write_refuses_git(ws):
    with pytest.raises(SandboxError):
        WriteFile(ws).run({"path": ".git/hooks/pre-commit", "content": "x"})


# --- command -----------------------------------------------------------------------


def test_a_rejected_command_never_reaches_a_process(ws, monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(CommandRejected):
        RunCommand(ws).run({"command": "python evil.py"})
    assert called == []


def test_a_command_runs_without_a_shell_in_the_workspace(ws, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)
    result = RunCommand(ws).run({"command": "git status"})
    assert result.ok and result.output == "ok\n"
    assert seen["args"] == ["git", "status"]
    assert seen["shell"] is False
    assert seen["cwd"] == ws.root


def test_the_command_environment_is_built_from_nothing(ws, monkeypatch):
    """The source blanked four named secrets and passed everything else."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)
    RunCommand(ws).run({"command": "git status"})
    assert "OPENAI_API_KEY" not in seen["env"]
    assert seen["env"]["HOME"] == str(ws.root)


def test_a_failing_command_is_a_failed_result_with_its_output(ws, monkeypatch):
    monkeypatch.setattr(
        tools_module.subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(args, 128, stdout="", stderr="not a repo"),
    )
    result = RunCommand(ws).run({"command": "git status"})
    assert not result.ok and result.error == "exit code 128" and "not a repo" in result.output


def test_a_timeout_is_reported(ws, monkeypatch):
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(tools_module.subprocess, "run", slow)
    result = RunCommand(ws, timeout_seconds=3).run({"command": "git log"})
    assert not result.ok and result.error == "timed out after 3s"


def test_a_real_command_really_runs(ws):
    """One end-to-end run, no fakes. git exists on every CI platform; the
    workspace is not a repository, so git exits non-zero -- which is still a
    process that ran, was captured, and reported its failure."""
    result = RunCommand(ws).run({"command": "git status"})
    assert not result.ok and result.error and result.error.startswith("exit code")
