# ADR-014 — Memory ownership for cross-session recall

**Status:** Accepted · Phase 5

## Context

ADR-009 deferred cross-session recall and named three things it needed:

1. A link from `MemoryRecord` to a user identity, or a resolvable path from
   `session_id` to `user_id` via `SessionRepository`.
2. A widening of the hard filter from one session to that user's session set,
   still enforced at the reader boundary.
3. A decision about cross-session conflict handling.

Points 1 and 2 are Phase 5 scope — this ADR and ADR-015. Point 3 is already
settled on the **write** side: `ExperiencePipeline.ingest` passes
`self._store.list_active()` — every active record, across all sessions, not
just the current one (`memory/pipeline.py`) — to the promotion gate. A
contradiction promoted in a later session is therefore HELD against the active
record from the earlier session at promotion time, and supersession is the
explicit, transactional way to demote an old claim. Cross-session recall does
not need new conflict machinery; it inherits the gate's.

ADR-010 separated durability ("whether anything survives process exit") from
cross-session semantics ("whose memories a turn may recall") and explicitly
left the ownership representation open:

> Cross-session recall needs a `user_id` on `MemoryRecord` or a session→user
> resolution (ADR-009). That is a separate design.

The question this ADR settles: **which of those two representations owns
memory in Phase 5?**

## Decision

**Option A — derive ownership.** A memory record's owner is resolved through
its session:

```text
MemoryRecord.session_id
        ↓
Session.user_id
```

The resolution is injected into `MemoryReader` as a `session_owner` callable,
so the read side stays read-only and the store stays unchanged. No `user_id`
is persisted on `MemoryRecord`, and the SQLite `memories` table is untouched.

Option B (persisting `user_id` on `MemoryRecord`) is rejected for Phase 5:

- It requires a `memories.user_id` column and a schema-version change, and the
  persistence layer deliberately refuses silent migration
  (`SchemaVersionMismatch` raises rather than adapting). Introducing a
  deliberate migration now would be infrastructure work in service of a
  representation the current domain does not need.
- The domain already guarantees a resolvable path: `sessions.user_id` is NOT
  NULL, and `SessionRepository.get(session_id)` exists on both stores. Every
  memory record was promoted from an experience that carries a session, so
  every record's owner is resolvable today.
- Option A keeps provenance session-based. The audit trail's unit of meaning
  is the session; a duplicated user column would have to be kept in agreement
  with `sessions` forever.

Option B remains open as a separate decision if a future phase ever writes
memory with no session anchor (for example, imported memory). That is not
Phase 5, and ADR-010's rule applies: do not build the migration until the use
it serves is real.

## Consequences

- User-scoped recall needs a resolver. A `MemoryReader` built without one can
  still answer the default session-scoped reads; requesting user scope through
  it raises, which the retriever classifies `UNAVAILABLE` — the same degraded,
  visible failure mode as an unreachable store.
- A record whose session resolves to no user has no owner and is never
  recalled under any user. An anchor session that resolves to no user recalls
  nothing. Both are the isolation guarantee working, not an error.
- No schema change, no migration, no `MemoryStore` widening. The sole writer,
  the promotion pipeline, and the sealed conversation-path store are all
  untouched.

## Alternatives considered

**Option B, persist `user_id` now.** Rejected above: needs a migration for no
present benefit; the join already exists and is authoritative.

**A third model, memory-scoped ownership outside users.** Rejected by the
Phase 5 baseline: no such domain exists, and inventing one is beyond the
authorized scope.