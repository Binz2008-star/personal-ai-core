"""A failing store ends in a sentence and the safe next step, not a traceback.

Gap analysis P1-5. A second `pac` writing the same file, a damaged file and a
file from another schema version each ended in a traceback. Each is now one
sentence from `pac:` and exit 1. A damaged file is never repaired or replaced:
the first step named is a copy, with the -wal and -shm files that hold its
latest writes.
"""
from __future__ import annotations

import io
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from personal_ai_core.app.cli import main
from personal_ai_core.conversation.factory import describe_store_failure


def _transport(url: str, payload: Mapping[str, Any], timeout: int):
    return {"model": payload["model"], "message": {"content": "ok"}}


def _run(database: Path, *lines: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--database", str(database)], transport=_transport,
                stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue()


def test_a_damaged_file_is_named_and_left_alone(tmp_path):
    database = tmp_path / "core.db"
    garbage = b"not a database at all " * 200
    database.write_bytes(garbage)

    code, output = _run(database, "hello")

    assert code == 1 and "Traceback" not in output
    assert output.startswith(f"pac: the database {database} cannot be read")
    assert "core.db-wal" in output and "core.db-shm" in output
    assert "--database" in output
    assert database.read_bytes() == garbage


def test_a_database_from_another_schema_version_is_named(tmp_path):
    database = tmp_path / "core.db"
    assert _run(database, "hello")[0] == 0
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_version SET version = 999")

    code, output = _run(database, "hello")

    assert code == 1 and "Traceback" not in output
    assert f"pac: the database {database} was written by a different version of pac" in output
    assert "999" in output


def test_a_database_another_writer_holds_is_said_to_be_busy(tmp_path):
    database = tmp_path / "core.db"
    assert _run(database, "hello")[0] == 0
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        result: dict[str, Any] = {}
        # sqlite waits its busy timeout (5 s) for the writer, then gives up.
        worker = threading.Thread(target=lambda: result.update(zip(
            ("code", "output"), _run(database, "hello"))))
        worker.start()
        worker.join(timeout=60)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result["code"] == 1 and "Traceback" not in result["output"]
    assert f"the database {database} is busy" in result["output"]
    assert "another pac" in result["output"]


def test_each_kind_of_failure_has_its_own_sentence():
    path = Path("core.db")
    said = {
        "busy": describe_store_failure(sqlite3.OperationalError("database is locked"), path),
        "keys": describe_store_failure(sqlite3.IntegrityError("UNIQUE constraint failed"), path),
        "damaged": describe_store_failure(sqlite3.DatabaseError("file is not a database"), path),
        "other": describe_store_failure(sqlite3.OperationalError("disk I/O error"), path),
    }
    assert "is busy" in said["busy"]
    assert "cannot be opened: UNIQUE constraint failed" in said["keys"]
    assert "may be damaged" in said["damaged"] and "core.db-wal" in said["damaged"]
    assert said["other"] == "the database core.db failed: disk I/O error"
    assert len(set(said.values())) == 4
