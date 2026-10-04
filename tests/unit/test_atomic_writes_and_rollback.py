"""Writes are atomic, and a rollback that cannot restore one file restores the rest.

A direct write leaves a half-written file if the process dies mid-write; the
agent now writes through a temporary file and `os.replace`. A rollback that
hits an unrestorable file used to stop there, leaving the files after it
unrestored and forgetting their checkpoints; now each file is tried, the ones
that failed are named and keep their checkpoints for another try.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_core.agent import recovery
from personal_ai_core.agent.recovery import (
    Checkpoints,
    RollbackIncomplete,
    atomic_write_bytes,
    atomic_write_text,
)
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import WriteFile


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("hello\n", encoding="utf-8")
    return Workspace(root)


def _leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("*.tmp"))


# --- atomic writes ------------------------------------------------------------------------


def test_write_file_writes_and_leaves_no_temporary_file(ws):
    result = WriteFile(ws, Checkpoints(ws)).run({"path": "notes.md", "content": "a\nb\n"})
    assert result.ok
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "a\nb\n"
    assert _leftovers(ws.root) == []


def test_text_is_written_byte_for_byte_as_write_text_writes_it(tmp_path):
    content = "line one\nسطر\n"
    atomic_write_text(tmp_path / "atomic.txt", content)
    (tmp_path / "plain.txt").write_text(content, encoding="utf-8")
    assert (tmp_path / "atomic.txt").read_bytes() == (tmp_path / "plain.txt").read_bytes()


def test_bytes_are_written_exactly(tmp_path):
    atomic_write_bytes(tmp_path / "f.bin", b"\x00a\r\nb")
    assert (tmp_path / "f.bin").read_bytes() == b"\x00a\r\nb"


def test_a_failure_before_the_replace_leaves_the_old_file_and_no_temporary(ws, monkeypatch):
    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(recovery.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write_text(ws.root / "notes.md", "half")
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "hello\n"
    assert _leftovers(ws.root) == []


# --- rollback -------------------------------------------------------------------------------


def _touch(ws, checkpoints, *names):
    write = WriteFile(ws, checkpoints)
    for name in names:
        assert write.run({"path": name, "content": f"changed {name}\n"}).ok


def test_rollback_restores_every_file_in_reverse_order(ws):
    checkpoints = Checkpoints(ws)
    _touch(ws, checkpoints, "notes.md", "new.md")
    assert checkpoints.rollback() == ("new.md", "notes.md")
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "hello\n"
    assert not (ws.root / "new.md").exists()
    assert checkpoints.touched() == ()


def test_one_unrestorable_file_does_not_strand_the_rest_and_keeps_its_checkpoint(
        ws, monkeypatch):
    (ws.root / "a.md").write_text("a\n", encoding="utf-8")
    (ws.root / "b.md").write_text("b\n", encoding="utf-8")
    checkpoints = Checkpoints(ws)
    _touch(ws, checkpoints, "a.md", "notes.md", "b.md")
    real = recovery.atomic_write_bytes

    def fail_for_notes(path, content):
        if path.name == "notes.md":
            raise OSError("locked")
        real(path, content)

    monkeypatch.setattr(recovery, "atomic_write_bytes", fail_for_notes)
    with pytest.raises(RollbackIncomplete) as caught:
        checkpoints.rollback()
    assert caught.value.restored == ("b.md", "a.md")
    assert caught.value.unrestored == ("notes.md",)
    assert "notes.md" in str(caught.value)
    # The files after the failure were restored; the failed one is still known.
    assert (ws.root / "a.md").read_text(encoding="utf-8") == "a\n"
    assert (ws.root / "b.md").read_text(encoding="utf-8") == "b\n"
    assert checkpoints.touched() == ("notes.md",)

    # Once the fault is gone, the kept checkpoint restores it.
    monkeypatch.setattr(recovery, "atomic_write_bytes", real)
    assert checkpoints.rollback() == ("notes.md",)
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "hello\n"

