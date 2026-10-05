"""`pac --sessions`: the way back to a conversation whose id was lost.

pac prints a session id once, when a conversation starts, and `--session ID` is
the only way back into it. Lose the id and the conversation was still in the
file but out of reach. `--sessions` lists what is stored, newest first, and
exits: read-only (the file is opened `mode=ro`), no model called.
"""
from __future__ import annotations

import io
import re
import sqlite3

from personal_ai_core.app.cli import main
from personal_ai_core.persistence.sqlite import connect


def refuse_model(url, payload, timeout):
    raise AssertionError("--sessions must not call the model")


def reply(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": "ok"}}


def pac(tmp_path, *argv, transport=reply, lines=()):
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db"), *argv], transport=transport,
                stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue()


def start(tmp_path, *lines) -> str:
    code, output = pac(tmp_path, lines=lines)
    assert code == 0, output
    return re.search(r"session: (\S+)", output).group(1)  # type: ignore[union-attr]


def test_every_stored_conversation_is_listed_newest_first_with_its_size(tmp_path):
    older = start(tmp_path, "one", "two")
    newer = start(tmp_path, "three")
    code, output = pac(tmp_path, "--sessions", transport=refuse_model)
    assert code == 0
    assert "2 conversation(s)" in output
    assert output.index(newer) < output.index(older)
    older_line = next(line for line in output.splitlines() if older in line)
    newer_line = next(line for line in output.splitlines() if newer in line)
    assert "4 message(s)" in older_line and "2 message(s)" in newer_line
    assert "continue one with --session ID" in output


def test_a_listed_id_continues_its_conversation(tmp_path):
    session = start(tmp_path, "remember the number 7")
    _, listing = pac(tmp_path, "--sessions", transport=refuse_model)
    listed = re.search(r"^  (\S+)  started", listing, re.MULTILINE).group(1)  # type: ignore[union-attr]
    assert listed == session
    code, output = pac(tmp_path, "--session", listed, lines=["and again"])
    assert code == 0 and "core> ok" in output


def test_listing_writes_nothing(tmp_path):
    start(tmp_path, "hello")
    database = tmp_path / "core.db"
    before = database.read_bytes()
    pac(tmp_path, "--sessions", transport=refuse_model)
    assert database.read_bytes() == before


def test_listing_works_while_another_writer_holds_the_database(tmp_path):
    start(tmp_path, "hello")
    writer = sqlite3.connect(tmp_path / "core.db")
    try:
        writer.execute("BEGIN IMMEDIATE")
        code, output = pac(tmp_path, "--sessions", transport=refuse_model)
    finally:
        writer.rollback()
        writer.close()
    assert code == 0 and "1 conversation(s)" in output


def test_a_missing_database_is_said_and_not_created(tmp_path):
    code, output = pac(tmp_path, "--sessions", transport=refuse_model)
    assert code == 2 and "no stored conversations at" in output
    assert not (tmp_path / "core.db").exists()


def test_an_empty_database_says_so(tmp_path):
    connect(tmp_path / "core.db").close()
    code, output = pac(tmp_path, "--sessions", transport=refuse_model)
    assert code == 0 and "no conversations stored" in output


def test_a_closed_session_is_marked(tmp_path):
    from personal_ai_core.conversation.factory import build_persistent_service
    from personal_ai_core.core.config import Settings

    slice_ = build_persistent_service(Settings.from_env({}), database=tmp_path / "core.db",
                                      transport=reply)
    try:
        service = slice_.service
        session = service.start_session(service.create_user().id)
        service.close_session(session.id)
    finally:
        slice_.close()
    _, output = pac(tmp_path, "--sessions", transport=refuse_model)
    line = next(line for line in output.splitlines() if session.id in line)
    assert "(closed)" in line and "0 message(s)" in line


def test_a_database_from_another_schema_version_is_refused_by_name(tmp_path):
    start(tmp_path, "hello")
    with sqlite3.connect(tmp_path / "core.db") as connection:
        connection.execute("UPDATE schema_version SET version = 99")
    code, output = pac(tmp_path, "--sessions", transport=refuse_model)
    assert code == 1 and "different version of pac" in output


def test_it_does_not_combine_with_ephemeral_or_other_modes(tmp_path):
    start(tmp_path, "hello")
    code, output = pac(tmp_path, "--sessions", "--ephemeral", transport=refuse_model)
    assert code == 2 and "--ephemeral" in output
    code, output = pac(tmp_path, "--sessions", "--observations", transport=refuse_model)
    assert code == 2 and "--observations" in output
