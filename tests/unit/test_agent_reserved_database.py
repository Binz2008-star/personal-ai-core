"""F-1: the Core's own database is not reachable from the workspace.

The default database lives under `~/.personal-ai-core/`, so `--workspace ~`
contains it, and before this fix `write_file core.db` returned ok. The
refusal is in the sandbox, not in the entry point: whatever directory is
chosen, a reserved file and its SQLite companions are refused for every tool.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import SQLITE_COMPANIONS, SandboxError, Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.core.agent import ToolRequest

ORIGINAL = b"SQLite format 3\x00 the Core's records"


def link(path, target):
    """Windows grants symlink creation only with a privilege CI may lack."""
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")


@pytest.fixture
def home(tmp_path):
    """A workspace that contains the database, as `--workspace ~` does."""
    root = tmp_path / "home"
    (root / ".personal-ai-core").mkdir(parents=True)
    database = root / ".personal-ai-core" / "core.db"
    database.write_bytes(ORIGINAL)
    (root / "notes.md").write_text("mine\n", encoding="utf-8")
    return root, database


def agent(root, database, *, confirm=lambda request, spec: True):
    workspace = Workspace(root, reserved=(database,))
    checkpoints = Checkpoints(workspace)
    return ToolExecutor(default_tools(workspace, checkpoints), RiskPolicy(), confirm=confirm)


def run(executor, tool, **arguments):
    record = executor.execute(ToolRequest(tool, arguments))
    assert record.result is not None
    return record.result


DB = ".personal-ai-core/core.db"


# --- write, delete, read ------------------------------------------------------------


def test_write_file_cannot_overwrite_the_database(home):
    root, database = home
    result = run(agent(root, database), "write_file", path=DB, content="x")
    assert not result.ok and "database is not reachable" in (result.error or "")
    assert database.read_bytes() == ORIGINAL


@pytest.mark.parametrize("companion", [c for c in SQLITE_COMPANIONS if c])
def test_write_file_cannot_create_a_companion_file(home, companion):
    """A planted `-journal` or `-wal` is replayed into the database by SQLite
    on the next open; creating one is writing to the database."""
    root, database = home
    result = run(agent(root, database), "write_file", path=DB + companion, content="x")
    assert not result.ok
    assert not (root / (DB + companion)).exists()


def test_delete_file_cannot_delete_the_database_even_when_the_user_confirms(home):
    root, database = home
    result = run(agent(root, database, confirm=lambda r, s: True), "delete_file", path=DB)
    assert not result.ok and "database is not reachable" in (result.error or "")
    assert database.read_bytes() == ORIGINAL


def test_delete_file_cannot_delete_an_existing_companion(home):
    root, database = home
    wal = root / (DB + "-wal")
    wal.write_bytes(b"wal")
    result = run(agent(root, database), "delete_file", path=DB + "-wal")
    assert not result.ok and wal.read_bytes() == b"wal"


def test_read_file_cannot_read_the_database(home):
    root, database = home
    database.write_text("the conversation history", encoding="utf-8")
    result = run(agent(root, database), "read_file", path=DB)
    assert not result.ok and "history" not in result.output


def test_search_text_does_not_look_inside_the_database(home):
    root, database = home
    database.write_text("needle in the history\n", encoding="utf-8")
    (root / "notes.md").write_text("needle in my notes\n", encoding="utf-8")
    result = run(agent(root, database), "search_text", text="needle")
    assert result.ok and "notes.md" in result.output
    assert "core.db" not in result.output and "history" not in result.output


def test_list_directory_does_not_show_the_database(home):
    root, database = home
    (root / (DB + "-wal")).write_bytes(b"wal")
    result = run(agent(root, database), "list_directory", path=".personal-ai-core")
    assert result.ok and result.output == ""


def test_other_files_beside_the_database_stay_usable(home):
    """The refusal is the database, not its directory."""
    root, database = home
    result = run(agent(root, database), "write_file", path=".personal-ai-core/notes.md", content="ok")
    assert result.ok


# --- other ways to name the same file ----------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "./.personal-ai-core/core.db",
        ".personal-ai-core//core.db",
        ".personal-ai-core\\core.db",
        ".personal-ai-core/./core.db",
    ],
)
def test_other_spellings_are_refused(home, path):
    root, database = home
    with pytest.raises(SandboxError, match="database"):
        Workspace(root, reserved=(database,)).resolve_for_write(path)


def test_traversal_from_a_workspace_below_the_database_is_refused(home):
    """The database is outside this workspace, so `..` and escape rules own it."""
    root, database = home
    below = root / "projects"
    below.mkdir()
    workspace = Workspace(below, reserved=(database,))
    for path in ("../.personal-ai-core/core.db", "x/../../.personal-ai-core/core.db"):
        with pytest.raises(SandboxError, match="traversal"):
            workspace.resolve_for_write(path)
    assert database.read_bytes() == ORIGINAL


def test_a_symlink_inside_the_workspace_to_the_database_is_refused(home):
    root, database = home
    link(root / "innocent.txt", database)
    result = run(agent(root, database), "write_file", path="innocent.txt", content="x")
    assert not result.ok and database.read_bytes() == ORIGINAL


def test_a_symlinked_directory_leading_to_the_database_is_refused(home):
    root, database = home
    link(root / "data", database.parent)
    result = run(agent(root, database), "write_file", path="data/core.db", content="x")
    assert not result.ok and database.read_bytes() == ORIGINAL


def test_a_hard_link_to_the_database_is_the_database(home):
    root, database = home
    try:
        os.link(database, root / "copy.db")
    except OSError as exc:
        pytest.skip(f"cannot create a hard link here: {exc}")
    result = run(agent(root, database), "write_file", path="copy.db", content="x")
    assert not result.ok and database.read_bytes() == ORIGINAL


@pytest.mark.parametrize("spelling", ["CORE.DB", "Core.Db", "core.db.", "core.db "])
def test_no_spelling_of_the_name_changes_the_database(home, spelling):
    """Case-insensitive filesystems, and Windows' trailing dot and space,
    map these to the database itself. Where they do, they are refused; where
    they name a different file, writing it leaves the database untouched."""
    root, database = home
    run(agent(root, database), "write_file", path=f".personal-ai-core/{spelling}", content="x")
    assert database.read_bytes() == ORIGINAL


# --- a real database, through the whole tool set ------------------------------------


def test_a_live_sqlite_database_survives_every_tool(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    database = root / "core.db"
    connection = sqlite3.connect(database)
    connection.execute("create table events (id text)")
    connection.execute("insert into events values ('e1')")
    connection.commit()
    connection.close()
    before = database.read_bytes()

    executor = agent(root, database)
    for tool, arguments in [
        ("write_file", {"path": "core.db", "content": "x"}),
        ("write_file", {"path": "core.db-journal", "content": "x"}),
        ("delete_file", {"path": "core.db"}),
        ("read_file", {"path": "core.db"}),
    ]:
        assert not run(executor, tool, **arguments).ok

    assert database.read_bytes() == before
    rows = sqlite3.connect(database).execute("select id from events").fetchall()
    assert rows == [("e1",)]


def test_without_a_reserved_database_nothing_is_reserved(tmp_path):
    """--ephemeral has no database file; the workspace reserves nothing."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "core.db").write_bytes(b"not ours")
    assert not Workspace(root).is_reserved(root / "core.db")
