# ADR-003 — `Event != Memory` is a hard invariant

**Status:** Accepted · Phase 0

## Context

**VERIFIED SOURCE FACT.** Rico @ `215c3169` has no event module in `src/`.
`rico_memory.py` exposes `add_memory(...)` callable directly from a conversation turn, so a
passing remark becomes durable memory with no intermediate step. `rag-engine`'s event layer
is `store.py` (52 lines), `schema.py` (32) and `replay.py` (**4 lines**).

No audited source separates events from memories.

## Decision

Events and memories are distinct subsystems with distinct storage.

```text
conversation → EVENT → EXPERIENCE → MEMORY CANDIDATE → PROMOTION GATE → MEMORY
```

Events are append-only and immutable. Persistent memory is written **only** by the
promotion gate. No conversation path writes memory directly.

## Consequences

Memory stays clean at the cost of a pipeline that must be built from scratch — no source
provides it. Rejected candidates are retained as evidence; repeated rejection is itself a
signal. Every memory can name the event that produced it.

## Alternatives rejected

**Write memory directly and prune later** — Rico's model. Pruning polluted memory requires
provenance that direct writes never captured.
