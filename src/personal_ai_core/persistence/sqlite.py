"""SQLite-backed repositories — ADR-010 option D with B as the durable store.

One file, no server, ACID, and it ships with Python. `sqlite3` is in the
standard library, so this adds no dependency: `pyproject.toml`'s
`dependencies = []` is unchanged, which was a condition of every option in
ADR-010, not a coincidence of this one.

WHAT IS HERE AND WHAT IS NOT
----------------------------
Events, messages, sessions, users and memory records. Knowledge chunks and
embeddings are deliberately absent, and that is ADR-010's R4 rather than an
unfinished job: chunks and vectors are DERIVED -- `IngestionService.ingest`
takes the content as a parameter and `Document` carries a `source_uri`, so
they are reconstructible by re-ingesting the sources. Losing them costs time.
Losing an event or a memory loses information that cannot be recovered.

Binding both to one store is exactly what makes a server database look
necessary, and R5 records that the one real performance ceiling -- brute-force
cosine over every vector -- is in the store that does NOT need durability.

WHY A SEQUENCE COLUMN
---------------------
ADR-010's R3: `new_id()` is uuid4 and does not sort, and `utcnow()` collides
heavily (2000 successive calls gave 499-632 distinct values). The in-memory
repositories return insertion order, which is an artefact of a Python list
rather than a property anything guarantees.

`seq INTEGER PRIMARY KEY AUTOINCREMENT` makes the order a stored fact. This is
the one respect in which B is more robust than an append-only log: a log's
order is correct BECAUSE there is a single writer, and this is correct whether
or not that stays true.

ON FOREIGN KEYS
---------------
There are none, on purpose. The in-memory repositories accept a session whose
user does not exist, and two implementations of one contract that disagree
about what they accept cannot both be tested against that contract. If
referential integrity is wanted it belongs in the contract, enforced by both,
not in whichever backend finds it easy.

ON MIGRATIONS
-------------
`schema_version` exists and is checked on open. ADR-010 names migrations as
option B's real cost and the strongest argument for the append-only log, so
the version is recorded from the first row rather than after the first schema
change, when data already exists and the question is harder.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

from ..core.domain import (
    Event,
    EventType,
    Message,
    Role,
    Session,
    SessionStatus,
    User,
    utcnow,
)
from ..core.memory import (
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    language    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_session ON messages (session_id, seq);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    session_id  TEXT NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    message_id  TEXT,
    actor       TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events (session_id, seq);

CREATE TABLE IF NOT EXISTS memories (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    session_id      TEXT NOT NULL,
    type            TEXT NOT NULL,
    content         TEXT NOT NULL,
    language        TEXT NOT NULL,
    status          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    version         INTEGER NOT NULL,
    supersedes      TEXT,
    links           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    prov_session_id TEXT NOT NULL,
    prov_event_id   TEXT NOT NULL,
    prov_promoted_by TEXT NOT NULL,
    prov_promoted_at TEXT NOT NULL,
    prov_message_id TEXT
);
CREATE INDEX IF NOT EXISTS memories_by_status ON memories (status, seq);
"""


