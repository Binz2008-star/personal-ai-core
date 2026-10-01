# Architecture

Written against the code at `d2fd7d0` (2026-10-01). Every statement below carries one of
three labels:

- **[CURRENT]** what the code does today
- **[TARGET]** what the architecture requires and the code does not yet do
- **[OPEN]** a decision the owner has not made

A **[TARGET]** or **[OPEN]** line authorizes nothing. Building any of them needs the owner's
explicit authorization.

## 1. Purpose and authority

Personal AI Core is an operating layer over a replaceable local model. This document is the
one current description of its architecture.

**Authority rule.**

- This document defines the current architectural model.
- Code defines implementation reality.
- Tests define executable guarantees.
- ADRs preserve historical decisions and their reasons.
- Where an ADR conflicts with this document, the conflict is listed in section 11 until it
  is resolved. It is not silently treated as current architecture.

`PROJECT_STATE.md` remains the merge ledger. The other files under `docs/` are history or
design background, not current architecture.

## 2. Architecture in one diagram

```text
 EVIDENCE SOURCES            RUNTIME INPUTS                 MEASUREMENT
 events · messages ·         git defaults · env · options   cases · scorer ·
 feedback                    identity rules · profile       harness · results
        │                    knowledge files · model               │
        │                    agent workspace                       │
        ▼                           │                              │
   derivation ──► candidate ──► gate ──► GOVERNED STATE            │
   (rules; observations)                 memory                    │
        ▲                                   │                      │
        │                                   ▼                      ▼
        └────── new evidence ◄──── turn · agent run        evaluation run
```

- **[CURRENT]** A turn reads runtime inputs and, when recall is wired, memory. It records
  events and messages.
- **[CURRENT]** Memory changes only through derivation, candidate and gate. That path is
  built and has no production caller.
- **[CURRENT]** Measurement sits beside the loop. It runs the same builders a turn uses and
  does not feed memory.

## 3. Evidence, state and runtime boundaries

| Category | What is in it | Where it lives |
|---|---|---|
| Evidence sources | events, messages, feedback | the conversation database |
| Governed state | memory | the `memories` table |
| Runtime inputs | defaults in git, environment variables, per-call options, identity rules, owner profile, knowledge files, model weights and server, agent workspace | git, the process environment, the filesystem, the model server |
| Measurement infrastructure | evaluation cases, scorer, harness, result files | git |

- **[CURRENT]** Memory is the only state with a gate in code.
- **[CURRENT]** The conversation database is SQLite in `pac`. A PostgreSQL backend and its
  builder exist (ADR-016); nothing in `src/` calls the builder. With `--ephemeral` nothing
  is stored. Of the durable backends, only SQLite stores feedback.
- **[CURRENT]** The knowledge corpus is not durable. Files are re-read on every run and the
  indexes live in process memory.
- **[CURRENT]** The owner profile is a file, with an optional projects file beside it. Both
  are composed into every turn and have no version, no gate and no event.
- **[CURRENT]** Deployment shape: one user, one process, one writer, synchronous contracts.
- **[OPEN]** Whether the owner profile is governed state (OD-5).
- **[OPEN]** Whether configuration overrides from the environment and per-call options are
  acceptable without review (OD-9).

## 4. Dependency direction

- **[CURRENT]** Every package imports `core` and nothing above it: `runtime`, `persistence`,
  `conversation`, `knowledge`, `context`, `memory`, `learning`, `identity`, `agent`.
- **[CURRENT]** `app` may import `core` and `conversation` only.
- **[CURRENT]** `conversation/factory.py` is the one composition root and the only module
  that names concrete adapters.
- **[CURRENT]** No model name appears outside `core/config.py`.
- **[CURRENT]** No Ollama endpoint or client import appears outside `runtime/ollama/`. The
  host setting lives in `core/config.py`, and the registry names the provider as a string.

## 5. Memory and event semantics

- **[CURRENT]** A conversation turn does not directly write persistent memory.
  `ConversationService` has no memory store. The only calls to `MemoryStore.write` in `src/`
  are in `memory/pipeline.py`.
- **[CURRENT]** The write path is `ExtractionRule` → `MemoryCandidate` → `PromotionGate` →
  `ExperiencePipeline` → `MemoryStore`. Nothing in `src/` builds an `ExperienceRecord` or
  constructs the pipeline.
- **[CURRENT]** The read path is `MemoryReader` → `SimpleMemoryRetriever` → `ContextBuilder`.
  A turn recalls its own session's active memories (ADR-009). User scope exists
  (ADR-014, ADR-015) and no caller selects it.
