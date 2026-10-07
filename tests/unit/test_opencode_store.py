"""The integration store maps sessions without storing PAC/OpenCode text."""
from __future__ import annotations

import json
import sqlite3
from uuid import UUID

import pytest

from personal_ai_core.integrations.opencode.store import (
    IntegrationStoreError,
    OpenCodeSessionStore,
)

FINGERPRINT = "ab" * 32


def _rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM opencode_sessions")]


def test_stable_external_session_survives_reopening(tmp_path):
    path = tmp_path / "opencode.db"
    store = OpenCodeSessionStore(path)
    session = store.touch("ses_opencode", "qwen:7b", FINGERPRINT,
                          metadata={"status": "created", "message_count": 1})
    assert str(UUID(session)) == session
    reopened = OpenCodeSessionStore(path)
    assert reopened.touch("ses_opencode", "qwen:7b", FINGERPRINT,
                          metadata={"message_count": 3}) == session
    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "ses_opencode"
    assert rows[0]["created_at"] <= rows[0]["updated_at"]
    assert json.loads(rows[0]["metadata_json"]) == {"status": "created", "message_count": 3}


def test_missing_mapping_gets_distinct_sessions_without_content_guessing(tmp_path):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    first = store.touch(None, "qwen:7b", FINGERPRINT)
    second = store.touch(None, "qwen:7b", FINGERPRINT)
    assert first != second
    assert len(_rows(store.path)) == 2


def test_returned_pac_session_id_can_resume_without_creating_second_mapping(tmp_path):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    session = store.touch(None, "qwen:7b", FINGERPRINT)
    assert store.touch(session, "qwen:7b", FINGERPRINT) == session
    assert len(_rows(store.path)) == 1
    assert _rows(store.path)[0]["external_id"] is None


def test_metadata_can_only_store_counts_and_fixed_statuses(tmp_path):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    secret = "sk-test-secret-never-persist"
    session = store.touch("ses_safe", "qwen:7b", FINGERPRINT, metadata={
        "status": "completed", "prompt_tokens": 23, "tool_calls": 1,
        "profile": secret, "content": secret, "messages": [{"content": secret}],
        "tool_results": secret, "completion_tokens": -1, "request_count": True,
    })
    store.record(session, {"completion_tokens": 7, "status": secret,
                           "tool_result": secret, "request_kind": "primary"})
    row = _rows(store.path)[0]
    assert json.loads(row["metadata_json"]) == {
        "status": "completed", "prompt_tokens": 23, "tool_calls": 1,
        "completion_tokens": 7, "request_kind": "primary",
    }
    assert secret.encode() not in store.path.read_bytes()


def test_refuses_core_database_even_before_it_exists(tmp_path):
    target = tmp_path / "core.db"
    with pytest.raises(IntegrationStoreError, match="separate database"):
        OpenCodeSessionStore(target)
    assert not target.exists()


def test_refuses_renamed_core_database_and_preserves_schema_and_rows(tmp_path):
    target = tmp_path / "renamed-owner.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE messages (content TEXT)")
        connection.execute("INSERT INTO messages VALUES ('owner content')")
        connection.execute("PRAGMA user_version = 1")
    before = target.read_bytes()
    with pytest.raises(IntegrationStoreError, match="not a supported"):
        OpenCodeSessionStore(target)
    assert target.read_bytes() == before
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT content FROM messages").fetchall() == [("owner content",)]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_refuses_future_integration_schema_without_migration(tmp_path):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 2")
    before = store.path.read_bytes()
    with pytest.raises(IntegrationStoreError, match="not a supported"):
        OpenCodeSessionStore(store.path)
    assert store.path.read_bytes() == before


def test_rejects_profile_text_and_unknown_record_id(tmp_path):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    with pytest.raises(IntegrationStoreError, match="fingerprint"):
        store.touch("session", "qwen:7b", "private profile contents")
    with pytest.raises(IntegrationStoreError, match="Unknown"):
        store.record("no-session", {"status": "failed"})
    assert _rows(store.path) == []


@pytest.mark.parametrize("kind", ["title", "generate"])
def test_actual_opencode_request_kinds_only_store_fixed_kind_metadata(tmp_path, kind):
    store = OpenCodeSessionStore(tmp_path / "opencode.db")
    store.touch("session", "qwen:7b", FINGERPRINT, request_kind=kind,
                metadata={"request_kind": kind, "content": "do not persist this title"})
    row = _rows(store.path)[0]
    assert row["request_kind"] == kind
    assert json.loads(row["metadata_json"]) == {"request_kind": kind}
