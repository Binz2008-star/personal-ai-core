"""The owner's profile is never left torn, and a bad one is a sentence.

Gap analysis P2-7. `--remember` rewrote profile.md in place, so a full disk or
a crash mid-write could leave it truncated; an unreadable profile escaped as
a traceback. Now the write is atomic (a temporary file, fsynced, renamed over
the old one) and both failures end in a sentence, with the profile as it was.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

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

    assert code == 1 and "Traceback" not in output
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
    assert code == 1 and "Traceback" not in output
    assert "nothing added: the profile is not UTF-8 text" in output
    assert path.read_bytes() == raw


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
