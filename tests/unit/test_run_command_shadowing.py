"""run_command refuses an allowlisted command that a workspace file would shadow.

Windows `CreateProcess` searches the working directory before `PATH`, so a
confirmed `ruff` could run a `ruff.exe` the model wrote into the workspace.
The check runs on Windows only, where that lookup happens.
"""
from __future__ import annotations

import sys

import pytest

from personal_ai_core.agent.commands import CommandRejected
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import RunCommand, _refuse_shadowed_executable


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def test_a_command_the_workspace_shadows_is_refused_on_windows(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: str(ws.root / "ruff.exe"))
    with pytest.raises(CommandRejected, match="shadows the command"):
        _refuse_shadowed_executable(ws, "ruff")


def test_a_system_binary_passes_on_windows(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: r"C:\Python\Scripts\ruff.exe")
    _refuse_shadowed_executable(ws, "ruff")


def test_a_missing_binary_is_left_to_the_not_installed_path(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: None)
    _refuse_shadowed_executable(ws, "ruff")


def test_off_windows_the_lookup_is_not_consulted(ws, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def fail(name):
        raise AssertionError("which must not be consulted off Windows")

    monkeypatch.setattr("shutil.which", fail)
    _refuse_shadowed_executable(ws, "ruff")


def test_run_command_refuses_a_shadowed_binary_before_running_it(ws, monkeypatch):
    (ws.root / "ruff.exe").write_bytes(b"fake")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: str(ws.root / "ruff.exe"))
    with pytest.raises(CommandRejected, match="shadows the command"):
        RunCommand(ws).run({"command": "ruff --version"})
    assert (ws.root / "ruff.exe").read_bytes() == b"fake"
