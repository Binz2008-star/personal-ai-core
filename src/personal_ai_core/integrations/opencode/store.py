"""Small, separate session mapping for the OpenCode provider integration.

OpenCode owns its transcript. This database holds identifiers, timestamps,
profile fingerprints and numerical/status metadata only; it cannot accept
message, profile or tool-result text through its metadata interface.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_APPLICATION_ID = 0x5041434F  # PACO: distinct from PAC's core persistence.
_SCHEMA_VERSION = 1
_COUNT_FIELDS = frozenset({
    "request_count", "message_count", "tool_count", "tool_call_count",
    "tool_result_count", "prompt_tokens", "completion_tokens", "total_tokens",
    "identity_tokens", "transcript_tokens", "tool_schema_tokens", "input_tokens",
    "output_tokens", "context_window", "output_limit", "omitted_messages",
    "redactions", "redacted_values", "status_code", "estimated_prompt_tokens",
    "generation_reserve", "overhead", "guard_reserve", "spoken_for",
    "tool_calls", "tool_results",
})
_STATUS_VALUES = frozenset({
    "created", "completed", "failed", "refused", "tool_calls", "stop",
    "provider_failure", "context_overflow",
})
SUPPORTED_REQUEST_KINDS = frozenset({"primary", "summary", "compaction", "auxiliary", "title", "generate"})


class IntegrationStoreError(ValueError):
    """The integration database is invalid or points at another database."""


def _metadata(values: Mapping[str, object] | None) -> dict[str, object]:
    """Discard arbitrary text, nested data and unknown fields before storage."""
    result: dict[str, object] = {}
    if values is None:
        return result
    for key, value in values.items():
        if key in _COUNT_FIELDS and isinstance(value, int) and not isinstance(value, bool):
            if 0 <= value <= 2**63 - 1:
                result[key] = value
        elif isinstance(value, str) and (
            (key == "status" and value in _STATUS_VALUES)
            or (key == "request_kind" and value in SUPPORTED_REQUEST_KINDS)
        ):
            result[key] = value
    return result


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise IntegrationStoreError(f"{name} must contain between 1 and 256 characters")
    if any(ord(character) < 32 for character in value):
        raise IntegrationStoreError(f"{name} cannot contain control characters")
    return value


class OpenCodeSessionStore:
    """Map a stable client conversation ID to an integration-specific UUID."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.name.casefold() == "core.db":
            raise IntegrationStoreError("OpenCode requires a separate database; core.db is protected")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            if tables or application_id or version:
                if (application_id != _APPLICATION_ID or version != _SCHEMA_VERSION
                        or tables != {"opencode_sessions"}):
                    raise IntegrationStoreError("Database is not a supported OpenCode integration store")
                return
            connection.execute("""
                CREATE TABLE opencode_sessions (
                    id TEXT PRIMARY KEY,
                    external_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    profile_fingerprint TEXT NOT NULL,
                    request_kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
            """)
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def touch(self, external_id: str | None, model: str,
              profile_fingerprint: str, request_kind: str = "primary",
              metadata: Mapping[str, object] | None = None) -> str:
        if external_id is not None:
            _identifier(external_id, "OpenCode conversation ID")
        _identifier(model, "model ID")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", profile_fingerprint):
            raise IntegrationStoreError("Profile fingerprint must be a SHA-256 hexadecimal digest")
        if request_kind not in SUPPORTED_REQUEST_KINDS:
            raise IntegrationStoreError("Unsupported request kind")
        now = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = None if external_id is None else connection.execute(
                "SELECT id, metadata_json FROM opencode_sessions WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            # A client without its own stable ID can echo X-PAC-Session-ID.
            # The generated UUID already identifies the integration session.
            if row is None and external_id is not None:
                row = connection.execute(
                    "SELECT id, metadata_json FROM opencode_sessions WHERE id = ?",
                    (external_id,),
                ).fetchone()
            if row is None:
                session_id = str(uuid4())
                connection.execute(
                    "INSERT INTO opencode_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, external_id, now, now, model, profile_fingerprint.lower(),
                     request_kind, json.dumps(_metadata(metadata), sort_keys=True)),
                )
            else:
                session_id = str(row[0])
                stored = _metadata(json.loads(row[1]))
                stored.update(_metadata(metadata))
                connection.execute(
                    "UPDATE opencode_sessions SET updated_at = ?, model = ?, "
                    "profile_fingerprint = ?, request_kind = ?, metadata_json = ? WHERE id = ?",
                    (now, model, profile_fingerprint.lower(), request_kind,
                     json.dumps(stored, sort_keys=True), session_id),
                )
        return session_id

    def record(self, session_id: str, metadata: Mapping[str, object]) -> None:
        """Merge safe counts/status into an existing session; never store text."""
        _identifier(session_id, "PAC integration session ID")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT metadata_json FROM opencode_sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                raise IntegrationStoreError("Unknown PAC integration session ID")
            stored = _metadata(json.loads(row[0]))
            stored.update(_metadata(metadata))
            connection.execute(
                "UPDATE opencode_sessions SET updated_at = ?, metadata_json = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(),
                 json.dumps(stored, sort_keys=True), session_id),
            )