class SchemaVersionMismatch(RuntimeError):
    """The database on disk was written by a different schema version.

    Raised rather than migrated. A backend that silently adapts to a file it
    does not recognise is how data is quietly lost; this says what it found
    and what it expected, and leaves the decision to a human.
    """


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a database with the schema applied.

    WAL because the alternative is a reader blocking on a writer, and this is
    a read-heavy store: every turn reads the session's whole history.

    `check_same_thread=False` is NOT set. The deployment constraint is one
    user, one process, and the default's complaint about cross-thread use is a
    true statement about a program that has outgrown that constraint.
    """
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    # FULL, not NORMAL. NORMAL can lose the last transactions on power loss,
    # and this store exists because losing an event loses information.
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(_SCHEMA)

    row = connection.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )
        connection.commit()
    elif row["version"] != SCHEMA_VERSION:
        connection.close()
        raise SchemaVersionMismatch(
            f"database schema version is {row['version']}, this build writes "
            f"{SCHEMA_VERSION}. Nothing was read or written. Migrate the file "
            "deliberately rather than letting a mismatched build touch it."
        )
    return connection


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SqliteUserRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def add(self, user: User) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO users (id, created_at) VALUES (?, ?)",
                (user.id, _iso(user.created_at)),
            )

    def get(self, user_id: str) -> User | None:
        row = self._db.execute(
            "SELECT id, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return User(id=row["id"], created_at=_dt(row["created_at"]))


class SqliteSessionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def add(self, session: Session) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions (id, user_id, status, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    session.user_id,
                    session.status.value,
                    _iso(session.created_at),
                ),
            )

    def get(self, session_id: str) -> Session | None:
        row = self._db.execute(
            "SELECT id, user_id, status, created_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return Session(
            id=row["id"],
            user_id=row["user_id"],
            status=SessionStatus(row["status"]),
            created_at=_dt(row["created_at"]),
        )

    def update(self, session: Session) -> None:
        # The in-memory repository raises KeyError for an unknown session, and
        # so does this. An UPDATE that matches no row is a silent no-op, which
        # would make one implementation report a lost write and the other not.
        with self._db:
            cursor = self._db.execute(
                "UPDATE sessions SET user_id = ?, status = ?, created_at = ? "
                "WHERE id = ?",
                (
                    session.user_id,
                    session.status.value,
                    _iso(session.created_at),
                    session.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown session: {session.id}")


class SqliteMessageRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def add(self, message: Message) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO messages (id, session_id, role, content, language, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.session_id,
                    message.role.value,
                    message.content,
                    message.language,
                    _iso(message.created_at),
                ),
            )

    def list_for_session(self, session_id: str) -> Sequence[Message]:
        rows = self._db.execute(
            "SELECT id, session_id, role, content, language, created_at "
            "FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return tuple(
            Message(
                id=row["id"],
                session_id=row["session_id"],
                role=Role(row["role"]),
                content=row["content"],
                language=row["language"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        )


class SqliteEventRepository:
    """Append-only, and it exposes no update or delete -- like its in-memory
    counterpart, and for the reason in `core/domain.py`: an event is evidence,
    a correction is a new event.

    The payload is stored as JSON, which is safe because `Event.__post_init__`
    now refuses any payload that could not be written down. That check is
    ADR-010's prerequisite and it is why this method has no error path for an
    unserialisable value: it cannot be handed one.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def append(self, event: Event) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO events (id, session_id, type, payload, message_id, "
                "actor, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.session_id,
                    event.type.value,
                    json.dumps(dict(event.payload)),
                    event.message_id,
                    event.actor,
                    _iso(event.occurred_at),
                ),
            )

    def _read(self, rows) -> Sequence[Event]:
        return tuple(
            Event(
                id=row["id"],
                session_id=row["session_id"],
                type=EventType(row["type"]),
                payload=json.loads(row["payload"]),
                message_id=row["message_id"],
                actor=row["actor"],
                occurred_at=_dt(row["occurred_at"]),
            )
            for row in rows
        )

    def list_for_session(self, session_id: str) -> Sequence[Event]:
        return self._read(
            self._db.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        )

    def all(self) -> Sequence[Event]:
        return self._read(
            self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        )


class SqliteMemoryRepository:
    """`MemoryStore` on SQLite.

    `supersede` is ADR-010's R2 and the whole of this system's atomicity
    requirement: two writes that must both land or neither. A crash between
    them leaves either two ACTIVE records making contradictory claims, or a
    `supersedes` link to a record that was never demoted. Here it is one
    transaction -- solved by the engine rather than designed around, which is
    the criterion that separated option B from the append-only log.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def _write(self, record: MemoryRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO memories ("
            "  id, session_id, type, content, language, status, confidence,"
            "  version, supersedes, links, created_at, updated_at,"
            "  prov_session_id, prov_event_id, prov_promoted_by,"
            "  prov_promoted_at, prov_message_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.session_id,
                record.type.value,
                record.content,
                record.language,
                record.status.value,
                record.confidence,
                record.version,
                record.supersedes,
                json.dumps(list(record.links)),
                _iso(record.created_at),
                _iso(record.updated_at),
                record.provenance.session_id,
                record.provenance.event_id,
                record.provenance.promoted_by,
                _iso(record.provenance.promoted_at),
                record.provenance.message_id,
            ),
        )

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            session_id=row["session_id"],
            type=MemoryType(row["type"]),
            content=row["content"],
            language=row["language"],
            status=MemoryStatus(row["status"]),
            confidence=row["confidence"],
            version=row["version"],
            supersedes=row["supersedes"],
            links=tuple(json.loads(row["links"])),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            provenance=MemoryProvenance(
                session_id=row["prov_session_id"],
                event_id=row["prov_event_id"],
                promoted_by=row["prov_promoted_by"],
                promoted_at=_dt(row["prov_promoted_at"]),
                message_id=row["prov_message_id"],
            ),
        )

    def write(self, record: MemoryRecord) -> MemoryRecord:
        with self._db:
            self._write(record)
        return record

    def read(self, memory_id: str) -> MemoryRecord | None:
        row = self._db.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_active(self) -> Sequence[MemoryRecord]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE status = ? ORDER BY seq",
            (MemoryStatus.ACTIVE.value,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        old = self.read(old_id)
        if old is None:
            raise KeyError(f"unknown memory record: {old_id}")
        if old.status is not MemoryStatus.ACTIVE:
            raise ValueError(
                f"cannot supersede a record whose status is {old.status.value!r}"
            )
        superseded_old = replace(
            old, status=MemoryStatus.SUPERSEDED, updated_at=utcnow()
        )
        linked_new = replace(new_record, supersedes=old.id)
        # One transaction. Both rows or neither -- R2, and the reason this
        # store is SQLite rather than a log.
        with self._db:
            self._write(superseded_old)
            self._write(linked_new)
        return linked_new
