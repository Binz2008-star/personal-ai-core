# ADR-016 — Production memory persistence (server database)

**Status:** Accepted · Phase 6
**Owner decision:** 2026-09-24

Records the owner's decision to move the durable stores to a server database
(Neon / PostgreSQL), which reverses ADR-010's explicit block on Option C.

## Context

ADR-010 (PROPOSED, PR #15) compared four persistence options and recommended
**D with B** — SQLite as the durable store, knowledge left in memory. It rated
Option C (PostgreSQL / Neon) as "explicitly blocked by owner authorization,"
and as "not worth revisiting unless the deployment constraint changes."

The deployment constraint has changed. On 2026-09-24 the owner:

- provisioned a Neon project endpoint (connection supplied via `DATABASE_URL`,
  database `neondb`, `sslmode=require`), and
- set `NEON_API_KEY` in the environment (Neon MCP tooling is configured in the
  development harness), and
- authorized production memory persistence.

That is the exact condition ADR-010 named: the local-machine-only assumption no
longer governs durability. Per ADR-010's own criteria, Option C becomes the
right answer precisely when concurrent-writer support and managed durability
are wanted — the server database is chosen for managed durability, not for its
query engine (R1: reads remain point lookups and filtered scans).

## Decision

**Option C (Neon / PostgreSQL) for the two non-rebuildable stores** — events,
messages, sessions and users (R4, append-only, cannot be rebuilt), and memory
records (curated by the promotion gate, cannot be rebuilt).

**Option D's insight is retained for the third store:** knowledge chunks and
embeddings are derived from the sources and are rebuilt by re-ingestion, so
they stay out of the durable store. There is no pgvector, no embedding adapter,
and no ANN index in this phase (R5: `HashingEmbeddingProvider` captures surface
overlap, not meaning, so accelerating search over it would be premature).

## Design decisions

These are carried over unchanged from ADR-010's criteria, which the change of
backend does not disturb:

- **R1 — reads stay trivial.** No joins, no aggregation, no ranking in SQL.
  Ranking remains in Python (`SimpleMemoryRetriever`), where it is testable.
- **R2 — `supersede` is one transaction.** Two writes land or neither; solved
  by the engine, exactly as SQLite did.
- **R3 — ordering has a durable basis.** A `BIGSERIAL` (or trusted-key)
  sequence column gives a total order that does not depend on the
  single-writer constraint holding.
- **Synchronous only.** No `async`/`await` anywhere in `src/`; the backend is
  another synchronous adapter under `persistence/`.
- **Event != Memory is untouched** (ADR-003). The sealed store stays sealed;
  `ExperiencePipeline` remains the sole writer to `MemoryStore` whatever backs
  it. `ConversationService` still performs no memory writes.
- **Dependency direction is untouched.** A backend imports only `core`.

## New dependency — the first since the standard-library rule

`persistence/sqlite.py` is stdlib. PostgreSQL requires a driver. A single new
dependency (psycopg) is added for the server backend. This is recorded here
explicitly because "no new dependency" was an invariant of the project's
persistence work; the owner's authorization for production persistence
covers it.

The SQLite backend remains the default for `pac` and for tests; the server
backend is an opt-in composition selected by environment (`DATABASE_URL`).

## Migrations

The repository has no migration framework — "no migrations framework" is a
current-limitation line in PROJECT_STATE.md, and migrations are a gated risk.
A server database with live data requires one, so a small, explicit one is
introduced with this backend:

- a versioned schema, applied transactionally,
- additive-first policy (no destructive changes in the first delivery),
- exercised by tests that apply the schema from scratch and re-open it.

The owner's authorization covers migrations as an inseparable part of running
a server database.

## Prerequisites

- **`Event.payload` serialisation contract: CLOSED** (PR #41). A durable
  `EventRepository` is unblocked.
- The smaller prerequisite (a caller cannot distinguish a completed `add`/`append`
  from an accepted one) is recorded, not fixed here.

## Explicitly out of scope (still blocked)

- pgvector, production embedding adapter, knowledge persistence, ANN indexing.
- BM25/stemming redesign; Boss model replacement; modifications to legacy
  repositories.

## Consequences

- SQLite stays the default; the server composition is opt-in via environment.
- The conformance suite parametrises over both substrates; the Postgres cases
  skip — accounted in `test_expected_skips` — when no server or driver is
  present, and run when `DATABASE_URL` (or a test override) is set.
- `MemoryStore` and the repository protocols are unchanged: this boundary was
  built for this substitution and is its second real exercise.

## Decision

**Accepted.** Implementation proceeds as Phase 6 behind this record, PR by PR,
each landing through the repo's normal merge workflow and gate.