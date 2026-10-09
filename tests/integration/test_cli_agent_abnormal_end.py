"""N3: an agent task that ends early still offers its undo, and leaves a record.

Before this, only a task stopped by its action budget asked "undo?". When the
model server failed mid-task, or the owner pressed Ctrl-C, pac printed a
sentence (or a traceback) and exited: the files the task had changed stayed
changed, the rollback points -- held in memory -- were gone, and no
AGENT_FINISHED was written. A command running at the moment of a Ctrl-C was
left running in its own process group, after pac had exited.

Keep is the default everywhere (owner decisions A and B): end of input, or a
second Ctrl-C at the question, keeps the changes and names them.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from personal_ai_core.agent.tools import run_bounded
from personal_ai_core.app.cli import main
from personal_ai_core.core.errors import ProviderError

ORIGINAL = "original\n"
WRITE = '{"tool": "write_file", "arguments": {"path": "keep.md", "content": "changed\\n"}}'
TASK = "[action_required=true] change keep.md"


def scripted(*replies):
    """Each reply is the model's text, or an exception the transport raises."""
    queue = list(replies)

    def transport(url, payload, timeout):
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return {"model": payload["model"], "message": {"content": reply}}

    return transport


def pac(argv, **kwargs):
    """`main`, with an interrupt that escapes it failing the test rather than
    stopping pytest: an escaped Ctrl-C is the defect these tests look for."""
    try:
        return main(argv, **kwargs)
    except KeyboardInterrupt:
        pytest.fail("a KeyboardInterrupt escaped pac instead of ending the task")


def run(tmp_path, transport, *lines):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "keep.md").write_text(ORIGINAL, encoding="utf-8")
    out = io.StringIO()
    code = pac(["--database", str(tmp_path / "core.db"), "--agent", "--workspace",
                str(workspace)], transport=transport, stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue(), workspace / "keep.md"


def finishes(tmp_path):
    connection = sqlite3.connect(tmp_path / "core.db")
    try:
        rows = connection.execute(
            "select payload from events where type = 'agent.finished' order by seq").fetchall()
    finally:
        connection.close()
    return [json.loads(row[0]) for row in rows]


def server_error():
    return ProviderError("ollama request failed: HTTP Error 500", kind="http_status", status=500)


# --- the model server fails after the task changed a file ------------------------


def test_a_failed_model_server_mid_task_offers_the_undo_and_yes_restores(tmp_path):
    code, output, kept = run(tmp_path, scripted(WRITE, server_error()), TASK, "y")
    assert code == 1
    assert "the model could not be reached" in output
    assert "undo 1 file change(s) from this task (keep.md)?" in output
    assert "restored: keep.md" in output
    assert kept.read_text(encoding="utf-8") == ORIGINAL


def test_no_keeps_the_change(tmp_path):
    code, output, kept = run(tmp_path, scripted(WRITE, server_error()), TASK, "n")
    assert code == 1
    assert kept.read_text(encoding="utf-8") == "changed\n"
    assert "restored" not in output


def test_end_of_input_at_the_question_keeps_the_change(tmp_path):
    code, _, kept = run(tmp_path, scripted(WRITE, server_error()), TASK)
    assert code == 1
    assert kept.read_text(encoding="utf-8") == "changed\n"


def test_the_end_is_recorded_with_a_classification_and_the_files(tmp_path):
    run(tmp_path, scripted(WRITE, server_error()), TASK, "n")
    [finish] = finishes(tmp_path)
    assert finish["finished"] is False
    assert finish["stopped_reason"] == "provider_failure (http_status)"
    assert finish["touched_files"] == ["keep.md"]
    assert finish["steps"] == 1
    assert "500" not in json.dumps(finish)  # a classification, never the error's text


def test_a_task_that_changed_nothing_asks_nothing(tmp_path):
    code, output, kept = run(tmp_path, scripted(server_error()), TASK)
    assert code == 1
    assert "undo" not in output
    assert kept.read_text(encoding="utf-8") == ORIGINAL


# --- Ctrl-C -------------------------------------------------------------------------


def test_an_interrupt_mid_task_offers_the_undo_and_exits_130(tmp_path):
    code, output, kept = run(tmp_path, scripted(WRITE, KeyboardInterrupt()), TASK, "y")
    assert code == 130
    assert "interrupted" in output and "Traceback" not in output
    assert "restored: keep.md" in output
    assert kept.read_text(encoding="utf-8") == ORIGINAL
    [finish] = finishes(tmp_path)
    assert finish["stopped_reason"] == "interrupted" and finish["touched_files"] == ["keep.md"]


def test_an_interrupt_at_the_question_keeps_the_change_and_names_it(tmp_path):
    def lines():
        yield TASK
        raise KeyboardInterrupt

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "keep.md").write_text(ORIGINAL, encoding="utf-8")
    out = io.StringIO()
    code = pac(["--database", str(tmp_path / "core.db"), "--agent", "--workspace",
                str(workspace)], transport=scripted(WRITE, server_error()),
               stdin=lines(), stdout=out, env={})
    assert code == 130
    assert "kept: keep.md" in out.getvalue()
    assert (workspace / "keep.md").read_text(encoding="utf-8") == "changed\n"


