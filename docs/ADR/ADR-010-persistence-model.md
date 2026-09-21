# ADR-010 — Persistence model

**Status:** PROPOSED — design only. Not accepted. No implementation authorized.

This ADR compares alternatives and recommends one. It does not authorize code, a
schema, a migration, or any dependency. Nothing in `src/` changes on its account.

## Context

Everything the Core stores lives in process memory and is lost on exit. `persistence/`
contains `in_memory.py` and `memory_store.py`; `grep` for `psycopg`, `sqlite`,
`sqlalchemy` or `asyncpg` across `src/` returns nothing.

This ADR is about **durable persistence** and nothing else. It does not gate any phase.

### Not to be conflated with cross-session memory

An earlier draft of this ADR claimed that "a memory that does not survive the process
cannot be cross-session, so Phase 5 cannot begin until this is settled." **That claim
is false and has been removed.** It confused two independent concerns:

| | |
|---|---|
| **Cross-session memory semantics** | *Whose* memories a turn may recall. Today `MemoryQuery.session_id` is a hard filter in `MemoryReader.list_active_for_session` (ADR-009). Widening it is a contract and policy question. |
| **Durable persistence** | *Whether* anything survives process exit. This ADR. |

They are orthogonal. Demonstrated against the current in-memory repositories: two
sessions write to the same `InMemoryMemoryRepository` within one process, and both
records are present in `list_active()`. Session-scoped recall returns only the second;
the first is filtered out by the reader, not absent from the store.

```
session-scoped (today)   session-B sees: ['something else']
unfiltered (the store)   session-B sees: ['user prefers Arabic', 'something else']
```

**What blocks cross-session recall is the filter, not the lack of durability.** So
cross-session memory contracts can be designed and validated against the existing
in-memory repositories, and this ADR takes no position on when or whether that happens.
If evidence later shows a cross-session contract that genuinely cannot be exercised
in-process, that evidence — not this document — is what would change the sequencing.

### The deployment constraint, and what kind of claim it is

`ARCHITECTURE.md` §1 records an owner decision: **one user, one process, local
machine**. Two distinct things follow, and they should not be read as one:

- **The constraint is a decision**, recorded in a document whose header states it
  describes the target architecture. It is not an observed property of a running
  system, and nothing enforces it.
- **The current implementation is consistent with it** — separately verified, and this
  part is evidence: no `async`/`await`, no threading or asyncio import, and no lock
  anywhere in `src/`.

The options below are weighed against the constraint as a *decision*. Where the text
cites code, it is citing implementation evidence. Where it cites the constraint, it is
citing a choice that can be revisited.

## Requirements, derived rather than assumed

Read off the contracts and implementations at `5dcf3ce`, not from what a storage layer
usually needs.

### R1 — The reads are trivial

Every read in `core/contracts.py` is a point lookup by id or a scan filtered by one
key:

```
UserRepository.get(user_id)              MemoryStore.read(memory_id)
SessionRepository.get(session_id)        MemoryStore.list_active()
MessageRepository.list_for_session(id)   EventRepository.list_for_session(id)
```

No joins. No aggregation. No range queries. No secondary indexes. Ranking happens in
Python: `SimpleMemoryRetriever` scores `0.5·confidence + 0.3·language + 0.2·overlap`
after the rows are in hand.

**A query engine is not what this system needs.** Nothing here is easier to express in
SQL than in a dictionary.

### R2 — `supersede` is the only multi-write operation

```python
self._records[old_id] = superseded_old     # ACTIVE -> SUPERSEDED
self._records[linked_new.id] = linked_new  # new record, supersedes=old.id
```

Two writes that must both land or neither. A crash between them leaves either two
ACTIVE records making contradictory claims, or a `supersedes` link to a record that was
never demoted. This is the whole of the atomicity requirement — there is no other
multi-row write anywhere in the Core.

### R3 — Ordering has no durable basis today

`new_id()` is `uuid4` and does not sort. `utcnow()` collides heavily: 2000 successive
calls yielded 499–632 distinct values across runs. `InMemoryEventRepository` returns
events in list-insertion order, which is an artefact of the list.

Under a single writer, **append order is a total order** and this problem does not
arise. Under any other deployment it does, and the fix is a schema change after data
exists.

### R4 — The three stores have genuinely different needs

Treating persistence as one decision is the mistake to avoid.

