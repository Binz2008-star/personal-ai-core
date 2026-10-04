"""Wave 1 hardening: P0-5 (shell env from nothing), P1-9 (no workspace
shadowing), P0-7 (atomic writes, isolated rollback).

P0-4 has its own file (test_run_command_protections.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_ai_core.agent.commands import CommandRejected
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import (
    RunCommand,
    WriteFile,
    _refuse_shadowed_executable,
    shell_environment,
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("hello\n", encoding="utf-8")
    return Workspace(root)


# --- P0-5 --------------------------------------------------------------------


def test_shell_environment_is_fixed_keys_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("MY_CUSTOM_SECRET", "s")
    env = shell_environment(tmp_path)
    assert set(env) >= {"PATH", "HOME", "LANG", "GIT_TERMINAL_PROMPT"}
    assert env["HOME"] == str(tmp_path)
    assert "AWS_ACCESS_KEY_ID" not in env and "MY_CUSTOM_SECRET" not in env


def test_shell_run_uses_the_fixed_environment(ws, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_SECRET", "s")
    seen: dict = {}
    import personal_ai_core.agent.tools as tools

    real = tools.run_bounded

    def spy(args, **kwargs):
        seen.update(kwargs.get("env", {}))
        return real(args, **kwargs)

    monkeypatch.setattr(tools, "run_bounded", spy)
    result = tools.Shell(ws).run({"command": "echo hi"})
    assert "MY_CUSTOM_SECRET" not in seen and seen["HOME"] == str(ws.root)
    assert result.ok


# --- P1-9 --------------------------------------------------------------------


def test_shadowed_executable_is_refused_on_windows(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "shutil.which", lambda name: str(ws.root / "ruff.exe")
    )
    with pytest.raises(CommandRejected, match="shadows the command"):
        _refuse_shadowed_executable(ws, "ruff")


def test_system_executable_passes_on_windows(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: r"C:\Python\Scripts\ruff.exe")
    _refuse_shadowed_executable(ws, "ruff")


def test_missing_executable_is_left_to_the_not_installed_path(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: None)
    _refuse_shadowed_executable(ws, "ruff")


def test_no_shadow_check_off_windows(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def fail(name):
        raise AssertionError("which must not be consulted off Windows")

    monkeypatch.setattr("shutil.which", fail)
    _refuse_shadowed_executable(ws, "ruff")


def test_run_command_refuses_a_shadowed_binary(ws, monkeypatch):
    (ws.root / "ruff.exe").write_bytes(b"fake")
    monkeypatch.setattr(
        "shutil.which", lambda name: str(ws.root / "ruff.exe")
    )
    with pytest.raises(CommandRejected, match="shadows the command"):
        RunCommand(ws).run({"command": "ruff --version"})
    assert (ws.root / "ruff.exe").read_bytes() == b"fake"


# --- P0-7 --------------------------------------------------------------------


def test_write_leaves_no_temporary_files(ws):
    WriteFile(ws).run({"path": "sub/a.txt", "content": "v1"})
    leftovers = [p for p in (ws.root / "sub").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert (ws.root / "sub" / "a.txt").read_text(encoding="utf-8") == "v1"


def test_rollback_restores_what_it_can_and_names_what_it_cannot(ws):
    (ws.root / "good.txt").write_text("before", encoding="utf-8")
    (ws.root / "bad.txt").write_text("before", encoding="utf-8")
    checkpoints = Checkpoints(ws)
    checkpoints.before_mutation(ws.root / "good.txt")
    checkpoints.before_mutation(ws.root / "bad.txt")
    (ws.root / "good.txt").write_text("after", encoding="utf-8")
    (ws.root / "bad.txt").write_text("after", encoding="utf-8")
    # Sabotage one restore only: a directory where the file was makes the
    # atomic replace fail, while the sibling must still be restored.
    (ws.root / "bad.txt").unlink()
    (ws.root / "bad.txt").mkdir()
    with pytest.raises(RuntimeError, match=r"unrestored: bad\.txt"):
        checkpoints.rollback()
    assert (ws.root / "good.txt").read_text(encoding="utf-8") == "before"
    assert (ws.root / "bad.txt").is_dir()


def test_rollback_still_returns_restored_in_order(ws):
    (ws.root / "a.txt").write_text("0", encoding="utf-8")
    checkpoints = Checkpoints(ws)
    checkpoints.before_mutation(ws.root / "a.txt")
    (ws.root / "a.txt").write_text("1", encoding="utf-8")
    assert checkpoints.rollback() == ("a.txt",)
    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "0"
    assert list(Path(ws.root).glob("*.tmp")) == []