# --- the store fails mid-task ----------------------------------------------------------


def test_a_store_failure_mid_task_still_offers_the_undo(tmp_path, monkeypatch):
    from personal_ai_core.persistence.sqlite import SqliteEventRepository

    original = SqliteEventRepository.append

    def failing(self, event):
        if event.type.value == "agent.step":
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, event)

    monkeypatch.setattr(SqliteEventRepository, "append", failing)
    code, output, kept = run(tmp_path, scripted(WRITE), TASK, "y")
    assert code == 1
    assert "undo 1 file change(s)" in output and "restored: keep.md" in output
    assert "failed: disk I/O error" in output  # the store's own sentence, from main
    assert kept.read_text(encoding="utf-8") == ORIGINAL


def test_when_the_end_cannot_be_recorded_the_original_failure_is_the_one_reported(tmp_path):
    from personal_ai_core.agent import RiskPolicy, ToolExecutor
    from personal_ai_core.agent.loop import AgentLoop
    from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator

    class Events:
        def append(self, event):
            if event.type.value == "agent.finished":
                raise sqlite3.OperationalError("database is locked")

        def list_for_session(self, session_id):
            return ()

    class DownProvider:
        name = "down"

        def generate(self, **kwargs):
            raise server_error()

    loop = AgentLoop(provider=DownProvider(), model="boss",  # type: ignore[arg-type]
                     executor=ToolExecutor([], RiskPolicy()), context_window=8192,
                     budget_policy=ReserveBasedBudgetPolicy(),
                     estimator=ScriptAwareTokenEstimator(),
                     events=Events(), session_exists=lambda s: True)  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        loop.run("x", session_id="s1")


# --- a defect mid-task ---------------------------------------------------------------------


def test_an_unexpected_exception_mid_task_still_offers_the_undo(tmp_path):
    """A failure pac has no sentence for still propagates -- the loop recorded
    it as internal_error -- but not before the changed files are offered back."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "keep.md").write_text(ORIGINAL, encoding="utf-8")
    out = io.StringIO()
    with pytest.raises(RuntimeError, match="a defect"):
        pac(["--database", str(tmp_path / "core.db"), "--agent", "--workspace",
             str(workspace)], transport=scripted(WRITE, RuntimeError("a defect")),
            stdin=iter([TASK, "y"]), stdout=out, env={})
    output = out.getvalue()
    assert "undo 1 file change(s) from this task (keep.md)?" in output
    assert "restored: keep.md" in output
    assert (workspace / "keep.md").read_text(encoding="utf-8") == ORIGINAL
    [finish] = finishes(tmp_path)
    assert finish["stopped_reason"] == "internal_error (RuntimeError)"
    assert finish["touched_files"] == ["keep.md"]


# --- a running command does not outlive the interrupt -----------------------------------


def test_an_interrupt_while_a_command_runs_ends_the_command(tmp_path, monkeypatch):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        """Interrupts the wait on the command under test, and on nothing else:
        on Windows the kill itself runs `taskkill` through Popen, and its wait
        must go through untouched. The wait is interrupted once, so the
        assertion afterwards can still reap the process."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)
            self._interrupted = False

        def wait(self, timeout=None):
            if self.args == command and not self._interrupted:
                self._interrupted = True
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    # Windows' Python does not start without SYSTEMROOT; elsewhere nothing is needed.
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}
    with pytest.raises(KeyboardInterrupt):
        run_bounded(command, cwd=tmp_path, env=env, timeout=30)
    process = next(p for p in started if p.args == command)
    # Killed with its tree, not left running: it exits well before its 60 s.
    assert process.wait(timeout=10) is not None
