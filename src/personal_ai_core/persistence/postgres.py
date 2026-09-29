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
import os
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlparse

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


# ============================================================================
# THE DESTRUCTIVE-ENTRY GATE
# ============================================================================
#
# `_SCHEMA` and `_DROP_ALL` are the only two things in this file that can
# change the shape of a database, and both are exact inverses. Against a
# database that is not this project's, the pair is not a partial risk: applying
# `_SCHEMA` writes six tables into a stranger's schema, and `_DROP_ALL` drops
# whatever occupies those six names, CASCADE, taking the stranger's data with
# it. An unrelated application reachable through the same driver can have a
# table called `users` with real accounts behind it, and the name alone does not
# distinguish the two databases.
#
# So neither runs on a URL. Both run on a URL PLUS an explicit statement of what
# the caller intends to do and an independent statement of which database it
# believes it is reaching -- and both are checked against what the server
# actually says, read-only, BEFORE any DDL. A refusal leaves zero writes.


class SchemaIntent(str, Enum):
    """What the caller is about to do, declared rather than inferred.

    `INITIALIZE` creates this project's schema; `OPERATE` runs against one that
    already exists. The two are separate acts on purpose: creating a schema in a
    database is the destructive one, and a caller that means to read and write
# should have to say so before it can do it.
    """

    INITIALIZE = "initialize"
    OPERATE = "operate"


class DatabaseVerdict(str, Enum):
    """What a database turned out to be, decided read-only.

    Every member is produced by `decide_verdict` and consulted by every gate;
    `tests/unit/test_postgres_safety.py` asserts both directions so that no
    member can be added here and left unreachable.
    """

    VACUOUS = "vacuous"
    PAC = "pac"
    PARTIAL = "partial"
    FOREIGN = "foreign"
    OCCUPIED = "occupied"


class DatabaseIdentityError(RuntimeError):
    """The connection is not the database the caller said it was."""


class UnapprovedDatabaseError(RuntimeError):
    """A destructive operation was attempted without a live, matching approval."""


@dataclass(frozen=True)
class DatabaseIdentity:
    """Host, port and database -- the whole of what `connect` may confirm.

    The caller's `identity=` is an INDEPENDENT assertion of these three values.
    It is never derived from `database_url`: a value read off the URL the code
    is about to connect to is a restatement of its own input, which would make
    the check unfailable by construction rather than merely untrue.

    `database` alone cannot discriminate. An unrelated application this backend
    must never touch is also named `neondb`, so the name proves nothing and the
    host is what actually separates the two. Port is included for the same
    reason: one host serves several endpoints.

    Deliberately three fields. Schema is session execution context, not
    identity: it is pinned per transaction by `_pin_target_schema` and verified
    there, rather than asserted by a caller who could simply get it wrong.
    """

    host: str
    port: int
    database: str


@dataclass(frozen=True)
class TestDatabaseApproval:
    """The capability `drop_all` requires, minted by `approve_test_database`.

    A frozen value, not a private symbol: the guarantee is that `drop_all`
    cannot succeed against an unapproved database, which is a property of the
    runtime re-check and not of the token's name. Binding the schema as well as
    the identity makes the capability exact -- this host, port, database AND
    schema -- rather than "whatever the session resolved to at drop time".
    """

    identity: DatabaseIdentity
    schema: str


@dataclass(frozen=True)
class SchemaSnapshot:
    """A read-only observation of the target schema. Pure data, no behaviour."""

    relations: Mapping[str, str]
    columns: Mapping[str, tuple[tuple[str, str, bool], ...]]

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]]) -> SchemaSnapshot:
        relations: dict[str, str] = {}
        columns: dict[str, list[tuple[str, str, bool]]] = {}
        for row in rows:
            relations[row["relname"]] = row["relkind"]
            if row["attname"] is not None:
                columns.setdefault(row["relname"], []).append(
                    (row["attname"], row["ty"], bool(row["attnotnull"]))
                )
        return cls(relations, {k: tuple(v) for k, v in columns.items()})


