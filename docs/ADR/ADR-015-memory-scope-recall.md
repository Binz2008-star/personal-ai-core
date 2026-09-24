# ADR-015 — Memory scope and recall semantics

**Status:** Accepted · Phase 5

## Context

Phase 4 (ADR-009) made recall session-scoped: `MemoryQuery` requires a
`session_id`, and `MemoryReader.list_active_for_session` applies it as a hard
filter at the reader boundary so no caller can widen it. ADR-009 rejected a
per-query scope flag at the time because "a configuration flag whose second
setting has no correct implementation behind it is not a choice, it is a
trap."

ADR-014 now provides the missing second setting: user ownership derived from
`session_id → user_id`. The trap ADR-009 warned about no longer exists, so the
scope contract can be made explicit.

## Decision

**One query type, one explicit scope.** `MemoryQuery` gains a `scope` field:

```text
MemoryScope.SESSION   (default)   a turn recalls only this session's memories
MemoryScope.USER                  a turn recalls the owner's memories,
                                  across every session that owner has
```

- **The default is SESSION.** Existing behavior is preserved exactly: every
  production query construction stays session-scoped, and the conversation
  path's `ContextBuilder` continues to build queries with the default. Nothing
  silently broadens.
- **USER is explicit.** It is requested on the query; it is never inferred.
- **Eligibility** is unchanged for both scopes and lives in the store:
  `list_active()` returns `ACTIVE` records only, so `REJECTED` and
  `SUPERSEDED` records are excluded by construction. `language` remains a
  ranking signal, never a hard exclusion.
- **Isolation** is enforced at the reader boundary, as ADR-009 required. The
  reader resolves the anchor session to a user, then admits exactly the active
  records whose own sessions resolve to that same user. Another user's
  memories are never eligible, and records with no resolvable owner are never
  attributed.
- **Ordering** is deterministic in both scopes: `SimpleMemoryRetriever`'s
  total order — score, then recency, then id — applies identically to the
  records the scope admits.
- **The reader stays read-only and the store stays write-only-from-the-
  pipeline.** The reader grows one read method
  (`list_active_for_session_owner`); it grows no write surface. `MemoryStore`
  is not widened.

## Consequences

- The three producers of the memory contract read as one consistent story:
  `MemoryStore` = storage, `MemoryReader` = eligibility/scope, retriever =
  ranking. Scope resolution happens in the eligibility layer, exactly where
  ADR-009 put the original session filter.
- A user who states a preference in one session and returns in another now has
  it recalled, which is what ADR-009 recorded as the price of calling the
  system a "personal AI". Supersession still governs conflicts: a superseded
  claim is a `SUPERSEDED` record and is not recalled; the promotion gate still
  holds true contradictions at write time.
- Two active, non-superseding memories that merely differ across sessions are
  both eligible and are surfaced in deterministic order (most recently updated
  first). That is the honest surface of a memory system whose write side
  already decided both were worth keeping.
- `MemoryScope` members each have a live producer in `src/`: `SESSION` is the
  default at every production query site, and `USER` is reached by the
  retriever's scope dispatch. No dead members are declared.
- No new `RetrievalMethod` or `ExclusionReason` member is added; the enum
  producer guard is unaffected.