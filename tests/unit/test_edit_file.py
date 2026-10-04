"""Slice 2: the surgical edit_file tool, run directly.

MEDIUM like write_file: allowed in the workspace, audited. The sandbox and
the protected/reserved refusals are the same calls write_file makes, so a
path write_file refuses is refused here too.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import SandboxError, Workspace
from personal_ai_core.agent.tools import MAX_WRITE_CHARS, EditFile, default_tools
from personal_ai_core.core.agent import RiskLevel


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "app.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    (root / "repeat.txt").write_text("hi hi hi\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    return Workspace(root)


def test_edit_file_is_a_default_medium_tool(ws):
    names = {t.spec.name: t.spec.risk_level for t in default_tools(ws)}
    assert names["edit_file"] is RiskLevel.MEDIUM


def test_edit_replaces_one_unique_match_and_returns_a_diff(ws):
    result = EditFile(ws).run(
        {"path": "app.py", "old_string": "y = 1", "new_string": "y = 2"}
    )
    assert result.ok
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    assert "edited app.py" in result.output
    assert "-y = 1" in result.output and "+y = 2" in result.output


def test_edit_rejects_zero_matches_and_changes_nothing(ws):
    before = (ws.root / "app.py").read_text(encoding="utf-8")
    result = EditFile(ws).run(
        {"path": "app.py", "old_string": "z = 9", "new_string": "z = 10"}
    )
    assert not result.ok and "zero times" in (result.error or "")
    assert (ws.root / "app.py").read_text(encoding="utf-8") == before


def test_edit_rejects_ambiguous_matches_when_uniqueness_is_required(ws):
    before = (ws.root / "repeat.txt").read_text(encoding="utf-8")
    result = EditFile(ws).run(
        {"path": "repeat.txt", "old_string": "hi", "new_string": "yo"}
    )
    assert not result.ok and "3 times" in (result.error or "")
    assert (ws.root / "repeat.txt").read_text(encoding="utf-8") == before


def test_edit_replaces_all_matches_when_uniqueness_is_off(ws):
    result = EditFile(ws).run(
        {"path": "repeat.txt", "old_string": "hi", "new_string": "yo",
         "require_unique": False}
    )
    assert result.ok
    assert (ws.root / "repeat.txt").read_text(encoding="utf-8") == "yo yo yo\n"


def test_edit_refuses_a_protected_file(ws):
    with pytest.raises(SandboxError, match="may not write"):
        EditFile(ws).run(
            {"path": ".env", "old_string": "secret", "new_string": "public"}
        )


def test_edit_refuses_a_reserved_database(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "core.db"
    target.write_bytes(b"db")
    sandbox = Workspace(root, reserved=(target,))
    (root / "other.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(SandboxError, match="database"):
        EditFile(sandbox).run(
            {"path": "core.db", "old_string": "db", "new_string": "x"}
        )


def test_edit_records_a_rollback_point(ws):
    checkpoints = Checkpoints(ws)
    EditFile(ws, checkpoints).run(
        {"path": "app.py", "old_string": "y = 1", "new_string": "y = 2"}
    )
    assert checkpoints.touched() == ("app.py",)
    checkpoints.rollback()
    assert (ws.root / "app.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"


def test_edit_preserves_the_write_limit(ws):
    big = "x" * (MAX_WRITE_CHARS + 1)
    result = EditFile(ws).run(
        {"path": "app.py", "old_string": "x = 1", "new_string": big}
    )
    assert not result.ok and "over" in (result.error or "")
