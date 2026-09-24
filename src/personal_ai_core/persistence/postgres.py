"""PostgreSQL-backed repositories -- ADR-016, Option C on Neon.

This is the second durable backend. ADR-016 records the owner's decision that
the deployment constraint changed (a Neon endpoint is provisioned and supplied
through `DATABASE_URL`) and that a server database should back the stores that
cannot be rebuilt: events, messages, sessions, users and memory records.
Knowledge chunks and vectors stay out for the same reason as in `sqlite.py`:
they are DERIVED and rebuilt by re-ingestion (ADR-010 R4), so persisting them
would be accelerating a search over embeddings that do not mean anything yet
(R5).

THE FIRST NEW DEPENDENCY
------------------------
`sqlite.py` is stdlib; this backend cannot be. It uses psycopg (3.x), declared
as the `server` extra in `pyproject.toml` and nowhere in the default
dependency list, so a SQLite-only install (`pip install .`) never pulls a
driver it does not use. For the same reason the import is LAZY: psycopg is
imported inside `connect()` and never at module level, so importing this module
never requires the driver -- a machine without it can still run `pac` on
SQLite, and the conformance module can report a clean skip instead of an
ImportError. `# pyright: ignore` on the import keeps the type gate green on
machines that have not installed the extra.

IDENTICAL SEMANTICS, STATED
---------------------------
The point of a second backend is that the layers above cannot tell the
difference. Where SQLite and PostgreSQL would naturally disagree, this module
sides with the existing behaviour, and each such place is a parity decision:

- **TEXT timestamps.** PostgreSQL's native timestamp types carry timezone
  semantics and rendering that would make `_iso`/`_dt` round-trips and
  comparisons differ from the SQLite store. Both backends store ISO-8601 TEXT
  the same way, so the round-trip is identical by construction.
- **`INSERT OR REPLACE` is delete-then-insert.** SQLite's REPLACE removes the
  conflicting row and inserts a fresh one, advancing `seq` -- so a rewritten
  memory record moves to the end of `ORDER BY seq`. `ON CONFLICT ... DO
  UPDATE` would leave the old `seq` in place and the two backends would order
  differently, which is exactly the class of disagreement the substrate
  conformance suite exists to catch (it bit Phase 5's CI once). Users and
  sessions have no `seq`, so those use `ON CONFLICT ... DO UPDATE` directly;
  memories first DELETE then INSERT inside the same transaction, matching the
  SQLite behaviour (and R2: both rows of a `supersede` land or neither).
- **No foreign keys.** Same decision as `sqlite.py`: the in-memory
  repositories accept a session whose user does not exist, and two
  implementations that disagree about what they accept cannot be tested
  against one contract. Referential integrity, if it ever belongs anywhere,
  belongs in the contract, not in one backend.

ON MIGRATIONS
-------------
The same discipline as `sqlite.py`: a `schema_version` row is written on
first open, and a database that reports a different version is refused with
`SchemaVersionMismatch` rather than silently adapted (migrating a schema you
do not recognise is how data is quietly lost). This is the versioned-schema
half of the migrations story ADR-016 authorizes; additive changes land as a
deliberate schema-version bump with a migration applied in one transaction,
never as an in-place edit to `_SCHEMA` that leaves old databases behind.

CONNECTION LIFECYCLE
--------------------
`connect()` returns an autocommit connection and every repository write is
wrapped in `with connection.transaction():`. Two psycopg 3 facts make this
the only correct shape, and both are worth stating because each is a silent
data-loss trap on its own:

- Since psycopg 3.3, `with connection:` no longer manages a transaction: on
  exit it COMMITs *and closes the connection*. The first repository write
  would therefore kill the connection for everything after it (psycopg 3.2
  only committed; the code below would have been correct there and broken
  on the first 3.3 install, which is exactly what happened).
- On a non-autocommit connection, a `SELECT` leaves the connection inside an
  implicit transaction, so a following `transaction()` block becomes a
  nested SAVEPOINT that never COMMITs on exit -- the "read then write"
  pattern (`supersede`, most factory flows) would leave writes uncommitted
  until the process ended, when they are rolled back.

With autocommit, statements do not linger in an implicit transaction, so
every `transaction()` block is a real `BEGIN ... COMMIT` (or `ROLLBACK` on
error) and reads are durably visible as soon as they return. The per-block
commit is the same shape as the SQLite backend's `with self._db:` blocks,
which is what keeps the two engines' observable behaviour identical, and it
behaves identically across the psycopg 3.2-3.3 range the `server` extra
permits.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Sequence

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

if TYPE_CHECKING:
    import psycopg

    # The connection type, re-exported so callers can name it without
    # importing psycopg themselves: the factory's composition root is kept
    # driver-free by the dependency-direction gate ("may name adapters; it
    # still must not speak SQL itself").
    Connection = psycopg.Connection[Any]

SCHEMA_VERSION = 1

# Each statement is executed individually: psycopg 3 refuses multi-statement
# strings on purpose, and a schema applied via split() has a line of defence
# against a stray semicolon sliding a second statement into an execute().
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);",
    "CREATE TABLE IF NOT EXISTS users ("
    "    id TEXT PRIMARY KEY,"
    "    created_at TEXT NOT NULL"
    ");",
    "CREATE TABLE IF NOT EXISTS sessions ("
    "    id TEXT PRIMARY KEY,"
    "    user_id TEXT NOT NULL,"
    "    status TEXT NOT NULL,"
    "    created_at TEXT NOT NULL"
    ");",
    "CREATE TABLE IF NOT EXISTS messages ("
    "    seq BIGSERIAL PRIMARY KEY,"
    "    id TEXT NOT NULL UNIQUE,"
    "    session_id TEXT NOT NULL,"
    "    role TEXT NOT NULL,"
    "    content TEXT NOT NULL,"
    "    language TEXT NOT NULL,"
    "    created_at TEXT NOT NULL"
    ");",
    "CREATE INDEX IF NOT EXISTS messages_by_session ON messages (session_id, seq);",
    "CREATE TABLE IF NOT EXISTS events ("
    "    seq BIGSERIAL PRIMARY KEY,"
    "    id TEXT NOT NULL UNIQUE,"
    "    session_id TEXT NOT NULL,"
    "    type TEXT NOT NULL,"
    "    payload TEXT NOT NULL,"
    "    message_id TEXT,"
    "    actor TEXT NOT NULL,"
    "    occurred_at TEXT NOT NULL"
    ");",
    "CREATE INDEX IF NOT EXISTS events_by_session ON events (session_id, seq);",
    "CREATE TABLE IF NOT EXISTS memories ("
    "    seq BIGSERIAL PRIMARY KEY,"
    "    id TEXT NOT NULL UNIQUE,"
    "    session_id TEXT NOT NULL,"
    "    type TEXT NOT NULL,"
    "    content TEXT NOT NULL,"
    "    language TEXT NOT NULL,"
    "    status TEXT NOT NULL,"
    "    confidence DOUBLE PRECISION NOT NULL,"
    "    version INTEGER NOT NULL,"
    "    supersedes TEXT,"
    "    links TEXT NOT NULL,"
    "    created_at TEXT NOT NULL,"
    "    updated_at TEXT NOT NULL,"
    "    prov_session_id TEXT NOT NULL,"
    "    prov_event_id TEXT NOT NULL,"
    "    prov_promoted_by TEXT NOT NULL,"
    "    prov_promoted_at TEXT NOT NULL,"
    "    prov_message_id TEXT"
    ");",
    "CREATE INDEX IF NOT EXISTS memories_by_status ON memories (status, seq);",
)

_DROP_ALL = (
    "DROP TABLE IF EXISTS memories CASCADE",
    "DROP TABLE IF EXISTS events CASCADE",
    "DROP TABLE IF EXISTS messages CASCADE",
    "DROP TABLE IF EXISTS sessions CASCADE",
    "DROP TABLE IF EXISTS users CASCADE",
    "DROP TABLE IF EXISTS schema_version CASCADE",
)


class SchemaVersionMismatch(RuntimeError):
    """The database was written by a different schema version.

    Mirrors `sqlite.SchemaVersionMismatch` exactly (same name, same refusal
    to adapt): a backend that silently adjusts to a database it does not
    recognise is how data is quietly lost.
    """


def _driver() -> Any:
    try:
        import psycopg  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - exercised on driverless installs
        raise RuntimeError(
            "the server backend needs psycopg; install the extra with "
            "`pip install 'personal-ai-core[server]'`"
        ) from exc
    return psycopg


def connect(database_url: str) -> "psycopg.Connection[Any]":
    """Open the database, applying the schema and checking its version.

    The schema and its version row are applied in ONE transaction, so a half
    applied schema cannot exist on disk. A database whose version disagrees
    with this build's `SCHEMA_VERSION` is closed and refused -- nothing is
    read or written -- exactly as the SQLite backend refuses a foreign file.

    The connection is opened in autocommit mode: statement groups commit
    when their explicit `transaction()` block exits, and nowhere else. In
    particular `with connection:` must not be used -- psycopg 3.3 changed
    it to close the connection on exit (see the module docstring).
    """
    psycopg = _driver()
    from psycopg.rows import dict_row

    connection = psycopg.connect(database_url, autocommit=True)
    connection.row_factory = dict_row
    try:
        with connection.transaction():
            for statement in _SCHEMA:
                connection.execute(statement)
            row = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (%s)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] != SCHEMA_VERSION:
                raise SchemaVersionMismatch(
                    f"database schema version is {row['version']}, this build "
                    f"writes {SCHEMA_VERSION}. Nothing was read or written. "
                    "Migrate the database deliberately rather than letting a "
                    "mismatched build touch it."
                )
    except BaseException:
        connection.close()
        raise
    return connection


def drop_all(connection: "psycopg.Connection[Any]") -> None:
    """Remove every table this backend creates, in dependency order.

    Exposed for the conformance suite so a verification run against a real
    server leaves the database exactly as it found it -- a test that writes
    into a server database must also be able to clean up after itself.
    """
    with connection.transaction():
        for statement in _DROP_ALL:
            connection.execute(statement)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class PostgresUserRepository:
    def __init__(self, connection: "psycopg.Connection[Any]") -> None:
        self._db = connection

    def add(self, user: User) -> None:
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO users (id, created_at) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET created_at = EXCLUDED.created_at",
                (user.id, _iso(user.created_at)),
            )

    def get(self, user_id: str) -> User | None:
        row = self._db.execute(
            "SELECT id, created_at FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return User(id=row["id"], created_at=_dt(row["created_at"]))


class PostgresSessionRepository:
    def __init__(self, connection: "psycopg.Connection[Any]") -> None:
        self._db = connection

    def add(self, session: Session) -> None:
        # `ON CONFLICT ... DO UPDATE` has the same observable effect as
        # SQLite's `INSERT OR REPLACE` here: sessions have no seq, so there
        # is no ordering to preserve.
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO sessions (id, user_id, status, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  user_id = EXCLUDED.user_id,"
                "  status = EXCLUDED.status,"
                "  created_at = EXCLUDED.created_at",
                (
                    session.id,
                    session.user_id,
                    session.status.value,
                    _iso(session.created_at),
                ),
            )

    def get(self, session_id: str) -> Session | None:
        row = self._db.execute(
            "SELECT id, user_id, status, created_at FROM sessions WHERE id = %s",
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
        # The in-memory repository raises KeyError for an unknown session,
        # and so does this. An UPDATE that matched no row must not become a
        # silent no-op, or one implementation reports a lost write and the
        # other does not. (PostgreSQL 18 counts UPDATE matches even when the
        # values are unchanged, which is the only way this stays honest.)
        with self._db.transaction():
            cursor = self._db.execute(
                "UPDATE sessions SET user_id = %s, status = %s, created_at = %s "
                "WHERE id = %s",
                (
                    session.user_id,
                    session.status.value,
                    _iso(session.created_at),
                    session.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown session: {session.id}")


class PostgresMessageRepository:
    def __init__(self, connection: "psycopg.Connection[Any]") -> None:
        self._db = connection

    def add(self, message: Message) -> None:
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO messages (id, session_id, role, content, language, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s)",
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
            "FROM messages WHERE session_id = %s ORDER BY seq",
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


class PostgresEventRepository:
    """Append-only, and it exposes no update or delete -- like its in-memory
    and SQLite counterparts. The payload is stored as JSON; the serialisation
    contract checked at `Event` construction (ADR-010's prerequisite, closed
    by PR #41) is what makes that safe.
    """

    def __init__(self, connection: "psycopg.Connection[Any]") -> None:
        self._db = connection

    def append(self, event: Event) -> None:
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO events (id, session_id, type, payload, message_id, "
                "actor, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
                "SELECT * FROM events WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
        )

    def all(self) -> Sequence[Event]:
        return self._read(
            self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        )


class PostgresMemoryRepository:
    """`MemoryStore` on PostgreSQL -- ADR-016.

    `supersede` is the whole of the system's atomicity requirement and it is
    one transaction here as it is on SQLite: both rows land or neither. The
    DELETE-then-INSERT in `_write` reproduces SQLite's `INSERT OR REPLACE`
    seq-bumping behaviour so the two backends order rewritten records
    identically (see the module docstring).
    """

    def __init__(self, connection: "psycopg.Connection[Any]") -> None:
        self._db = connection

    def _write(self, record: MemoryRecord) -> None:
        self._db.execute("DELETE FROM memories WHERE id = %s", (record.id,))
        self._db.execute(
            "INSERT INTO memories ("
            "  id, session_id, type, content, language, status, confidence,"
            "  version, supersedes, links, created_at, updated_at,"
            "  prov_session_id, prov_event_id, prov_promoted_by,"
            "  prov_promoted_at, prov_message_id"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s)",
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
        with self._db.transaction():
            self._write(record)
        return record

    def read(self, memory_id: str) -> MemoryRecord | None:
        row = self._db.execute(
            "SELECT * FROM memories WHERE id = %s", (memory_id,)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_active(self) -> Sequence[MemoryRecord]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE status = %s ORDER BY seq",
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
        # One transaction. Both rows or neither -- R2.
        with self._db.transaction():
            self._write(superseded_old)
            self._write(linked_new)
        return linked_new