@dataclass(frozen=True)
class DatabaseInspection:
    """Everything the gate learned, in one value, so refusals can say why."""

    verdict: DatabaseVerdict
    reason: str
    snapshot: SchemaSnapshot
    identity: DatabaseIdentity
    schema: str
    reported_database: str
    server_address: str


def database_identity_of(url: str) -> DatabaseIdentity:
    """Normalise a PostgreSQL URL down to (host, port, database).

    Everything that does not change WHICH database a URL reaches is discarded:
    scheme, credentials, query parameters, their order, and their absence. Two
    URLs differing only in `?sslmode=require&channel_binding=require` versus
    `?channel_binding=require&sslmode=require` name the same database, and a
    safety check that missed that would be trivially evaded by adding a
    parameter.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"URL names no host: {url!r}")
    try:
        port = 5432 if parsed.port is None else int(parsed.port)
    except ValueError as exc:  # non-numeric port
        raise ValueError(f"URL names an invalid port: {url!r}") from exc
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise ValueError(f"URL names no database: {url!r}")
    return DatabaseIdentity(host=host, port=port, database=database)


def live_identity_of(connection: Any) -> DatabaseIdentity:
    """The identity of the connection actually in hand, from libpq's report.

    Taken from `connection.info` -- the endpoint this session actually opened.

    NOT from `inet_server_addr()`. That returns the server's IP address, and a
    hostname never equals an IP literal, so comparing a caller's `host` against
    it could never match: the check would fail on every connection, including
    the correct ones. `inspect_database` keeps that value for diagnostics only.

    A Unix-socket connection reports its socket directory as the host and has no
    port, so it cannot be expressed in this model. Rather than approximate it
    and compare the wrong thing, this refuses and says so.
    """
    info = connection.info
    port = info.port
    if not isinstance(port, int):
        raise DatabaseIdentityError(
            "this connection is not a TCP endpoint, so it has no host/port "
            "identity to confirm; use a TCP URL"
        )
    return DatabaseIdentity(
        host=(info.host or "").lower(), port=int(port), database=info.dbname or ""
    )


# The shape `_SCHEMA` produces, read off PostgreSQL 18.6 rather than derived by
# hand. `confidence DOUBLE PRECISION` reports as `double precision`, not
# `float8`, which is exactly the kind of detail a hand-written expectation gets
# wrong and a live server does not. A change to `_SCHEMA` must change this, and
# `test_the_expected_shape_matches_what_the_schema_creates` is what says so.
_EXPECTED_TABLES: Mapping[str, tuple[tuple[str, str, bool], ...]] = {
    "events": (
        ("seq", "bigint", True),
        ("id", "text", True),
        ("session_id", "text", True),
        ("type", "text", True),
        ("payload", "text", True),
        ("message_id", "text", False),
        ("actor", "text", True),
        ("occurred_at", "text", True),
    ),
    "memories": (
        ("seq", "bigint", True),
        ("id", "text", True),
        ("session_id", "text", True),
        ("type", "text", True),
        ("content", "text", True),
        ("language", "text", True),
        ("status", "text", True),
        ("confidence", "double precision", True),
        ("version", "integer", True),
        ("supersedes", "text", False),
        ("links", "text", True),
        ("created_at", "text", True),
        ("updated_at", "text", True),
        ("prov_session_id", "text", True),
        ("prov_event_id", "text", True),
        ("prov_promoted_by", "text", True),
        ("prov_promoted_at", "text", True),
        ("prov_message_id", "text", False),
    ),
    "messages": (
        ("seq", "bigint", True),
        ("id", "text", True),
        ("session_id", "text", True),
        ("role", "text", True),
        ("content", "text", True),
        ("language", "text", True),
        ("created_at", "text", True),
    ),
    "schema_version": (("version", "integer", True),),
    "sessions": (
        ("id", "text", True),
        ("user_id", "text", True),
        ("status", "text", True),
        ("created_at", "text", True),
    ),
    "users": (
        ("id", "text", True),
        ("created_at", "text", True),
    ),
}

# `_SCHEMA` declares `seq BIGSERIAL` on messages, events and memories, and
# BIGSERIAL creates a sequence named `<table>_<column>_seq`. Verified on 18.6:
# exactly these three, and nothing else beyond the six tables and their indexes.
#
# They matter because a sequence IS a user-defined relation. Without naming them
# here, a correctly initialized database would read as OCCUPIED and OPERATE
# would refuse the database this backend itself created.
_EXPECTED_SEQUENCES = frozenset(
    {"messages_seq_seq", "events_seq_seq", "memories_seq_seq"}
)
_EXPECTED_RELATIONS = frozenset(_EXPECTED_TABLES) | _EXPECTED_SEQUENCES

# `relkind` values that count as a user-defined relation: ordinary and
# partitioned tables, views, materialized views, sequences, foreign tables and
# composite types. `i` and `I` (indexes) are excluded because every table has
# them and none is user-authored, and `T` (TOAST) likewise. `c` is included
# because `CREATE TYPE ... AS` is something a user did.
_USER_RELATION_KINDS = ("r", "p", "v", "m", "S", "f", "c")
_TABLE_KIND = "r"

# Which verdicts each gate admits. Stated as data rather than as scattered
# `is` comparisons so that "every verdict is decided somewhere" is a property a
# test can read, and a sixth enum member cannot be added and left unhandled.
_INITIALIZE_ALLOWS = frozenset({DatabaseVerdict.VACUOUS})
_OPERATE_ALLOWS = frozenset({DatabaseVerdict.PAC})
_APPROVAL_ALLOWS = frozenset({DatabaseVerdict.VACUOUS, DatabaseVerdict.PAC})
_DROP_ALLOWS = frozenset({DatabaseVerdict.VACUOUS, DatabaseVerdict.PAC})


def decide_verdict(snapshot: SchemaSnapshot) -> tuple[DatabaseVerdict, str]:
    """Classify a snapshot. Pure: no connection, no environment, no DDL.

    The ordering below is the safety argument, so it is worth stating why each
    rule sits where it does.

    KIND BEFORE SHAPE, and both before completeness. A relation called `users`
    that is a view would otherwise be compared column-by-column against a
    table's shape and, if the columns happened to line up, accepted as this
    project's table. Shape-beats-completeness for the same reason: on a foreign
    database, "most of the tables matched" is precisely the answer that fails
    silently.

    `VACUOUS` is EMPTINESS, not absence-of-collision. A database holding only
    `jobs`, `leads` and `paddle_*` collides with nothing and is still not
    empty, and treating it as a blank slate would create this project's tables
    inside somebody else's application. Name-collision detection is not
    emptiness detection, and only the second prevents the first.

    `PAC` is EXCLUSIVE. Six correct tables coexisting with fifty foreign
    relations is not this project's database; it is a database this project was
    installed into, and refusing it is the point.
    """
    expected = frozenset(_EXPECTED_TABLES)
    claimed = sorted(name for name in snapshot.relations if name in expected)
    for name in claimed:
        kind = snapshot.relations[name]
        if kind != _TABLE_KIND:
            return DatabaseVerdict.FOREIGN, (
                f"{name} exists but is a {kind}, not an ordinary table"
            )
        if snapshot.columns.get(name, ()) != _EXPECTED_TABLES[name]:
            return DatabaseVerdict.FOREIGN, f"{name} has unexpected columns"
    if len(claimed) == len(expected):
        present = set(snapshot.relations)
        if present == _EXPECTED_RELATIONS:
            return DatabaseVerdict.PAC, "this project's schema, and nothing else"
        outside = sorted(present - _EXPECTED_RELATIONS)
        if outside:
            return DatabaseVerdict.OCCUPIED, (
                f"this project's tables coexist with {len(outside)} unrelated "
                f"relations (e.g. {', '.join(outside[:3])})"
            )
        # All six tables, correct, but a relation this project's schema should
        # have is missing -- almost always one of the BIGSERIAL sequences, whose
        # absence leaves the column default dangling. A subset of PAC is
        # PARTIAL, not PAC: the check is equality, so an incomplete database is
        # refused rather than adopted.
        return DatabaseVerdict.PARTIAL, (
            "all six tables exist but "
            f"{len(_EXPECTED_RELATIONS - present)} expected relations are "
            f"missing (e.g. {', '.join(sorted(_EXPECTED_RELATIONS - present)[:3])})"
        )
    if claimed:
        return DatabaseVerdict.PARTIAL, (
            f"{len(claimed)} of {len(expected)} expected tables exist"
        )
    extra = sorted(snapshot.relations)
    if extra:
        return DatabaseVerdict.OCCUPIED, (
            f"the schema holds {len(extra)} unrelated relations and none of "
            f"this project's tables (e.g. {', '.join(extra[:3])})"
        )
    return DatabaseVerdict.VACUOUS, "no user-defined relations"


def _pin_target_schema(connection: Any) -> str:
    """Bind the current transaction to one schema, and return it.

    `_SCHEMA` writes unqualified names, so those names land wherever
    `search_path` resolves -- and `inspect_database` reads from
    `current_schema()`. If the two could disagree, the gate would classify one
    database and the DDL would write to another, and no amount of correct
    reasoning elsewhere would help. So the session is pinned FIRST, the value is
    read from the pin, and the inspection re-reads it: the property holds by
    construction and is then verified rather than assumed.

    `set_config(..., is_local := true)` is `SET LOCAL`, reverted on commit and
    on rollback alike, so nothing leaks to the caller. `quote_ident` is applied
    SERVER-side, so a schema name containing quotes, commas or whitespace is
    handled exactly by PostgreSQL's own rule and cannot inject. `pg_temp` is
    listed last deliberately: left implicit, a temporary table would be searched
    before the pinned schema and resolution would stop being deterministic.
    """
    schema = connection.execute("SELECT current_schema() AS n").fetchone()["n"]
    if not schema:
        raise DatabaseIdentityError(
            "search_path names no existing schema, so an unqualified CREATE "
            "TABLE would fail"
        )
    connection.execute(
        "SELECT set_config('search_path', "
        "       quote_ident(%s) || ', pg_catalog, pg_temp', true)",
        (schema,),
    )
    return schema


def inspect_database(connection: Any) -> DatabaseInspection:
    """READ-ONLY. Decide what this database is.

    One catalog query yields schema, relation kind and column shape together,
    from an explicit `pg_namespace` join. `to_regclass()` is deliberately not
    used: it resolves through `search_path` without stating a schema, so a
    relation of the same name in an earlier schema would be inspected, and it
    matches ANY relation kind, so a view could pass as a table.

    Writes nothing, deletes nothing, and is called before any DDL in the same
    transaction, so a refusal leaves the database exactly as it was found.
    """
    connection.row_factory = _dict_row()
    snapshot = SchemaSnapshot.from_rows(
        connection.execute(
            "SELECT c.relname, c.relkind, a.attname, "
            "       format_type(a.atttypid, a.atttypmod) AS ty, a.attnotnull "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attribute a "
            "  ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE n.nspname = current_schema() "
            f"  AND c.relkind IN ({', '.join(repr(k) for k in _USER_RELATION_KINDS)}) "
            "ORDER BY c.relname, a.attnum"
        ).fetchall()
    )
    verdict, reason = decide_verdict(snapshot)
    return DatabaseInspection(
        verdict=verdict,
        reason=reason,
        snapshot=snapshot,
        identity=live_identity_of(connection),
        schema=connection.execute("SELECT current_schema() AS n").fetchone()["n"],
        reported_database=connection.execute(
            "SELECT current_database() AS n"
        ).fetchone()["n"],
        # Diagnostics only. Never compared: this is an IP literal and a
        # hostname is never equal to one.
        server_address=connection.execute(
            "SELECT coalesce(inet_server_addr()::text, 'local') || ':' "
            "|| coalesce(inet_server_port()::text, '') AS a"
        ).fetchone()["a"],
    )


_PROTECTED_ENV = "PAC_PROTECTED_DATABASE_URLS"


def _protected_identities() -> frozenset[DatabaseIdentity]:
    """OPTIONAL hardening, from configuration. Empty unless an owner sets it.

    A production endpoint's hostname does not belong in library source, where
    it goes stale the moment an endpoint is rebuilt and cannot be updated by
    whoever owns it. So this reads a denylist instead of hardcoding one, and it
    is inert by default.

    It is a tripwire in front of the guard, not the guard. The guard is the
    shape check: a database holding an unrelated `users` table is FOREIGN and
    `approve_test_database` refuses it without consulting this at all. Configure
    it for the one case the shape check cannot cover -- an endpoint that is
    entirely pac-shaped and still must not be dropped.
    """
    raw = os.environ.get(_PROTECTED_ENV, "")
    if not raw.strip():
        return frozenset()
    found = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            found.add(database_identity_of(piece))
    return frozenset(found)


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


def _dict_row() -> Any:
    """The dict row factory, imported lazily for the same reason as the driver.

    Rows are read by COLUMN NAME throughout this module, and `dict_row` is
    what makes that possible. It is fetched through a function rather than
    imported at module level so that importing this file on a machine with no
    psycopg installed still works -- the module docstring's whole argument
    about the `server` extra depends on that staying true.
    """
    from psycopg.rows import dict_row  # pyright: ignore[reportMissingImports]

    return dict_row


def connect(
    database_url: str,
    *,
    intent: SchemaIntent,
    identity: DatabaseIdentity,
) -> psycopg.Connection[Any]:
    """Open the database, after checking it is the one the caller named.

    `intent` and `identity` are REQUIRED and keyword-only, with no defaults. A
    default would be a value the caller did not choose, and a value derived
    from `database_url` would be a restatement of this function's own input --
    either way the check could not fail, which is the same as not having it.

    The order is fixed and every step precedes any DDL:

      1. pin `search_path` to one schema for this transaction, so the
         inspection and the DDL that follows cannot target different schemas;
      2. inspect, read-only, IN THE SAME TRANSACTION as the DDL -- so the
         catalog snapshot and the statements it authorises share one snapshot
         and a concurrent `DROP` cannot make the verdict describe a database
         that no longer exists by the time `_SCHEMA` runs;
      3. confirm the live identity equals what the caller asserted, on all
         three fields;
      4. confirm the verdict is one this intent admits;
      5. only then, and only for `INITIALIZE`, apply `_SCHEMA`.

    `OPERATE` applies NO DDL at all -- not even `CREATE TABLE IF NOT EXISTS`,
    which would be a write on every open and would mask the fact that it is
    being used against a database that was never initialized.

    Any refusal closes the connection and raises before a single row is
    written. The transaction rolls back, so the database is as it was found.

    The connection is opened in autocommit mode: statement groups commit when
    their explicit `transaction()` block exits, and nowhere else. In particular
    `with connection:` must not be used -- psycopg 3.3 changed it to close the
    connection on exit (see the module docstring).
    """
    psycopg = _driver()

    connection = psycopg.connect(database_url, autocommit=True)
    connection.row_factory = _dict_row()
    try:
        with connection.transaction():
            schema = _pin_target_schema(connection)
            inspection = inspect_database(connection)
            if inspection.schema != schema:
                raise DatabaseIdentityError(
                    "the target schema moved between the search-path pin and "
                    f"the inspection ({schema} -> {inspection.schema}). "
                    "Nothing was written."
                )
            live = inspection.identity
            if (live.host, live.port, live.database) != (
                identity.host,
                identity.port,
                identity.database,
            ):
                raise DatabaseIdentityError(
                    f"confirmed {identity.host}:{identity.port}/{identity.database}, "
                    f"connected to {live.host}:{live.port}/{live.database} "
                    f"(server address {inspection.server_address}). "
                    "Nothing was written."
                )
            if live.database != inspection.reported_database:
                raise DatabaseIdentityError(
                    f"the URL named {live.database} but the server reports "
                    f"{inspection.reported_database}. Nothing was written."
                )
            if intent is SchemaIntent.INITIALIZE and (
                inspection.verdict not in _INITIALIZE_ALLOWS
            ):
                raise DatabaseIdentityError(
                    "INITIALIZE requires an empty database; this one is "
                    f"{inspection.verdict.value}: {inspection.reason}. "
                    "Nothing was written."
                )
            if intent is SchemaIntent.OPERATE and (
                inspection.verdict not in _OPERATE_ALLOWS
            ):
                raise DatabaseIdentityError(
                    "OPERATE requires this project's schema; this one is "
                    f"{inspection.verdict.value}: {inspection.reason}. "
                    "Nothing was written."
                )
            if intent is SchemaIntent.INITIALIZE:
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


def approve_test_database(connection: Any) -> TestDatabaseApproval:
    """Mint the capability `drop_all` requires, or refuse to.

    Reads `POSTGRES_TEST_URL` and configuration. It never reads `DATABASE_URL`,
    and no module under `src/` names that variable at all; whether the two URLs
    point at the same database is a TEST-policy question and lives in
    `tests/support/postgres_target.py`, where it can be compared with the
    normaliser instead of with string equality.

    Three conditions, all required:
      - `POSTGRES_TEST_URL` is set, and names the database this connection
        actually reached -- compared through `database_identity_of`, so a
        differing query string or credential cannot make a foreign URL look
        like a different one;
      - the verdict is VACUOUS or PAC, so a test run can only ever clean up
        after this project;
      - the endpoint is not on the optional configured denylist.
    """
    declared = os.environ.get("POSTGRES_TEST_URL", "")
    if not declared:
        raise UnapprovedDatabaseError(
            "POSTGRES_TEST_URL is not set, so there is no approved test "
            "database. Nothing was dropped."
        )
    inspection = inspect_database(connection)
    live = inspection.identity
    wanted = database_identity_of(declared)
    if live != wanted:
        raise UnapprovedDatabaseError(
            f"POSTGRES_TEST_URL names {wanted.host}:{wanted.port}/"
            f"{wanted.database}, but this connection is {live.host}:"
            f"{live.port}/{live.database}. Nothing was dropped."
        )
    if inspection.verdict not in _APPROVAL_ALLOWS:
        raise UnapprovedDatabaseError(
            f"a test database holds foreign objects: {inspection.reason}. "
            "Nothing was dropped."
        )
    if live in _protected_identities():
        raise UnapprovedDatabaseError(
            f"{live.host}:{live.port}/{live.database} is listed in "
            f"{_PROTECTED_ENV}. Nothing was dropped."
        )
    return TestDatabaseApproval(identity=live, schema=inspection.schema)


def drop_all(connection: Any, *, approval: TestDatabaseApproval) -> None:
    """Remove every table this backend creates, in dependency order.

    Exposed for the conformance suite so a verification run against a real
    server leaves the database exactly as it found it -- a test that writes
    into a server database must also be able to clean up after itself.

    Requires an approval, and RE-VERIFIES it against the live connection: a
    token is not authority. An approval minted for one database is checked
    again here, by re-reading the identity and the schema from the server, so
    holding a valid approval does not make one database's passkey open another
    database's door. The verdict is re-checked for the same reason -- the
    approval says this was a safe database when it was minted, and the question
    is worth asking again now.
    """
    with connection.transaction():
        # Pinned for its EFFECT, not for the name it returns: `inspect_database`
        # reads `current_schema()` on this same transaction, so the pin this
        # call installs is the one the inspection is taken under. Naming the
        # result would imply the name is what makes the two agree, and it is
        # not -- the shared transaction is.
        _pin_target_schema(connection)
        inspection = inspect_database(connection)
        live = inspection.identity
        if live != approval.identity or inspection.schema != approval.schema:
            raise UnapprovedDatabaseError(
                f"approved for {approval.identity.host}:"
                f"{approval.identity.port}/{approval.identity.database} in "
                f"{approval.schema}, connected to {live.host}:{live.port}/"
                f"{live.database} in {inspection.schema}. Nothing was dropped."
            )
        if live in _protected_identities():
            raise UnapprovedDatabaseError(
                f"{live.host}:{live.port}/{live.database} is listed in "
                f"{_PROTECTED_ENV}. Nothing was dropped."
            )
        if inspection.verdict not in _DROP_ALLOWS:
            raise UnapprovedDatabaseError(
                f"a test database holds foreign objects: {inspection.reason}. "
                "Nothing was dropped."
            )
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
