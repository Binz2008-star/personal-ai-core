"""The owner's profile is never left torn, and a bad one is a sentence.

Gap analysis P2-7. `--remember` rewrote profile.md in place, so a full disk or
a crash mid-write could leave it truncated; an unreadable profile escaped as
a traceback. Now the write is atomic (a temporary file, fsynced, renamed over
the old one) and both failures end in a sentence, with the profile as it was.
"""
from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

from personal_ai_core.app.cli import main

ORIGINAL = "# About me\n- I live in Ajman\n"


def _profile(tmp_path, text: str | bytes = ORIGINAL) -> Path:
    path = tmp_path / "data" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()

    def transport(url, payload, timeout):
        return {"model": payload["model"], "message": {"content": "ok"}}

    code = main(["--database", str(tmp_path / "data" / "core.db"), *argv],
                transport=transport, stdin=iter(["hello"]), stdout=out, env={})
    return code, out.getvalue()


def test_a_remember_that_cannot_finish_leaves_the_profile_as_it_was(tmp_path, monkeypatch):
    path = _profile(tmp_path)

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", disk_full)
    code, output = _run(tmp_path, "--remember", "my CV is up to date")

    assert code == 2 and "Traceback" not in output
    assert f"nothing added to {path}: No space left on device" in output
    assert "The profile is as it was." in output
    assert path.read_text(encoding="utf-8") == ORIGINAL
    # No half-written temporary file is left beside it.
    assert sorted(p.name for p in path.parent.iterdir()) == ["profile.md"]


def test_a_remember_still_adds_the_line(tmp_path):
    path = _profile(tmp_path)
    code, output = _run(tmp_path, "--remember", "my CV is up to date")
    assert code == 0 and "remembered" in output
    assert path.read_text(encoding="utf-8") == ORIGINAL + "- my CV is up to date\n"


def test_a_remember_on_a_profile_that_is_not_utf8_changes_nothing(tmp_path):
    raw = "# About me\n- café\n".encode("latin-1")
    path = _profile(tmp_path, raw)
    code, output = _run(tmp_path, "--remember", "anything")
    assert code == 2 and "Traceback" not in output
    assert f"the profile is not UTF-8 text: {path}" in output
    assert path.read_bytes() == raw
    assert sorted(p.name for p in path.parent.iterdir()) == ["profile.md"]


def test_a_remember_on_a_profile_with_a_stray_byte_changes_nothing(tmp_path):
    raw = b"# me\n\xff\xfe not text\n"
    path = _profile(tmp_path, raw)
    code, output = _run(tmp_path, "--remember", "anything")
    assert code == 2 and "Traceback" not in output
    assert f"the profile is not UTF-8 text: {path}" in output
    assert path.read_bytes() == raw


def test_a_write_torn_mid_way_leaves_the_profile_byte_for_byte(tmp_path, monkeypatch):
    """A write that gets a prefix out and then fails (a full disk, a crash)."""
    raw = "\ufeff# About me\r\n- I live in Ajman\r\n".encode("utf-8")
    path = _profile(tmp_path, raw)
    real_open = io.open

    class Torn:
        """Gets half of what it is given to disk, then the disk is full."""

        def __init__(self, handle):
            self._handle = handle

        def write(self, s):
            self._handle.write(s[: len(s) // 2])
            self._handle.flush()
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def open_torn(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return Torn(handle) if "w" in mode else handle

    monkeypatch.setattr(io, "open", open_torn)
    try:
        code, output = _run(tmp_path, "--remember", "my CV is up to date")
    finally:
        monkeypatch.undo()

    assert code == 2 and "Traceback" not in output
    assert f"nothing added to {path}: No space left on device" in output
    assert "The profile is as it was." in output
    assert path.read_bytes() == raw
    assert sorted(p.name for p in path.parent.iterdir()) == ["profile.md"]


def test_a_directory_named_as_the_profile_is_a_sentence(tmp_path):
    path = tmp_path / "data" / "profile.md"
    path.mkdir(parents=True)
    code, output = _run(tmp_path, "--profile", str(path), "--remember", "anything")
    assert code == 2 and "Traceback" not in output
    assert f"the profile is not a file: {path}" in output
    assert path.is_dir() and list(path.iterdir()) == []


def test_a_remember_on_a_profile_that_cannot_be_read_is_a_sentence(tmp_path, monkeypatch):
    path = _profile(tmp_path)
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    code, output = _run(tmp_path, "--remember", "anything")
    monkeypatch.undo()

    assert code == 2 and "Traceback" not in output
    assert f"the profile cannot be read: {path} (Permission denied)" in output
    assert path.read_text(encoding="utf-8") == ORIGINAL


def test_a_read_only_profile_is_not_replaced(tmp_path, monkeypatch):
    path = _profile(tmp_path)
    real_access = os.access

    def read_only(p, mode, *args, **kwargs):
        if Path(p) == path and mode & os.W_OK:
            return False
        return real_access(p, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", read_only)
    code, output = _run(tmp_path, "--remember", "anything")
    monkeypatch.undo()

    assert code == 2 and "Traceback" not in output
    assert f"nothing added to {path}: Permission denied. The profile is as it was." in output
    assert path.read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_remember_keeps_the_profile_mode(tmp_path):
    path = _profile(tmp_path)
    path.chmod(0o640)
    code, _ = _run(tmp_path, "--remember", "my CV is up to date")
    assert code == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text(encoding="utf-8") == ORIGINAL + "- my CV is up to date\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_new_profile_gets_the_usual_mode_not_0600(tmp_path):
    path = tmp_path / "data" / "profile.md"
    umask = os.umask(0o022)
    try:
        code, _ = _run(tmp_path, "--profile", str(path), "--remember", "first line")
    finally:
        os.umask(umask)
    assert code == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_a_profile_that_cannot_be_read_is_named_not_a_traceback(tmp_path, monkeypatch):
    path = _profile(tmp_path)
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    code, output = _run(tmp_path)

    assert code == 2 and "Traceback" not in output
    assert f"the profile cannot be read: {path} (Permission denied)" in output
    assert "session:" not in output
