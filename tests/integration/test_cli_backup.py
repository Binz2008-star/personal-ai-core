"""`pac --backup PATH`: a consistent, checked copy of the database.

The README described a manual backup -- copy `core.db` with its `-wal` and
`-shm`, with no pac running. A copy of the main file alone loses whatever is
still in the WAL, and a copy taken while pac writes can be torn. `--backup`
uses SQLite's online backup from a read-only connection: one consistent
snapshot, WAL included, safe while another pac runs. It never overwrites, it
checks the copy before saying so, and it calls no model.
"""
from __future__ import annotations

import io
import re
import sqlite3

from personal_ai_core.app.cli import main


def refuse_model(url, payload, timeout):
    raise AssertionError("--backup must not call the model")


def reply(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": "ok"}}


def pac(database, *argv, transport=reply, lines=()):
    out = io.StringIO()
    code = main(["--database", str(database), *argv], transport=transport,
                stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue()


def counts(database):
    with sqlite3.connect(database) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("users", "sessions", "messages", "events")}


def test_the_copy_holds_everything_and_says_where_it_is(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["one", "two"])
    copy = tmp_path / "copy.db"
    code, output = pac(database, "--backup", str(copy), transport=refuse_model)
    assert code == 0
    assert f"backed up {database} to {copy}" in output and "checked" in output
    assert counts(copy) == counts(database)


def test_the_copy_is_a_working_database_a_session_continues_in(tmp_path):
    database = tmp_path / "core.db"
    _, started = pac(database, lines=["remember 7"])
    session = re.search(r"session: (\S+)", started).group(1)  # type: ignore[union-attr]
    copy = tmp_path / "copy.db"
    pac(database, "--backup", str(copy), transport=refuse_model)
    code, output = pac(copy, "--session", session, lines=["and again"])
    assert code == 0 and "core> ok" in output


def test_writes_still_in_the_wal_are_in_the_copy(tmp_path):
    """The case a copy of `core.db` alone gets wrong: a write another pac has
    made and not yet checkpointed lives in `core.db-wal`."""
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO users (id, created_at) VALUES ('wal-only', 'x')")
        writer.commit()
        assert (tmp_path / "core.db-wal").stat().st_size > 0
        copy = tmp_path / "copy.db"
        code, _ = pac(database, "--backup", str(copy), transport=refuse_model)
    finally:
        writer.close()
    assert code == 0
    with sqlite3.connect(copy) as connection:
        assert connection.execute("SELECT id FROM users WHERE id = 'wal-only'").fetchone()


def test_it_works_while_another_writer_holds_the_database(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    writer = sqlite3.connect(database)
    try:
        writer.execute("BEGIN IMMEDIATE")
        code, _ = pac(database, "--backup", str(tmp_path / "copy.db"), transport=refuse_model)
    finally:
        writer.rollback()
        writer.close()
    assert code == 0


def test_an_existing_file_is_never_overwritten(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    existing = tmp_path / "keep.db"
    existing.write_bytes(b"something of mine")
    code, output = pac(database, "--backup", str(existing), transport=refuse_model)
    assert code == 2 and "never overwrites" in output
    assert existing.read_bytes() == b"something of mine"


def test_the_database_itself_is_refused_as_the_destination(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    before = database.read_bytes()
    code, output = pac(database, "--backup", str(database), transport=refuse_model)
    assert code == 2 and "another file" in output
    assert database.read_bytes() == before


def test_the_source_is_not_written(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    before = database.read_bytes()
    pac(database, "--backup", str(tmp_path / "copy.db"), transport=refuse_model)
    assert database.read_bytes() == before


def test_a_missing_database_or_directory_is_said(tmp_path):
    code, output = pac(tmp_path / "none.db", "--backup", str(tmp_path / "copy.db"),
                       transport=refuse_model)
    assert code == 2 and "no stored conversations at" in output
    assert not (tmp_path / "none.db").exists() and not (tmp_path / "copy.db").exists()
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    code, output = pac(database, "--backup", str(tmp_path / "nowhere" / "copy.db"),
                       transport=refuse_model)
    assert code == 2 and "no such directory" in output


def test_a_database_from_another_schema_version_is_not_copied(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_version SET version = 99")
    code, output = pac(database, "--backup", str(tmp_path / "copy.db"), transport=refuse_model)
    assert code == 1 and "different version of pac" in output
    assert not (tmp_path / "copy.db").exists()


def test_it_does_not_combine_with_ephemeral_or_other_modes(tmp_path):
    database = tmp_path / "core.db"
    pac(database, lines=["hello"])
    code, output = pac(database, "--backup", str(tmp_path / "c.db"), "--ephemeral",
                       transport=refuse_model)
    assert code == 2 and "--ephemeral" in output
    code, output = pac(database, "--backup", str(tmp_path / "c.db"), "--session", "x",
                       transport=refuse_model)
    assert code == 2 and "--session" in output