| Store | Volume | Mutability | Rebuildable? |
|---|---|---|---|
| events, messages, sessions, users | grows without bound | append-only | **No** |
| memory records | small — the promotion gate curates | status mutates via `supersede` | **No** |
| knowledge chunks + embeddings | large, proportional to corpus | replaced on re-ingest | **Yes** |

The third row is the important one. `IngestionService.ingest(document, content)` takes
content as a parameter and `Document` carries a `source_uri`: chunks and vectors are
**derived data**, reconstructible by re-ingesting the sources. Losing them costs time,
not information. Losing an event or a memory loses information that cannot be recovered.

So durability is a hard requirement for two stores and a performance convenience for
the third. A single backend chosen for all three optimizes the wrong thing.

### R5 — Vector search is where a scale problem actually exists

`InMemoryVectorIndex.search` is a brute-force cosine over every entry, in Python. That
is the one place with a real performance ceiling, and the only place an ANN index or
pgvector would earn its cost. It is also the store that does not need durability.

Worth stating plainly: `HashingEmbeddingProvider` captures surface overlap, not
semantics. Investing in ANN infrastructure to accelerate search over vectors that do
not carry meaning would be premature regardless of backend.

## Options

### A — Append-only file log (+ in-memory projection)

Events and memory writes append JSON lines to a file; on start the file is replayed to
rebuild in-memory state. Knowledge stays in memory, rebuilt by re-ingestion.

- **Ordering** File position. A total order, free, under a single writer.
- **Durability** `write` + `fsync`. Explicit and auditable.
- **Atomicity** A single `write` under `PIPE_BUF` is atomic. R2 needs two records
  atomically, which means one framed record containing both, or a commit marker —
  a real design detail, not a hand-wave.
- **Concurrent writers** Not supported. Out of scope by constraint.
- **Crash recovery** Truncate a trailing partial record, replay the rest. Simple to
  reason about and to test.
- **Migration burden** None. A version tag per record; readers tolerate old shapes.
- **Query** Replay-and-filter. Adequate for R1; nothing else is possible without an
  index.
- **`MemoryStore` fit** Direct. `list_active()` reads the projection.
- **`EventRepository` fit** Direct; the contract is already append-only.
- **Neon / pgvector / migrations** None.
- **Cost** The whole active set must fit in memory. For memory records the gate keeps
  that small; for events it does not, and an unbounded log eventually will not replay
  in acceptable time. Compaction or snapshotting becomes necessary, and it is the one
  piece of real engineering this option requires.

### B — Embedded relational (SQLite)

One file, no server, ACID, ships with Python.

- **Ordering** `INTEGER PRIMARY KEY AUTOINCREMENT` gives a monotonic sequence — and
  gives it independently of the single-writer constraint, which is the one respect in
  which B is more robust than A.
- **Durability** WAL mode, well-trodden.
- **Atomicity** R2 is one transaction. Solved rather than designed.
- **Concurrent writers** One writer, many readers — exceeds the requirement.
- **Crash recovery** Handled by the engine.
- **Migration burden** Real. Schema changes need migrations once data exists. Lower
  than a server database, not zero.
- **Query** Far more than R1 needs. Available if requirements grow.
- **`MemoryStore` fit** Direct.
- **`EventRepository` fit** Direct, with the sequence column R3 wants.
- **Neon / pgvector / migrations** No Neon, no pgvector. Migrations: yes, eventually.
- **Cost** A schema, and the discipline a schema imposes. Also the temptation to push
  ranking into SQL, which would move policy out of Python where it is currently
  testable.

### C — Server relational (PostgreSQL / Neon, ± pgvector)

- **Ordering, durability, atomicity, recovery** All solved, thoroughly.
- **Concurrent writers** Full support — for a requirement that does not exist.
- **Migration burden** Highest. Migrations, connection management, a service to run or
  a network to depend on.
- **Query** pgvector would replace R5's brute-force scan.
- **Cost** Contradicts "local machine" unless self-hosted, and then adds an operational
  dependency to a personal tool. **Explicitly blocked by owner authorization.**
- **Verdict** The concurrency and availability it buys are exactly what the deployment
  constraint says are not needed. It would be chosen for the vector index alone — and
  R5 argues that is premature while embeddings are a hashing stand-in.

### D — Hybrid: durable log for truth, rebuildable index for search

