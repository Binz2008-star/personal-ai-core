PROJECT STATE
=============

CURRENT ACCEPTED STATE (REMOTE)
-------------------------------
Accepted phase: Phase 4 (ACCEPTED / MERGED — PR #2, implementation e8062ff2fa8b6eb5a4471ac8475f29bed76fd369, merge bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
Main branch head: 7aa7b18fa3f4316fea4ab457ce6805b3e0097e12 (short: 7aa7b18 — Merge pull request #27)
  The accepted PHASE is still Phase 4. PRs #3-#27 are correction, hardening
  and design work on top of it, not a new phase: see POST-PHASE-4 MERGES.
Phase 2 accepted commit: 0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511
Phase 3 accepted merge: f090100e933d1a6ff18d6e546b384b2e727b2889 (PR #1)
Phase 4 implementation: e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 (PR #2, branch claude/phase-4-memory-aware-context)

Test verification (remote):
- Phase 2 baseline: 389 passed / 14 skipped
- Phase 3: 443 passed / 14 skipped
- Phase 4: 494 passed / 14 skipped
- Current main (7aa7b18): 541 passed / 14 skipped (Linux CI and Windows)
- ruff: 0 errors  |  pyright: 0 errors

Synchronization: origin/main is at 7aa7b18 (Phase 4 merged, plus PRs #3-#27)

PHASE STATUS SUMMARY
--------------------
Phase 0 — HISTORICAL / COMPLETED
  Scope: Core source audit, evidence freeze, component extraction matrix
  Note: Historical foundation; no separate acceptance gate.
        Evidence: docs/COMPONENT_EXTRACTION_MATRIX.md

Phase 1 — PARTIAL / NOT COMPLETE
  Scope: Core foundation vertical slice —
         User → Session → Message → ModelProvider → Response → Event
  Status: docs/PHASE_1_RECONCILIATION.md states that ONE of the five playbook
          components defined for Phase 1 is unbuilt -- identity. It was two
          until PR #23; the memory foundation was built in Phase 3 and the
          reconciliation had gone on saying otherwise. Phase 1 is therefore
          still NOT complete, and must not be summarised as completed.
          Verified at 7aa7b18: src/personal_ai_core/identity/ does not exist,
          and ResponsePolicy / BehavioralContract have 0 occurrences in src/.
          Contract design: ADR-011 — PROPOSED, not accepted. It fixes the shape
          of the contract and authorises no implementation, so Phase 1 is not
          advanced by it.
  Evidence: docs/PHASE_1_VERTICAL_SLICE.md, docs/PHASE_1_RECONCILIATION.md

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
  Subsequent corrections: defects in this phase's shipped code were found
         after acceptance and fixed in PRs #5, #10 and #12. Acceptance did not
         catch them; an independent review did. Recorded so the acceptance gate
         is not read as stronger than it proved to be.

Phase 5 — NOT AUTHORIZED / DESIGN NOT STARTED
  Note: UNAUTHORIZED / FUTURE DESIGN — no contract, no implementation, no cross-session recall approval
  Not gated by persistence. ADR-010 (PROPOSED, PR #15) records that
         cross-session memory semantics and durable persistence are separate
         concerns.

         Cross-session memory SEMANTICS can be designed and contract-validated
         against the existing in-memory repositories. Recall in Phase 4 REMAINS
         SESSION-SCOPED (ADR-009): no cross-session recall implementation
         exists and none is authorized. What scopes recall today is the session
         filter in MemoryReader, not the absence of durability -- which is a
         statement about where the constraint lives, not about a capability
         being available.

         This is a limit on what persistence can be claimed to block, not a
         widening of what Phase 5 is permitted to be. An earlier draft of
         ADR-010 claimed persistence gated Phase 5; that claim was corrected
         before merge. See also CURRENT LIMITATIONS and BLOCKED WORK, which
         state the same thing from the feature side and remain accurate.

PERSISTENCE — DESIGN PROPOSED, NOT SELECTED
  ADR-010 (docs/ADR/ADR-010-persistence-model.md, PR #15) compares four
  options and recommends a hybrid with SQLite, recording why an append-only
  log remains defensible. NOTHING IS SELECTED. No schema, no migration, no
  dependency, no Neon, no pgvector. Choosing an option authorizes nothing
  about Phase 5, and authorizing Phase 5 would not select an option.
  Prerequisite for a durable backend only: Event.payload is Mapping[str, Any]
  with no serialisation contract.

POST-PHASE-4 MERGES
-------------------
None of these is a phase. Phase 4 remains the accepted phase. This section
exists because the record previously stopped at PR #2 while main advanced by
thirteen merges -- the same defect several of these PRs found elsewhere: a
claim written in one place with nothing that notices it going stale.

  #3  6171cdc  docs: sync PROJECT_STATE after Phase 4. Landed unreviewed and
               carried factual errors; corrected by #4. This file is itself an
               instance of the defect it now records.
  #4  4e3074b  docs: correct the errors introduced by #3
  #5  ca9f7ab  fix(memory): stop recall failures escaping the turn
               FOUR DEFECTS ON MAIN, each reproduced before being fixed. One
               let a credential-shaped exception message escape the turn --
               the exact failure MemoryRetrievalError exists to prevent.
  #6  657fc5e  docs: ARCHITECTURE.md marked as target, not built state.
               Established that identity/, agent/, learning/, evaluation/,
               projects/, api/, ui/ do not exist.
  #7  c2cca65  test: non-URL sentinel for the leak-detection canary
  #8  7b22345  test: path-separator bug in the skip-accounting gate.
               The fork-bomb canary could never fire on Windows.
  #9  5d67e71  test: the contract-purity proof now runs the real scanner.
               It had been asserting against its own reimplementation.
  #10 dc693d9  chore: clear the static-analysis backlog -- 13 findings, 2 real
  #11 034284b  ci: gate lint and types, not just tests. CI had run pytest
               alone; ruff and pyright findings had accumulated unread for the
               life of the repository.
  #12 9341e6e  feat(packaging): Phase 4 implementations on their package
               surface, + tests/unit/test_package_surface.py
  #13 cd40871  test: two guards now cover what they claim. The dependency
               guard skipped any package with no layer rule -- silently, for
               every future package -- and MemoryStore conformance had only
               ever been checked against the sealed decoy.
  #14 5dcf3ce  docs: deployment shape recorded as a constraint --
               one user, one process, local machine.
  #15 9922160  docs(adr): ADR-010 persistence model, PROPOSED not accepted.
  #16 15ba882  docs: this ledger. Written because the record had stopped at
               PR #2 while main advanced thirteen merges.
  #17 0af4a94  test: the ledger is now checked against `git log --merges`
               (tests/unit/test_merge_ledger.py). Writing the list down did
               not fix the defect; only something that rereads git does.
               Requires fetch-depth: 0, since CI's default shallow clone shows
               no merges and the comparison would pass having compared nothing.
  #18 9f9cc20  docs: record #16 and #17 in the merge ledger
  #19 546238e  test: compare ledger SHAs exactly, not by seven characters
  #20 812f290  test: make the sole-writer guard separator-independent
  #21 0b0e1e5  docs: record #18 #19 and #20 in the merge ledger
  #22 bb2e953  docs: note cold-start request timeout and the env knob
  #23 350d783  docs: correct the Phase 1 reconciliation to current reality
  #24 8fe4bc9  docs(adr): ADR-011 identity layer contract, PROPOSED
  #25 1b42368  docs: PROJECT_STATE current; Phase 1 read COMPLETED in the
               gates list and NOT COMPLETE in the summary -- Phase 0 had no
               row and its description had drifted onto the Phase 1 line
  #26 761accc  docs: record #24 and #25, and stop asserting merge state
  #27 7aa7b18  docs(adr): ADR-011 rewritten clean; Q1-Q4 design questions
               resolved; PHASE_1_RECONCILIATION superseded. Still PROPOSED,
               authorises no implementation

Pattern worth recording: #8, #9, #11, #12 and #13 are one defect class -- a
claim written down with nothing checking it, so a guard had quietly stopped
guarding. Each fix added the missing check and proved it non-vacuous by
mutation.

Governance note: all thirteen merged without an independent review being
required on main, because no such requirement exists. #13 and #15 did receive
substantive owner review before merge, and #15's review caught a factual error
in the ADR. A proposal to require the `suite` and `static` checks plus review
is PENDING OWNER DECISION and was deliberately not acted on -- branch
protection is the owner's to change, not the agent's.

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
   Enforced by test_enum_producer_guard.py.
   No mutation testing is configured in this project, so no mutation score
   is claimed here.

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
  → ollama/ (provider adapter), model_registry.py
  → Boss model configuration only
Conversation
  → sessions, messages, events
  → optional grounding (ContextBuilder injected)
  → EventRecorder only — never writes memory
Knowledge
  → catalog, chunking, embedding, fusion, ingestion, language,
    lexical_index, retrieval, text, vector_index
  → all in-memory implementations behind core.contracts protocols
  → HashingEmbeddingProvider, InMemoryVectorIndex, InMemoryLexicalIndex, HybridRetriever, RRF
Context
  → assembler, budget, token_estimator
  → ReserveBasedBudgetPolicy, GreedyContextAssembler, ScriptAwareTokenEstimator
  → budget derives from active model in registry (ADR-005)

Phase 3 (implemented & merged, f44de80 + ac41d27):
Memory
  → domain types (MemoryRecord, MemoryProvenance, MemoryStatus, MemoryType,
    ExperienceRecord, MemoryCandidate, PromotionDecision, PromotionOutcome)
  → extraction rules (memory/rules.py) propose MemoryCandidate from an
    ExperienceRecord — NOT from the event stream
  → DefaultPromotionGate (pure decision: promoted / rejected / held)
  → ExperiencePipeline — the SOLE writer to MemoryStore; it materialises a
    MemoryRecord from the gate's decision and emits MEMORY_* events
  → MemoryStore protocol + InMemoryMemoryRepository implementation
  → Event != Memory enforced: SealedMemoryStore remains on conversation path

Phase 4 (implemented, e8062ff, PR #2):
Memory Read/Recall
  → MemoryReader — a NOMINAL CLASS in core/memory.py, deliberately NOT a
    Protocol. A Protocol would be structurally satisfied by
    SealedMemoryStore (same method names, raises on both), which would make
    the boundary a convention rather than a type. Guarded by
    test_memory_reader_is_a_class_not_a_protocol.
    It exposes only read() and list_active_for_session(); no write, no
    supersede. The session filter lives inside the read contract.
  → SimpleMemoryRetriever (memory/retriever.py) — ranks memories for one
    turn. It does NOT build hybrid context.
  → HybridContextAssembler (context/assembler.py) — merges documents and
    memories under ONE shared token budget, neither source privileged.
    Document rank is the retriever's 1-based output position, not
    provenance.ranks (per-arm, pre-fusion) and not fused_score.
  → session-scoped recall (ADR-009)
  → Grounding integration: memory_enabled is CONFIGURATION, not outcome —
    true whenever a retriever is wired, including when recall returns
    nothing or fails
  → degraded failure behaviour: MemoryRetrievalError is a stable
    classification (unavailable / invalid_query / internal); the turn
    continues with documents only and never raises. Raw exception text
    never reaches the event payload.

Explicitly distinguished:
- Event → conversation/event path (append-only evidence)
- Memory → persistent memory path (promotion gate only, write path)
- Memory recall → context enrichment path (session-scoped, read-only enrichment)

Do NOT imply that conversation turns directly write memory.

CURRENT LIMITATIONS (ACCEPTED)
------------------------------
- Phase 4 recall is session-scoped (ADR-009) — no cross-session recall.
  Unchanged by ADR-010: that a contract could be validated in-process says
  nothing about a capability existing. None does.
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

Phase 0: HISTORICAL / COMPLETED (evidence freeze ec04071; no separate gate)
Phase 1: PARTIAL / NOT COMPLETE — identity unbuilt; see PHASE_1_RECONCILIATION.md
         and the PHASE STATUS SUMMARY above, which this line used to contradict.
Phase 2: ACCEPTED (0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511)
Phase 3: ACCEPTED / MERGED (f090100e933d1a6ff18d6e546b384b2e727b2889)
Phase 4: ACCEPTED / MERGED (PR #2 — e8062ff2fa8b6eb5a4471ac8475f29bed76fd369 → bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a)
Phase 5: NOT AUTHORIZED / DESIGN NOT STARTED — UNAUTHORIZED / FUTURE DESIGN
Post-Phase-4: PRs #3-#27, merged, no new phase (head 7aa7b18) — see POST-PHASE-4 MERGES
Persistence: ADR-010 PROPOSED, no option selected (PR #15)
Identity: ADR-011 PROPOSED, not accepted — contract design only, nothing built (PRs #24, #27)

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
- cross-session memory recall (deferred with ADR-009) — still blocked, and not
  unblocked by ADR-010, which addresses persistence only

Important:
Do NOT imply that real embedding adaptation or BM25/stemming is a prerequisite for Phase 3/4 Memory Foundation.
Those belong to later explicitly authorized work unless separately authorized.

OPEN ITEMS
==========

1. ScriptAwareTokenEstimator calibration remains unverified where tokenizer infrastructure is unavailable.

Do not turn these open items into unauthorized implementation.

DOCUMENTATION DISCIPLINE
========================

The ADRs in docs/ADR/ are the decision record. That directory is authoritative;
this file does not enumerate them, because an enumeration here is a second source of
truth that goes stale the moment one is added.

Not every ADR is accepted. PROPOSED, not accepted:
- ADR-010 (persistence) — recommends without selecting; nothing is selected.
- ADR-011 (identity layer contract) — fixes the shape of the contract;
  authorises no implementation.

Record what is PROPOSED versus accepted, never whether a PR has merged. The
previous wording said ADR-011 was "NOT merged (PR #24)", which was true when it
was written and false one merge later -- a claim whose truth depended on merge
order, written into the very change that de-enumerated this list so it could not
go stale. Whether a file is in docs/ADR/ is git's business; whether a decision is
accepted is this file's.
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
- Test-count discrepancy on Windows (path-separator bug in skip accounting): FIXED -- the two
  substring matchers in tests/unit/test_expected_skips.py now normalize the separator, and four
  tests drive them with both platforms' line shapes so Linux-only CI holds the Windows behaviour.
- ScriptAwareTokenEstimator calibration: unverified without tokenizer infrastructure
- Event.payload has no serialisation contract (Mapping[str, Any]; an event holding a
  non-serialisable value constructs successfully and fails json.dumps). Blocks a durable
  EventRepository; does not block a cross-session memory contract. See ADR-010.
- CI does not require the `suite` and `static` checks to pass before merge, and no
  independent review is required on main. Both are owner decisions, PENDING.
- This ledger is enforced: tests/unit/test_merge_ledger.py (PR #17) compares it to
  `git log --merges` and fails on a wrong SHA, an invented PR, or a lag of more than
  three merges. A PR cannot record its own merge, so a lag of one or two is normal.

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