- **[CURRENT]** `SealedMemoryStore` exists and refuses every operation. Nothing in `src/`
  references it outside its own definition.
- **[CURRENT]** Nothing separates memory events from conversation events in storage. A
  pipeline given the same database writes to the same `events` table; the two are told
  apart by type and actor.
- **[CURRENT]** A held conflict writes an event and nothing else. `supersede` has no caller.
- **[TARGET]** The permitted sources of a memory candidate are stated: which evidence, and
  whose text.
- **[OPEN]** The role of `SealedMemoryStore` on the conversation path (OD-1).
- **[OPEN]** Whether the store is the truth about memory or memory must be derivable from
  evidence (OD-2).
- **[OPEN]** Whether assistant or agent text may ever be a candidate source (OD-7).

## 6. Gates and governance

- **[CURRENT]** `DefaultPromotionGate` decides promoted, rejected or held. It is pure. The
  pipeline acts on the decision.
- **[CURRENT]** `MemoryStore.write` accepts any record. The sole-writer guarantee is a test
  that matches call patterns in source.
- **[CURRENT]** Code, identity rules, defaults, evaluation cases and the scorer change
  through pull requests in git. Whether review is required is not enforced by the
  repository.
- **[CURRENT]** Nothing in the system adapts its own behaviour automatically.
- **[TARGET]** A memory write is legitimate only as the result of a gate decision.
- **[TARGET]** Every gate decision is recorded with the identity of the rule that made it,
  including a decision that proposed nothing.
- **[OPEN]** Whether approving a function once is enough or every behavioural change must
  pass a gate (OD-4).

## 7. Identity, provenance and replay

- **[CURRENT]** Events and messages have stable ids and a stored order. Chunk ids are minted
  per run. A memory id is random at creation and stable once stored. Neither can be
  reproduced by replay.
- **[CURRENT]** A `MemoryRecord` carries provenance, and its session must match its
  provenance's session.
- **[CURRENT]** Retrieval results carry source, version, character range, method and rank.
- **[CURRENT]** A turn records the model name, sampling and output limit. It does not record
  the profile, the corpus, the code version or the model server's state.
- **[CURRENT]** Decision replay works in two places: scoring a stored evaluation run, and
  deriving observations from stored events and feedback.
- **[CURRENT]** Decision replay does not work for promotion, context assembly or agent
  policy decisions.
- **[CURRENT]** The language guard records the counts and the reason its verdict was
  computed on. The rejected draft itself is discarded.
- **[CURRENT]** Generation replay is impossible. No seed is sent, the weights are not
  pinned, and the prompt is rebuilt from inputs that can change. It is not a goal.
- **[TARGET]** Whatever an event cites keeps its identity across restarts.
- **[TARGET]** Decision replay is the only replay this architecture claims.

## 8. Evaluation and model runtime

- **[CURRENT]** The Boss model is `huihui_ai/qwen2.5-abliterate:7b`. It is operationally
  primary and architecturally replaceable (ADR-002). Its open-response behaviour is a
  requirement to preserve (ADR-002 owner note).
- **[CURRENT]** The model is reached through `ModelProvider`. `pac` uses Ollama. A llama.cpp
  adapter exists for the evaluation harness only.
- **[CURRENT]** The harness scores the contract with mechanical checks and no judge model.
  Raw replies and verdicts are committed unmodified. Each result names the commit, the
  cases version and the scorer version.
- **[CURRENT]** Three case files exist: `contract_v0` (kept for comparison), `contract_v1`
  (language and contract) and `refusal_v1` (open-response). A `refusal_v1` baseline is
  committed: two runs at `c8738e9`, no refusals, one case scored REVIEW in one run.
  `evals/README.md` asks for at least three runs.
- **[CURRENT]** The harness refuses to run under any model name but the Boss model's.
- **[CURRENT]** An evaluation run records the weights' digest (`weights`, or
  `weights_unverified` when no digest can be confirmed) and the adapter that served it
  (`provider`, read from the turns' `GENERATION_REQUESTED`). Ollama's digest is the
  manifest's; llama.cpp's is the GGUF file's SHA-256 or its Ollama blob name (ADR-020 §3.1,
  §3.2, unit 1). A `pac` turn records the provider, not the digest.
- **[TARGET]** Each reply and each evaluation run names the weights and the runtime that
  produced it.
- **[TARGET]** A change to the instrument is never approved by the result it produces.
- **[TARGET]** A candidate is evaluated under its own name and digest, without changing
  what `pac` runs. It is judged per case over several runs, against a baseline taken with
  the same cases version, scorer version and settings (ADR-020 §3.3 to §3.6).
