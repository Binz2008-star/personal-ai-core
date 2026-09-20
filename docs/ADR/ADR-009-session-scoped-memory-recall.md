# ADR-009 — Phase 4 memory recall is session-scoped

**Status:** Accepted · Phase 4

## Context

Phase 3 built the write side of memory: experiences are extracted into candidates, a
promotion gate decides, and `ExperiencePipeline` writes `MemoryRecord`s. Every record
carries a `session_id` and a `MemoryProvenance` whose `session_id` must agree with it.

Phase 4 adds the read side — recall — so that context assembly can consult what the
system already knew about the user alongside what retrieval found for the current
question.

The question that had to be settled: **whose memories may a turn recall?**

Two answers were available.

**Session-scoped.** A turn recalls only memories written during that same session.
`MemoryQuery.session_id` is a hard filter applied inside `MemoryReader`, so no caller
can widen it.

**Personal (cross-session).** A turn recalls every memory belonging to the same *user*,
across all their sessions. This requires resolving a session to its user and then to
that user's other sessions — a join the memory subsystem does not currently have, since
`MemoryRecord` links to a session, not to a user.

## Decision

**Phase 4 recall is session-scoped.** Cross-session recall is deferred.

`MemoryQuery.session_id` is required and is enforced as a hard filter at the reader
boundary (`MemoryReader.list_active_for_session`) rather than left to each retriever to
remember.

## Consequences

This is the limitation worth stating plainly, because the project's name invites the
opposite assumption:

> What Phase 4 ships is **session memory**, not **personal memory**.

A user who states a preference in one session and returns in another will not have it
recalled. Within a session, recall works as intended.

That gap is deliberate and bounded, not an oversight. Recording it as an ADR rather than
a docstring is the point: a limitation that lives only next to the code that implements
it is a limitation nobody finds before they rely on its absence.

**Before the system is described as a "personal AI" with memory, this must be lifted.**
Doing so needs:

1. A link from `MemoryRecord` to a user identity, or a resolvable path from `session_id`
   to `user_id` via `SessionRepository`.
2. A widening of the hard filter from one session to that user's session set, still
   enforced at the reader boundary.
3. A decision about cross-session conflict: two sessions can hold contradicting
   preferences, and the Phase 3 promotion gate's conflict detection is currently scoped
   to a single session as well.

Point 3 is the reason this was not simply widened now. Cross-session recall without
cross-session conflict handling would surface contradictory memories side by side with no
way to tell which is current — which is worse than not recalling them at all.

## Alternatives considered

**Widen the filter now, defer conflict handling.** Rejected: it produces exactly the
failure mode above, where a stale preference and a current one are presented as equally
true. ADR-003's reasoning applies — a system that presents an unresolved contradiction as
settled fact is worse than one that admits it knows less.

**Make the scope configurable per query.** Rejected as premature. A configuration flag
whose second setting has no correct implementation behind it is not a choice, it is a
trap.