Events and memories in A or B; knowledge left in memory, rebuilt by re-ingestion, with
an ANN index added later *only* if measurement shows brute-force cosine is the
bottleneck **and** real embeddings make the ranking worth accelerating.

This is not a fourth backend. It is the recognition that R4 describes two problems, and
that binding them to one store is what makes option C look necessary.

## Recommendation

**This is a persistence decision only.** Choosing among A–D authorizes nothing about
cross-session memory, and authorizing cross-session memory would not select an option
here. Neither decision implies the other, and neither is taken in this document.

**Option D, with B (SQLite) as the durable store.**

Reasoning, in the order the criteria actually decide it:

1. **Concurrent writers: none required.** This eliminates C's principal advantage.
2. **Atomicity: R2 is genuine.** A gives ordering free but makes the two-record write a
   design problem to solve and test. B makes it one transaction. This is the criterion
   that separates A from B.
3. **Ordering: B does not depend on the constraint holding.** A's total order is
   correct *because* there is one writer. B's sequence column is correct regardless.
   Since the constraint is the thing most likely to change, the option that survives
   its change is worth a modest cost.
4. **Durability by store: R4.** Knowledge is derived and stays out of the durable
   store. This removes the only requirement that pointed at C.
5. **Migrations: real but small.** B's burden is a genuine cost, honestly the strongest
   argument for A. Two things tip it: the schema is small (five tables of flat records),
   and SQLite tolerates additive change well.

**A is the credible alternative, and the case for it is not weak.** It has no schema and
no migrations, which matters for a project whose governance treats migrations as a
gated risk. If the owner weighs "no migrations, ever" above "atomicity handled by the
engine", A is defensible and I would not argue it is wrong. The recommendation rests on
R2 and on point 3, not on SQL being generally preferable.

**C is not recommended** and should not be revisited unless the deployment constraint
changes.

## What this does not decide

- **Anything about cross-session memory.** Cross-session recall needs a `user_id` on
  `MemoryRecord` or a session→user resolution (ADR-009). That is a separate design
  question, exercisable against the in-memory repositories, and nothing here advances
  or blocks it.
- **Schema.** No tables, columns or types are proposed here.
- **Where the file lives**, and how configuration names it.
- **Snapshot or compaction policy**, if A is chosen.
- **Whether the event log is bounded at all.** R4 notes it grows without bound; no
  retention policy exists, under any option.

## Prerequisite — for a durable backend only

Classified precisely, because the earlier draft overstated its reach:

- It **is** a prerequisite for implementing a durable `EventRepository` or any backend
  that must write an event to disk.
- It is **not** a prerequisite for defining a cross-session memory contract. That
  contract concerns `MemoryRecord` and the reader's filter; it does not serialise
  anything.

**`Event.payload` has no serialisation contract.** It is typed `Mapping[str, Any]` and
nothing validates the values. Verified:

```
Event(payload={'obj': object(), 'fn': len})   ->  constructs successfully
json.dumps(dict(event.payload))               ->  TypeError: not JSON serializable
```

Any durable backend must serialise this. Today a caller can record an event that cannot
be stored, and nothing says so until the write fails — at which point the event is the
thing being lost. Whichever of A–D is chosen, this contract has to be settled before
that backend is built, and it is a change to `core/domain.py`, not to a persistence
adapter.

A second, smaller one: `add` and `append` return `None`, so a caller cannot distinguish
a completed write from an accepted one. With an in-memory dict the distinction is empty.
With a durable store and an `fsync` policy it is not.

## Consequences if accepted

- `MemoryStore` and the repository protocols are unchanged. That boundary was built for
  this substitution; this ADR is its first real exercise.
- Contracts stay synchronous, per the deployment constraint.
- Ranking stays in Python. No policy moves into storage.
- `Event != Memory` (ADR-003) is unaffected: the sealed store stays sealed, and the
  promotion pipeline remains the sole writer to `MemoryStore` whatever backs it.
- Dependency direction is unaffected: a backend is another adapter under
  `persistence/`, importing only `core`.

## Decision

**None.** This ADR is proposed for review. It recommends D+B and records why A remains
defensible.

Its scope is durable persistence. It does not gate, sequence or authorize Phase 5, and
Phase 5 remains NOT AUTHORIZED / DESIGN NOT STARTED independently of it. Implementation
of any option requires separate owner authorization, and the `Event.payload`
prerequisite has to be resolved before durable backend work begins.
