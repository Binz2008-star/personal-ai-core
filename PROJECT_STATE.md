PROJECT STATE
=============

CURRENT ACCEPTED STATE (REMOTE)
-------------------------------
Accepted phase: Phase 4 (ACCEPTED / MERGED — PR #2, implementation e8062ff2fa8b6eb5a4471ac8475f29bed76fd369, merge bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
Main branch head: bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a (short: bbbf4c3 — Merge pull request #2 — PR #2 MERGED)
Phase 2 accepted commit: 0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511
Phase 3 accepted merge: f090100e933d1a6ff18d6e546b384b2e727b2889 (PR #1)
Phase 4 implementation: e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 (PR #2, branch claude/phase-4-memory-aware-context)

Test verification (remote):
- Phase 2 baseline: 389 passed / 14 skipped
- Phase 3: 443 passed / 14 skipped
- Phase 4: 494 passed / 14 skipped
- pyright: 0 errors

Local worktree: governance commit 80f0a8a (ahead of origin/main by 1)
Synchronization: origin/main is at bbbf4c3 (Phase 4 merged)

PHASE STATUS SUMMARY
--------------------
Phase 1 — HISTORICAL / COMPLETED
  Scope: Core source audit, evidence freeze, component extraction matrix (Phase 0)
  Note: Historical foundation; no separate acceptance gate — see docs/PHASE_1_RECONCILIATION.md and ec04071

Phase 2 — ACCEPTED
  Commit: 0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511 (short: 0a8d798)
  Scope: Knowledge & context foundations (contracts, in-memory implementations, retrieval, budgeting)
  Verification: 389 passed / 14 skipped (historical)

Phase 3 — ACCEPTED / MERGED (PR #1)
  Implementation: f44de80a5a31e079dbdef4ab7f173d40bd3bc15e (short: f44de80)
  Correction: ac41d2799c4698960f63c00d024010d1e45f1b18 (short: ac41d27)
  Merge: f090100e933d1a6ff18d6e546b384b2e727b2889 (short: f090100)
  Scope: Memory domain + promotion + persistence contracts + write path
  Verification: 443 passed / 14 skipped

Phase 4 — ACCEPTED / MERGED (PR #2)
  Implementation: e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 (short: e8062ff)
  Branch: claude/phase-4-memory-aware-context
  PR: #2 — MERGED (merge commit bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
  Scope: session-scoped MemoryReader, deterministic memory retrieval,
         MemoryRetrievalError classification, hybrid document/memory context,
         shared token budget, Grounding integration, memory_enabled semantics,
         degraded memory retrieval failure behavior, ADR-009 session-scoped memory recall,
         Phase 4 regression/invariant tests
  Verification: 494 passed / 14 skipped, pyright: 0 errors
  Architectural review: PASS
  Post-commit audit: PASS
  Push verification: PASS

Phase 5 — NOT AUTHORIZED / DESIGN NOT STARTED
  Note: UNAUTHORIZED / FUTURE DESIGN — no contract, no implementation, no cross-session recall approval

HARD ARCHITECTURAL INVARIANTS
=============================

1. Event != Memory.
   A conversation turn never writes directly to persistent memory.
   Enforced by SealedMemoryStore + test_event_not_memory.py.

2. Dependency direction:
   memory → core
   knowledge → core
   context → core
   conversation → core
   No layer imports a layer above it.
   Enforced by test_internal_layering_is_respected.

3. Boss model:
   huihui_ai/qwen2.5-abliterate:7b
   Appear only in config.py (DEFAULT_BOSS_MODEL).
   Never in business logic — enforced by test_no_model_name_literal_in_business_logic.
   A Boss-model change requires explicit owner authorization.

4. No dead RetrievalMethod or ExclusionReason members.
   Every declared member must have a producer in src/.
   Enforced by test_enum_producer_guard.py (17 mutations killed, 0 survived).

5. SealedMemoryStore remains sealed until explicit authorization.
   Refuses all write attempts with InvariantViolation("Event != Memory").

6. No Neon / pgvector / Postgres / migration / schema changes without explicit owner authorization.

7. Source repositories are read-only and must never be modified:
   - Rico: source of application/conversation behavior, NOT a dependency
   - unified-llm-local: source of retrieval/memory/chunking behavior and characterization, NOT a dependency
   - second-brain-kb: source of knowledge/persistence/graph behavior and characterization, NOT a dependency
   - Any other legacy/source repository: read-only

SOURCE REPOSITORY MAP
=====================

Rico
→ source of application/conversation behavior
→ NOT a dependency

unified-llm-local
→ source of retrieval/memory/chunking behavior and characterization
→ NOT a dependency

second-brain-kb
→ source of knowledge/persistence/graph behavior and characterization
→ NOT a dependency

Neon
→ brownfield persistence substrate
→ studied read-only
→ NOT currently used by Core
→ no pgvector/migration work authorized

Architecture extraction rule:
Extract → Characterize → Contract → Implement → Verify → Adapt

NOT:
Copy → Glue → Hope

CURRENT ARCHITECTURE
====================

Phase 2 (implemented, 0a8d798):
Core
  → contracts, schemas, errors, config, lifecycle
  → hard invariants enforced by tests
Runtime
  → ollama/, model_registry/, inference/, generation/
  → Boss model configuration only
Conversation
  → sessions, messages, events
  → optional grounding (ContextBuilder injected)
  → EventRecorder only — never writes memory
Knowledge
  → ingestion, parsers, chunking, embeddings, indexing, retrieval, reranking, provenance
  → all in-memory implementations behind core.contracts protocols
  → HashingEmbeddingProvider, InMemoryVectorIndex, InMemoryLexicalIndex, HybridRetriever, RRF
Context
  → retrieval, ranking, compression, budget
  → ReserveBasedBudgetPolicy, GreedyContextAssembler, ScriptAwareTokenEstimator
  → budget derives from active model in registry (ADR-005)

Phase 3 (implemented & merged, f44de80 + ac41d27):
Memory
  → domain types (MemoryRecord, MemoryProvenance, Experience, Candidate, PromotionDecision)
  → ExperiencePipeline (extracts candidates from events)
  → PromotionGate (decides promotion)
  → MemoryStore protocol + InMemoryMemoryStore implementation
  → Persistence contracts (MemoryRepository, MemoryReader)
  → Event != Memory enforced: SealedMemoryStore remains on conversation path

Phase 4 (implemented, e8062ff, PR #2):
Memory Read/Recall
  → MemoryReader protocol + InMemoryMemoryReader implementation
  → session-scoped recall (ADR-009)
  → MemoryRetriever (hybrid document/memory context)
  → shared token budget across knowledge + memory
  → Grounding integration (memory_enabled flag)
  → degraded failure behavior (MemoryRetrievalError → fallback to knowledge-only)

Explicitly distinguished:
- Event → conversation/event path (append-only evidence)
- Memory → persistent memory path (promotion gate only, write path)
- Memory recall → context enrichment path (session-scoped, read-only enrichment)

Do NOT imply that conversation turns directly write memory.

CURRENT LIMITATIONS (ACCEPTED)
------------------------------
- Phase 4 recall is session-scoped (ADR-009) — no cross-session recall
- No semantic embedding-based memory ranking (Phase 2 HashingEmbeddingProvider is not semantic)
- Memory retrieval is enrichment/degradation, not a write path
- Phase 2 rendering overhead limitation remains out of scope
- No production database persistence changes introduced (in-memory only)
- Cross-session conflict handling not implemented (deferred with ADR-009)

AGENT BOOT PROTOCOL
===================

Every agent must, in order:

1. Read PROJECT_STATE.md.
2. Read relevant ADRs/phase documentation.
3. Inspect git branch, SHA and worktree.
4. Determine accepted phase.
5. Determine explicitly authorized work.
6. Read hard invariants.
7. Read blockers/open findings.
8. Only then inspect or modify implementation.

Agent rule:

If requested work is outside explicit authorization:
STOP AND REPORT.

Never implement a feature merely because it appears missing.

PHASE GATES
===========

A phase is not complete because code exists.

A phase becomes accepted only after:

Design → Authorization → Implementation → Tests → Invariant review → Git verification → Owner acceptance

Phase 1: HISTORICAL / COMPLETED (evidence freeze ec04071; reconciliation — no separate gate)
Phase 2: ACCEPTED (0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511)
Phase 3: ACCEPTED / MERGED (f090100e933d1a6ff18d6e546b384b2e727b2889)
Phase 4: ACCEPTED / MERGED (PR #2 — e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 → bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
Phase 5: NOT AUTHORIZED / DESIGN NOT STARTED — UNAUTHORIZED / FUTURE DESIGN

BOSS MODEL
==========

Canonical Boss model: huihui_ai/qwen2.5-abliterate:7b

This is an architectural invariant.

Do not recommend substitutions.

Do not describe newer/different models as replacements.

A Boss-model change requires explicit owner authorization.

BLOCKED WORK
============

Explicitly recorded as BLOCKED / NOT AUTHORIZED:

- Neon changes
- pgvector changes
- database migrations
- production memory persistence (Postgres/Neon adapter)
- production embedding adapter
- BM25/stemming redesign
- Boss model replacement
- modifications to legacy/source repositories
- Phase 5 implementation
- any unapproved Phase 3/4 expansion
- cross-session memory recall (deferred with ADR-009)

Important:
Do NOT imply that real embedding adaptation or BM25/stemming is a prerequisite for Phase 3/4 Memory Foundation.
Those belong to later explicitly authorized work unless separately authorized.

OPEN ITEMS
==========

1. Resolve test-count accounting discrepancy on Windows (path-separator bug in skip accounting).
2. ScriptAwareTokenEstimator calibration remains unverified where tokenizer infrastructure is unavailable.

Do not turn these open items into unauthorized implementation.

DOCUMENTATION DISCIPLINE
========================

ADR-001 through ADR-009 are present in docs/ADR/ and serve as the decision record.
Do NOT create docs/DECISIONS.md — ADRs are the historical decision record.

Avoid duplicate sources of truth.

VALIDATION
==========

After changes:

- run the existing test suite
- verify no invariant regression
- verify Boss model unchanged (config.py DEFAULT_BOSS_MODEL = "huihui_ai/qwen2.5-abliterate:7b")
- verify no Neon/pgvector/migration changes
- verify no legacy repositories touched
- verify git diff contains ONLY governance/documentation changes (no application architecture changes)

Do NOT commit.
Do NOT push.

Gaps verified:
- Boss model test gap: no mechanical CI pin beyond code literal check (tracked)
- Test-count discrepancy: 389 vs 388 passed on Windows (path-separator bug in skip accounting)
- ScriptAwareTokenEstimator calibration: unverified without tokenizer infrastructure

TRACEABILITY
============

Phase 2:
- Commit: 0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511 (short: 0a8d798) — ACCEPTED
- Docs: docs/PHASE_2_CONTRACT.md, docs/PHASE_2_IMPLEMENTATION.md
- ADRs: 001-008 (relevant: 002, 003, 004, 005, 006)
- Verification: 389 passed / 14 skipped

Phase 3:
- Implementation: f44de80a5a31e079dbdef4ab7f173d40bd3bc15e (short: f44de80)
- Correction: ac41d2799c4698960f63c00d024010d1e45f1b18 (short: ac41d27)
- Merge: f090100e933d1a6ff18d6e546b384b2e727b2889 (short: f090100, PR #1) — ACCEPTED / MERGED
- ADRs: 003 (Event != Memory), 007 (training-built-not-extracted)
- Verification: 443 passed / 14 skipped

Phase 4:
- Implementation: e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 (short: e8062ff)
- Branch: claude/phase-4-memory-aware-context
- PR: #2 — MERGED (merge commit bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
- ADR: 009 — docs/ADR/ADR-009-session-scoped-memory-recall.md (on main at bbbf4c3; see e8062ff)
- Scope: session-scoped MemoryReader, deterministic retrieval, hybrid context, shared budget, Grounding
- Verification: 494 passed / 14 skipped, pyright: 0 errors, architectural review: PASS, post-commit audit: PASS, push verification: PASS
- Key files: src/personal_ai_core/memory/retriever.py, src/personal_ai_core/core/memory.py, src/personal_ai_core/context/assembler.py, src/personal_ai_core/conversation/grounding.py (+ tests/unit/test_memory_*.py, test_grounding*.py, test_hybrid_assembler.py)

Architecture docs:
- docs/ARCHITECTURE.md — boundaries, dependency direction, invariants (updated to Phase 4)
- docs/MEMORY_ARCHITECTURE.md — Event != Memory, promotion, provenance
- docs/MODEL_ARCHITECTURE.md — registry, Boss model huihui_ai/qwen2.5-abliterate:7b
- docs/AGENT_ARCHITECTURE.md — agent loop, tool contracts