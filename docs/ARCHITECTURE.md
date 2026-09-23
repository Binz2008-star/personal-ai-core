# Architecture

**This document describes the TARGET architecture, not the built system.** It was drafted
during the Phase 0 audit and still states where the Core is going. Most of what it
describes does not exist yet.

**For what is actually built and verified, read [`PROJECT_STATE.md`](../PROJECT_STATE.md).**
Built through Phase 4 and the post-Phase-4 PRs: `core/`, `runtime/`, `conversation/`,
`memory/`, `knowledge/`, `context/`, `persistence/` (in-memory and SQLite), `identity/`
(#39) and `app/`, which provides the `pac` entry point (#45). Not built: `agent/`, `learning/`,
`evaluation/`, `projects/`, `api/`, `ui/`. Sections below are labelled accordingly.
Aligned with the code at `74a2ae2`; where it disagrees with `PROJECT_STATE.md`, that file
wins.

Evidence for every source claim is in [`COMPONENT_EXTRACTION_MATRIX.md`](COMPONENT_EXTRACTION_MATRIX.md).
Phase 4 recall is session-scoped (ADR-009).

A previous edit set this header to "Status: Phase 4 ACCEPTED / MERGED" while leaving the
body at its Phase 0 content. That turned a design document into an apparent status report
and made every unbuilt subsystem below read as shipped. The header states the document's
genre now, because that is what was actually wrong.

---

## 1. What this system is

Personal AI Core is an **AI operating layer**, not an application with a model inside it.
The model is a replaceable backend. Memory, knowledge, experience, learning, tools and
projects are separate layers with their own contracts.

**ARCHITECTURAL DECISION.** The Core backbone is newly engineered. The audit established
that no source repository provides the event, experience, promotion, verifier or
reranking subsystems. Existing repositories supply **edge components and patterns**.

**ARCHITECTURAL DECISION — DEPLOYMENT SHAPE.** The Core runs as **one user, one process,
on the local machine**. The model is local (Ollama); nothing here is served over a
network to other people.

This is a constraint, not an observation, and it is written down because everything
below already assumed it without saying so. The tree at `cd40871` contains no `async`
or `await`, no `threading`, `asyncio` or `multiprocessing` import, and no lock or
semaphore anywhere in `src/`; every store in the tree — the repositories, the memory
store, the registry, the catalogue and both indexes — holds its state in a process-local
`dict` or `list`. A reader had no way to tell whether that was a decision or an
oversight.

What it permits, and what the persistence design may therefore rely on:

- **A single writer.** Append order is a total order, so an event log needs no sequence
  column and no sortable id. This matters because the alternatives do not work:
  `new_id()` is `uuid4`, which does not sort, and `utcnow()` collides heavily — 2000
  successive calls yielded 499–632 distinct values across repeated runs, so roughly
  three in four share a timestamp with another. Without this constraint the event log
  would have **no** total order that survives a durable store. The durable store built
  later (SQLite, #42) records order anyway, in a `seq` column, so order is stored rather
  than inferred from append order. It is still one process with one writer.
- **Synchronous contracts.** Every protocol in `core/contracts.py` is sync. Serving the
  Core over a network would make that the wrong choice, and changing it later is a
  breaking change to every contract and every caller.
- **No coordination layer.** No locks, no transactions across processes, no leader
  election, no connection pool.

What would invalidate it, and must therefore reopen the persistence decision before any
code is written against it: a second concurrent writer, a second user, or serving the
Core over a network. Any of the three, and the three bullets above stop holding
together.

This constraint does **not** by itself select a storage backend. It removes options that
only concurrency justifies. ADR-010 compares what remains and recommends one option, which
is now built and wired (#42, #44). The ADR is still **PROPOSED**: building an option does not
accept it.

## 2. System boundaries

```text
                         PERSONAL AI CORE
                                │
      ┌─────────────────────────┼─────────────────────────┐
      │                         │                         │
  Identity                   Memory                  Knowledge
      │                         │                         │
      └─────────────────────────┼─────────────────────────┘
                                │
                         Context Engine
                                │
                         Agent Runtime
                                │
                      Tool / Policy Layer
                                │
                     Learning / Evaluation
                                │
                         Model Registry
                                │
                         Ollama Runtime
                                │
                   local-llm-rig  (separate repo)
```

`local-llm-rig` is **not** part of this repository and **not a runtime dependency**. The
Core does not import from it, call into it, or require it to be present in order to run. It
owns the model runtime, Modelfiles, benchmarks and hardware evidence; the Core cites its
measurements as evidence and adapts specific harness scripts into its own `evaluation/`
tree. See `COMPONENT_EXTRACTION_MATRIX.md` §1.2.

Project-specific systems attach at the edge, never inside:

```text
                     PROJECT CONNECTORS
                             │
      ┌──────────┬───────────┼───────────┬──────────┐
      │          │           │           │          │
    Rico       Robin    Second Brain   GitHub    Future
```

## 3. Module architecture

**BUILT.** Present in `src/personal_ai_core/`, with the real submodule names:

```text
core/          contracts, domain, memory, knowledge, context, config, errors
runtime/       ollama/ (provider adapter), model_registry.py
conversation/  service, factory, grounding, events
memory/        rules, gate, pipeline (write path) · retriever (read path)
knowledge/     catalog, chunking, embedding, fusion, ingestion, language,
               lexical_index, retrieval, text, vector_index
context/       assembler, budget, token_estimator
identity/      composer, text          (contract and policy types live in core/identity)
persistence/   in_memory, memory_store, sqlite
app/           cli, __main__           (the `pac` command)
tests/
```

**DESIGN TO BUILD.** None of these exist. They are the target, not the state:

```text
agent/         planner, executor, tools, policy, verifier, state, recovery
learning/      events, feedback, experience, analysis, dataset,
               training, evaluation, promotion
evaluation/    regression, capability, memory, retrieval, agent,
               Arabic, coding, performance
projects/      registry, connectors, adapters, indexes
api/  ui/  scripts/
```

Corrections worth stating, because each was asserted in this file and copied into other
documents:

- `identity/` is **built** (#39): a behavioural contract and a response policy, composed
  into the first message of every model call. The **personality** this section used to
  list was deliberately left out. ADR-012 says "No tone or persona": a persona is not a
  rule and cannot be violated.
- `persistence/` has two backends: process memory, and SQLite (#42) for users, sessions,
  messages, events and memories. There is **no migrations framework**. `SCHEMA_VERSION`
  is 1, and a database written at a different version is refused (`SchemaVersionMismatch`)
  rather than altered. There is no Postgres. Adding migrations or Postgres requires
  explicit authorisation; see the hard invariants in `README.md`.
- `memory/` implements **four** `MemoryType` values — `preferences`, `lessons`,
  `semantic`, `episodic`. The wider taxonomy in `MEMORY_ARCHITECTURE.md` (`working`,
  `decisions`, `patterns`) is design, not code: a member is declared only once a rule
  produces it.

## 4. Dependency direction

**ARCHITECTURAL DECISION.** Dependencies point inward. The Core depends on abstractions;
concrete providers depend on the Core.

The principle is **BUILT** and enforced by `test_internal_layering_is_respected`. The
concrete adapters shown are what exists today:

```text
             OllamaProvider ──┐
    InMemoryMemoryRepository ─┤
         Sqlite*Repository ───┼──▶  core/contracts  ◀── memory, knowledge, context,
   HashingEmbeddingProvider ──┤                          conversation, identity
    DefaultIdentityComposer ──┘
```

`app/` sits outside this picture on purpose. It may import only `core` and `conversation`,
and calls the one composition root, `conversation/factory.py`. That is the only module the
layering rule exempts (`COMPOSITION_ROOTS`), which is how it may name concrete adapters.

`HashingEmbeddingProvider` captures surface overlap, not meaning — it is a development
stand-in, and `EmbeddingProvider.model_id` is what identifies whichever model actually
ranked a passage (ADR-006). A Postgres-backed store and a real embedding provider are
**DESIGN TO BUILD**; naming them here previously implied they were wired.

Three consequences, each a direct response to an audited defect:

- **No `import ollama` outside `runtime/ollama/`.** The audit found embedding calls
  hard-wired and duplicated across `api.py`, `evolve.py` and `scripts/e2e_test.py`.
- **No direct database driver use in components.** Access goes through repositories.
- **No model name in business logic.** The Boss model is registry configuration.

## 5. Runtime flow

**DESIGN TO BUILD**, except where marked. Four of these eleven steps exist today; the
agent loop, the tool policy gate and the learning path have no code at all.

```text
USER
 ↓ UNDERSTAND
 ↓ IDENTITY           BUILT — the contract is the first message of every call
 ↓ CONTEXT BUILD      BUILT — retrieve → recall → merge → budget
 ↓ PLAN
 ↓ POLICY CHECK       (allow / deny / ask)
 ↓ TOOL EXECUTION     (schema, timeout, audit)
 ↓ VERIFY
 ↓ REPAIR / RETRY
 ↓ RESPONSE           BUILT
 ↓ EVENT              BUILT — recorded; not a memory
 ↓ LEARNING           (asynchronous, batch, evaluated)
```

`CONTEXT BUILD` is built but differs from the sketch: `ContextBuilder` retrieves
documents, recalls session-scoped memories, and `HybridContextAssembler` merges both
into one shared token budget. There is no separate compression stage. Evidence is fenced
between boundary lines (#49, with its limits stated in #53) and charged at its rendered
cost, not its bare text (#52).

**Reachable from `pac` with `--documents`** (#58, closing Finding F-2). The corpus is re-read
from the named paths on every run and held in memory; the conversation is stored. Memory
recall is built but not wired into `pac`: nothing on that path promotes memories yet.

The prompt the provider receives is `[identity, evidence?, *history]`: separate messages,
the first two both `Role.SYSTEM`.

The word "policy" does appear in the source — as `ContextBudgetPolicy` and
`ReserveBasedBudgetPolicy`, which allocate a token budget. That is not the tool policy
gate described above, and the name collision should not be read as partial coverage.

## 6. Layer boundaries

**Memory** — BUILT. Holds what the system knows about the user and itself. Written only
through the promotion pipeline, never from a conversation turn: `ExperiencePipeline` is
the sole writer, and `SealedMemoryStore` on the conversation path refuses every
operation. Read back through `MemoryReader`, which exposes no write. `MEMORY_ARCHITECTURE.md`.

**Knowledge** — BUILT. Holds ingested documents, retrieved with provenance. Retrieval
goes through the `Retriever` contract (`HybridRetriever` today); no component reaches an
index directly.

**Agent** — DESIGN TO BUILD. No `agent/` package exists; no planner, executor, verifier
or tool policy gate has been written. `AGENT_ARCHITECTURE.md` is the target.

**Learning** — DESIGN TO BUILD. No `learning/` package exists. `LEARNING_ARCHITECTURE.md`
is the target.

**Evaluation** must use the **same** retrieval, context and prompting path as production.
A separate evaluation pipeline proves nothing about the runtime — a principle carried from
`rag-engine`, which had previously fixed exactly that split.

## 7. Hard invariants

An invariant nothing enforces is an intention. These are split so the two are not read
as equally binding.

**ENFORCED** — each has a test that fails if it is broken:

1. `Event != Memory`. Conversations create events; memories are promoted.
   `test_event_not_memory.py`, plus the sole-writer AST scan in
   `test_experience_pipeline.py`.
2. The base model is replaceable and never hard-coded into business logic.
   `test_no_model_name_literal_in_business_logic`.
5. Every memory carries provenance, confidence, version and status.
   `MemoryRecord.__post_init__`, `test_memory_record.py`.
8. Context is budgeted; the knowledge base is never dumped into a prompt.
   `test_hybrid_assembler.py::test_token_estimate_never_exceeds_the_budget`, and, for the
   rendered message rather than the selection, `test_rendered_budget.py`.

Also enforced, and worth naming because they are not in the original list: dependency
direction (`test_internal_layering_is_respected`), no dead enum members
(`test_enum_producer_guard.py`), and no raw exception data in an event payload
(`test_payload_never_carries_raw_exception_data`).

**INTENDED — NOT YET ENFORCEABLE.** Nothing tests these because the subsystems they
constrain do not exist:

3. Memory and knowledge are never baked into model weights.
4. Normal conversation never modifies weights.
6. Every tool has a schema, permission, risk level, timeout and audit record.
   *(no tools)*
7. Retrieval preserves provenance to the response.
   *(`RetrievalProvenance` exists and travels into the grounding message; no test asserts
   it survives all the way to the user-visible response)*
9. Runtime and evaluation share one grounding path. *(no evaluation harness)*
10. Project business logic stays in connectors. *(no connectors)*

## 8. Cross-cutting decisions from the audit

**Multilingual, not English-with-Arabic-added.** The audited lexical search is
`to_tsvector('english', …)` hard-coded. Arabic lexical retrieval would silently fail. The
Core treats language as a first-class retrieval parameter. See `ADR/ADR-006`.

**Context budget is provider-aware.** The audited builder hard-codes
`DEFAULT_TOKEN_BUDGET = 24000` for a 32K model, and `CHARS_PER_TOKEN = 4`, an
English-centric estimate. The Core derives the budget from the active model and uses a
tokenizer-backed count. See `ADR/ADR-005`.

**Security-first tool execution.** The strongest verified asset in any source is
`unified-llm-local/tool_security.py` — command validation, path containment, audit log,
workspace scoping, ~962 lines of tests, zero project coupling. It anchors the agent's
policy layer. See `ADR/ADR-004`.
