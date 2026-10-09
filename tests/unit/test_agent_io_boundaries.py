"""Resource and alias regressions, isolated from the owner's files and model.

Memory is measured inside disposable Python processes on both platforms. Linux
also imposes an address-space limit; only the actual FIFO case is POSIX-only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from personal_ai_core.agent import tools as tools_module
from personal_ai_core.agent import recovery as recovery_module
from personal_ai_core.agent.commands import CommandRejected
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import SandboxError, Workspace
from personal_ai_core.agent.tools import DeleteFile, ReadFile, RunCommand, SearchText, WriteFile


@pytest.mark.parametrize("protected", [
    ".env", "credentials.json", "nested/private.key", ".git", ".git/config", ".git/objects/private",
])
@pytest.mark.parametrize("operation", ["read", "search", "write", "delete", "command"])
def test_a_hard_link_cannot_bypass_a_protected_name(tmp_path, monkeypatch, protected, operation):
    secret = tmp_path / protected
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("vault-canary-62831\n", encoding="utf-8")
    alias = tmp_path / "ordinary.txt"
    os.link(secret, alias)
    workspace = Workspace(tmp_path)

    def never_run(*args, **kwargs):
        pytest.fail("a protected hard-link operand reached a subprocess")

    monkeypatch.setattr(tools_module, "run_bounded", never_run)
    if operation == "read":
        with pytest.raises(SandboxError, match="protected"):
            ReadFile(workspace).run({"path": alias.name})
    elif operation == "search":
        result = SearchText(workspace).run({"text": "vault-canary"})
        assert result.ok and "vault-canary" not in result.output
    elif operation == "write":
        with pytest.raises(SandboxError, match="protected"):
            WriteFile(workspace).run({"path": alias.name, "content": "replacement"})
    elif operation == "delete":
        with pytest.raises(SandboxError, match="protected"):
            DeleteFile(workspace, Checkpoints(workspace)).run({"path": alias.name})
    else:
        with pytest.raises(CommandRejected, match="protected"):
            RunCommand(workspace).run({"command": "cat ordinary.txt"})
    assert secret.read_text(encoding="utf-8") == "vault-canary-62831\n"
    assert alias.samefile(secret)


def test_an_ordinary_hard_link_is_still_readable(tmp_path):
    original = tmp_path / "original.txt"
    original.write_text("public needle\n", encoding="utf-8")
    os.link(original, tmp_path / "alias.txt")
    workspace = Workspace(tmp_path)
    assert ReadFile(workspace).run({"path": "alias.txt"}).output == "public needle\n"
    assert "alias.txt:1: public needle" in SearchText(workspace).run({"text": "needle"}).output


def test_search_discloses_when_a_late_match_is_outside_its_read_budget(tmp_path):
    (tmp_path / "large.txt").write_text("x" * 1_000_001 + "\nlate-needle\n", encoding="utf-8")
    result = SearchText(Workspace(tmp_path)).run({"text": "late-needle"})
    assert result.ok
    assert "late-needle" in result.output or result.truncated


PROBE = r'''
import json, os, sys, tracemalloc
from pathlib import Path
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import ReadFile, SearchText, WriteFile, DeleteFile, run_bounded
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import SandboxError
if sys.platform == 'linux':
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
mode, directory = sys.argv[1:]
workspace = Workspace(Path(directory))
tracemalloc.start()
if mode.startswith('checkpoint-'):
    checkpoint = Checkpoints(workspace)
    target = Path(directory) / 'large.txt'
    try:
        if mode == 'checkpoint-write':
            WriteFile(workspace, checkpoint).run({'path': 'large.txt', 'content': 'replacement'})
        else:
            DeleteFile(workspace, checkpoint).run({'path': 'large.txt'})
    except SandboxError as error:
        data = {'refused': True, 'error': str(error)}
    else:
        data = {'refused': False}
    data['original_size'] = target.stat().st_size if target.exists() else None
    data['touched'] = checkpoint.touched()
elif mode == 'read':
    result = ReadFile(workspace).run({'path': 'large.txt'})
    data = {'ok': result.ok, 'size': len(result.output), 'truncated': result.truncated}
elif mode in ('search', 'matches', 'fifo'):
    result = SearchText(workspace).run({'text': 'needle'})
    data = {'ok': result.ok, 'size': len(result.output), 'truncated': result.truncated}
    if mode == 'fifo':
        data['output'] = result.output
else:
    producer = (
        "import sys\nblock = b'x' * 65536\n"
        "for _ in range(512):\n    sys.stdout.buffer.write(block)\n"
        "for _ in range(512):\n    sys.stderr.buffer.write(block)\n"
    )
    code, stdout, stderr = run_bounded([sys.executable, '-c', producer],
                                      cwd=directory, env=dict(os.environ), timeout=20)
    data = {'ok': code == 0, 'size': len(stdout) + len(stderr)}
data['peak_bytes'] = tracemalloc.get_traced_memory()[1]
print(json.dumps(data))
'''


def _probe(mode: str, directory: Path, timeout: int = 30) -> dict:
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", PROBE, mode, str(directory)], cwd=directory,
        env=environment, capture_output=True, text=True, timeout=timeout,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


@pytest.mark.parametrize("mode", ["read", "search", "matches", "capture"])
def test_large_inputs_are_bounded_before_output_is_collected(tmp_path, mode):
    if mode in {"read", "search"}:
        with (tmp_path / "large.txt").open("wb") as handle:
            block = b"x" * (1024 * 1024)
            for _ in range(32):
                handle.write(block)
    elif mode == "matches":
        line = "needle " + "x" * 900_000
        for index in range(32):
            (tmp_path / f"long-{index:02}.txt").write_text(line, encoding="utf-8")
    result = _probe(mode, tmp_path)
    assert result["ok"]
    assert result["size"] <= (20_001 if mode == "capture" else 20_000)
    if mode == "capture":
        assert result["size"] == 20_001, "capture must preserve its bounded prefix"
    else:
        assert result["truncated"], "shortened input must disclose incomplete coverage"
    assert result["peak_bytes"] < 16 * 1024 * 1024, "output was bounded only after allocation"


@pytest.mark.skipif(os.name == "nt", reason="POSIX named pipes")
def test_search_skips_a_fifo_before_opening_it(tmp_path):
    os.mkfifo(tmp_path / "blocked-pipe")
    (tmp_path / "ordinary.txt").write_text("needle in a regular file\n", encoding="utf-8")
    result = _probe("fifo", tmp_path, timeout=5)
    assert result["ok"] and "needle in a regular file" in result["output"]


@pytest.mark.parametrize("mode", ["checkpoint-write", "checkpoint-delete"])
def test_large_originals_are_refused_before_checkpoint_allocation_or_mutation(tmp_path, mode):
    with (tmp_path / "large.txt").open("wb") as handle:
        block = b"x" * (1024 * 1024)
        for _ in range(96):
            handle.write(block)
    result = _probe(mode, tmp_path)
    assert result["refused"], "a large original was loaded into the rollback snapshot"
    assert "checkpoint" in result["error"] and "budget" in result["error"]
    assert result["original_size"] == 96 * 1024 * 1024
    assert not result["touched"], "refusal must not keep a partial snapshot"
    assert result["peak_bytes"] < 32 * 1024 * 1024


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_checkpoint_budget_is_shared_and_released_between_tasks(tmp_path, monkeypatch, release):
    monkeypatch.setattr(recovery_module, "MAX_CHECKPOINT_BYTES", 16, raising=False)
    original = tmp_path / "first.txt"
    original.write_bytes(b"a" * 12)
    second = tmp_path / "second.txt"
    second.write_bytes(b"b" * 12)
    checkpoint = Checkpoints(Workspace(tmp_path))
    checkpoint.before_mutation(original)
    original.write_bytes(b"edited")
    with pytest.raises(SandboxError, match="checkpoint.*budget"):
        checkpoint.before_mutation(second)
    assert second.read_bytes() == b"b" * 12
    assert checkpoint.touched() == ("first.txt",)
    getattr(checkpoint, release)()
    assert original.read_bytes() == (b"edited" if release == "commit" else b"a" * 12)
    checkpoint.before_mutation(second)
    second.unlink()
    checkpoint.rollback()
    assert second.read_bytes() == b"b" * 12


def test_an_unrestored_checkpoint_still_spends_the_budget(tmp_path, monkeypatch):
    from personal_ai_core.core.errors import RollbackIncomplete

    monkeypatch.setattr(recovery_module, "MAX_CHECKPOINT_BYTES", 16, raising=False)
    original = tmp_path / "first.txt"
    original.write_bytes(b"a" * 12)
    second = tmp_path / "second.txt"
    second.write_bytes(b"b" * 12)
    checkpoint = Checkpoints(Workspace(tmp_path))
    checkpoint.before_mutation(original)
    original.write_bytes(b"edited")
    write = recovery_module.atomic_write_bytes

    def fail(*args, **kwargs):
        raise PermissionError("temporary refusal")

    monkeypatch.setattr(recovery_module, "atomic_write_bytes", fail)
    with pytest.raises(RollbackIncomplete):
        checkpoint.rollback()
    with pytest.raises(SandboxError, match="checkpoint.*budget"):
        checkpoint.before_mutation(second)
    assert checkpoint.touched() == ("first.txt",)
    monkeypatch.setattr(recovery_module, "atomic_write_bytes", write)
    checkpoint.rollback()
    assert original.read_bytes() == b"a" * 12


def test_checkpoint_read_remains_bounded_if_a_file_grows_after_stat(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(recovery_module, "MAX_CHECKPOINT_BYTES", 16, raising=False)
    target = tmp_path / "growing.txt"
    target.write_bytes(b"x" * 1024)
    checkpoint = Checkpoints(Workspace(tmp_path))
    real_stat = Path.stat

    def stale_stat(path, *args, **kwargs):
        actual = real_stat(path, *args, **kwargs)
        if path == target:
            snapshot = Mock(wraps=actual)
            snapshot.st_size = 0
            snapshot.st_mode = actual.st_mode
            return snapshot
        return actual

    monkeypatch.setattr(Path, "stat", stale_stat)
    with pytest.raises(SandboxError, match="checkpoint.*budget"):
        checkpoint.before_mutation(target)
    assert not checkpoint.touched()
    assert target.read_bytes() == b"x" * 1024
