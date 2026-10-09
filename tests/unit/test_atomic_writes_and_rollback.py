"""Writes are atomic, and a rollback that cannot restore one file restores the rest.

A direct write leaves a half-written file if the process dies mid-write; the
agent now writes through a temporary file and `os.replace`. A rollback that
hits an unrestorable file used to stop there, leaving the files after it
unrestored and forgetting their checkpoints; now each file is tried, the ones
that failed are named and keep their checkpoints for another try.
"""
from __future__ import annotations

import os
import stat
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
from personal_ai_core.agent.tools import DeleteFile, WriteFile


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

    def fail_for_notes(path, content, **kwargs):
        if path.name == "notes.md":
            raise OSError("locked")
        real(path, content, **kwargs)

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


# --- file modes -------------------------------------------------------------------------------
# mkstemp creates the temporary file 0600, and os.replace carried that onto the
# target: an executable script lost its x bit, a new file came out 0600.
# Windows has no mode bits beyond read-only, so these are POSIX-only.

posix_modes = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@posix_modes
def test_rewriting_a_file_keeps_its_mode_and_undo_restores_it(ws):
    script = ws.root / "run.sh"
    script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    script.chmod(0o755)
    checkpoints = Checkpoints(ws)
    assert WriteFile(ws, checkpoints).run({"path": "run.sh", "content": "echo new\n"}).ok
    assert _mode(script) == 0o755
    script.chmod(0o600)  # changed during the run, e.g. by a command
    assert checkpoints.rollback() == ("run.sh",)
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\necho old\n"
    assert _mode(script) == 0o755


@posix_modes
def test_a_new_file_gets_the_umask_default_not_0600(ws):
    current = os.umask(0o022)
    os.umask(current)
    assert WriteFile(ws).run({"path": "fresh.txt", "content": "x"}).ok
    assert _mode(ws.root / "fresh.txt") == 0o666 & ~current


@posix_modes
def test_undo_of_a_delete_restores_the_mode(ws):
    script = ws.root / "run.sh"
    script.write_text("echo\n", encoding="utf-8")
    script.chmod(0o750)
    checkpoints = Checkpoints(ws)
    assert DeleteFile(ws, checkpoints).run({"path": "run.sh"}).ok
    checkpoints.rollback()
    assert _mode(script) == 0o750


# --- directories a run created ----------------------------------------------------------------


def test_undo_removes_the_directories_a_write_created(ws):
    checkpoints = Checkpoints(ws)
    _touch(ws, checkpoints, "x/y/z.txt")
    assert checkpoints.rollback() == ("x/y/z.txt",)
    assert not (ws.root / "x").exists()
    assert sorted(p.name for p in ws.root.iterdir()) == ["notes.md"]


def test_a_file_turned_into_a_directory_gets_its_exact_content_back(ws):
    original = b"precious\r\n\x00bytes"
    (ws.root / "x").write_bytes(original)
    checkpoints = Checkpoints(ws)
    assert DeleteFile(ws, checkpoints).run({"path": "x"}).ok
    _touch(ws, checkpoints, "x/y.txt")
    assert (ws.root / "x").is_dir()
    assert checkpoints.rollback() == ("x/y.txt", "x")
    assert (ws.root / "x").is_file()
    assert (ws.root / "x").read_bytes() == original
    assert checkpoints.touched() == ()


def test_undo_never_removes_a_directory_that_existed_before(ws):
    (ws.root / "keep" / "empty").mkdir(parents=True)
    checkpoints = Checkpoints(ws)
    _touch(ws, checkpoints, "keep/empty/new.txt", "keep/sub/new.txt")
    checkpoints.rollback()
    assert (ws.root / "keep" / "empty").is_dir()
    assert not (ws.root / "keep" / "sub").exists()


def test_undo_leaves_a_created_directory_that_holds_files_it_did_not_create(ws):
    checkpoints = Checkpoints(ws)
    _touch(ws, checkpoints, "out/a.txt")
    (ws.root / "out" / "other.txt").write_text("not the run's\n", encoding="utf-8")
    assert checkpoints.rollback() == ("out/a.txt",)
    assert (ws.root / "out" / "other.txt").read_text(encoding="utf-8") == "not the run's\n"
    assert not (ws.root / "out" / "a.txt").exists()


# --- delete_file and symlinks -----------------------------------------------------------------


def test_delete_file_refuses_a_symlink_and_leaves_both_link_and_target(ws):
    link = ws.root / "link.md"
    try:
        link.symlink_to("notes.md")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    result = DeleteFile(ws, Checkpoints(ws)).run({"path": "link.md"})
    assert not result.ok
    assert "symlink" in (result.error or "")
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "hello\n"
    assert link.is_symlink()