- **[DECIDED]** For models, a passed ADR-020 gate is required before adoption (ADR-020
  D4, 2026-10-01). Whether other evaluation results block anything stays open (OD-3).
- **[DECIDED]** How a candidate model is named and admitted (OD-8): ADR-020, D1-D4
  approved 2026-10-01. Not built yet; the TARGET lines above stay TARGET until it is.

## 9. Feedback, erasure and forget semantics

Feedback:

- **[CURRENT]** A judgement is stored as a `FEEDBACK_RECORDED` event. Its key is session,
  source event, outcome and actor, so a repeated outcome or a second correction on the same
  reply is dropped. `pac --feedback` can judge only a session's latest reply;
  `FeedbackRecorder` accepts any source event in the session.
- **[CURRENT]** `derive_observations` reads feedback and stops at a read-only display.
- **[TARGET]** Feedback on a reply is an ordered history. The last judgement is effective.
  Duplicate protection covers a retried submission, not an outcome value.

Erasure:

- **[CURRENT]** A memory that is not active is not recalled.
- **[CURRENT]** A memory can be superseded only by a replacement. There is no way to retire
  one outright.
- **[CURRENT]** No repository contract can delete an event or a message. Secret redaction
  applies when text is rendered, not when it is stored.
- **[CURRENT]** Evaluation cases are synthetic. No personal text is in a committed result.
- **[TARGET]** Five operations are distinct and each is named: forget a memory, suppress its
  recall, erase source evidence, erase what was derived from it, and handle copies held for
  evaluation.
- **[OPEN]** Whether source evidence may be deleted (OD-6).

## 10. Hard invariants

These are the owner's invariants. Where the code does not yet match one, the gap is stated.

1. **Event != Memory.** A conversation turn never directly writes persistent memory.
   Checked by `test_event_not_memory.py` and the sole-writer scan in
   `test_experience_pipeline.py`.
2. **Dependency direction.** `memory → core`, `knowledge → core`, `context → core`. No layer
   imports a layer above it. Checked by `test_dependency_direction.py`.
3. **Boss model.** `huihui_ai/qwen2.5-abliterate:7b`, not silently replaced. Pinned by
   `test_config.py`.
4. **No dead enum members.** Every `RetrievalMethod` and `ExclusionReason` member has a
   producer in `src/`. Checked by `test_enum_producer_guard.py`.
5. **`SealedMemoryStore`** remains on the conversation path until the owner authorizes
   otherwise. The code does not place it on any path today. See OD-1.
6. **Database boundary.** No Neon, pgvector, schema or migration change without the owner's
   explicit authorization.
7. **Legacy repositories are immutable.** Rico, unified-llm-local, second-brain-kb and
   rag-engine are reference only.

## 11. Known gaps and bypasses

Bypasses:

- `MemoryStore.write` takes any record, and three slice objects hand the store to callers.
- In grounded builds the memory reader wraps the real, write-capable store.
- `pac --remember` changes every later prompt with no gate and no event.
- Environment variables and `send(options=...)` change behaviour without review.

Gaps:

- The promotion pipeline is unfed. `repetition_count`, user-scope recall and `supersede`
  have no producer or caller.
- Conflict detection compares the first twelve characters of whole messages.
- `GENERATION_FAILED` and `RETRIEVAL_FAILED` store the raw exception text.
- Agent runs store tool names and outcomes, not tasks, arguments or answers.
- Append-only storage is enforced by the repository API, not by the database.

Marked conflicts with ADRs:

| ADR | Conflict |
|---|---|
| ADR-003 | "distinct storage" and "written only by the promotion gate": events share one table, and the pipeline writes |
| ADR-002 | "name appears only in `ModelRegistry`": it is in `core/config.py` |
| ADR-010–013, 017–019 | status PROPOSED while the code is in use (OD-10) |

## 12. Open decisions

| # | Decision |
|---|---|
| OD-1 | The role of `SealedMemoryStore` on the conversation path |
| OD-2 | Memory authority: the store, or derivable from evidence |
| OD-3 | Whether evaluation gates anything, and who approves changes to the instrument |
| OD-4 | Gate a function once, or gate every behavioural change |
| OD-5 | Whether the owner profile is governed state, and its gate |
| OD-6 | Whether source evidence may be deleted |
| OD-7 | Whether assistant or agent text may be a memory candidate source |
| OD-8 | How a candidate model is named and admitted. Decided: ADR-020 (D1-D4 approved 2026-10-01) |
| OD-9 | Whether ungated configuration overrides are acceptable |
| OD-10 | Accept the built-but-proposed ADRs, or let this document supersede them |
