"""An Arabic reply prints whatever code page the process's streams start in.

On Windows a redirected or piped stdout uses the ANSI code page (cp1252), and
an Arabic reply raised UnicodeEncodeError: a traceback, the reply stored but
never shown. `main` now reads and writes its own streams as UTF-8.
"""
from __future__ import annotations

import io
import sys

from personal_ai_core.app.cli import main

ARABIC_REPLY = "مرحبا، كيف حالك؟"


def reply(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": ARABIC_REPLY}}


def ansi(stream_bytes: io.BytesIO) -> io.TextIOWrapper:
    return io.TextIOWrapper(stream_bytes, encoding="cp1252", errors="strict", newline="")


def test_an_arabic_reply_prints_through_a_cp1252_stdout(tmp_path, monkeypatch):
    raw_out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", ansi(raw_out))
    monkeypatch.setattr(sys, "stderr", ansi(io.BytesIO()))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(
        io.BytesIO("مرحبا\n".encode("utf-8")), encoding="cp1252", errors="strict"))
    code = main(["--database", str(tmp_path / "core.db")], transport=reply, env={})
    sys.stdout.flush()
    assert code == 0
    assert ARABIC_REPLY in raw_out.getvalue().decode("utf-8")


def test_a_stream_the_caller_passes_is_left_as_it_is(tmp_path, monkeypatch):
    process_out = ansi(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", process_out)
    given = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db")], transport=reply,
                stdin=iter(["hello"]), stdout=given, env={})
    assert code == 0 and ARABIC_REPLY in given.getvalue()
    assert process_out.encoding == "cp1252"  # only the caller's stream was used
