# Architecture

**Status: Phase 0 — Core Source Audit: sufficiently verified for architecture drafting.**

Evidence for every source claim is in [`COMPONENT_EXTRACTION_MATRIX.md`](COMPONENT_EXTRACTION_MATRIX.md).

---

## 1. What this system is

Personal AI Core is an **AI operating layer**, not an application with a model inside it.
The model is a replaceable backend. Memory, knowledge, experience, learning, tools and
projects are separate layers with their own contracts.

**ARCHITECTURAL DECISION.** The Core backbone is newly engineered. The audit established
that no source repository provides the event, experience, promotion, verifier or
reranking subsystems. Existing repositories supply **edge components and patterns**.

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

`local-llm-rig` is **not** part of this repository. It owns the model runtime, Modelfiles,
benchmarks and hardware evidence. This Core consumes its measurements; it does not absorb
its scripts wholesale.

Project-specific systems attach at the edge, never inside:

```text
                     PROJECT CONNECTORS
                             │
      ┌──────────┬───────────┼───────────┬──────────┐
      │          │           │           │          │
    Rico       Robin    Second Brain   GitHub    Future
```

## 3. Module architecture

```text
core/          contracts, schemas, errors, config, lifecycle
runtime/       ollama/, model_registry/, inference/, generation/
identity/      personality, behavioral contract, response policy
conversation/  sessions, messages, events
memory/        working, episodic, semantic, preferences, decisions,
               lessons, patterns, promotion, storage
knowledge/     ingestion, parsers, chunking, embeddings, indexing,
               retrieval, reranking, provenance
context/       retrieval, ranking, compression, budget
agent/         planner, executor, tools, policy, verifier, state, recovery
learning/      events, feedback, experience, analysis, dataset,
               training, evaluation, promotion
evaluation/    regression, capability, memory, retrieval, agent,
               Arabic, coding, performance
projects/      registry, connectors, adapters, indexes
persistence/   migrations, repositories, postgres
api/  ui/  tests/  scripts/
```

## 4. Dependency direction

**ARCHITECTURAL DECISION.** Dependencies point inward. The Core depends on abstractions;
concrete providers depend on the Core.

```text
        OllamaProvider ──┐
    PostgresMemoryStore ─┼──▶  core/contracts  ◀── agent, memory, knowledge
   OllamaEmbeddingProvider ┘
```

Three consequences, each a direct response to an audited defect:

- **No `import ollama` outside `runtime/ollama/`.** The audit found embedding calls
  hard-wired and duplicated across `api.py`, `evolve.py` and `scripts/e2e_test.py`.
- **No direct database driver use in components.** Access goes through repositories.
- **No model name in business logic.** The Boss model is registry configuration.

## 5. Runtime flow

```text
USER
 ↓ UNDERSTAND
 ↓ CONTEXT BUILD      (retrieve → rank → dedupe → compress → budget)
 ↓ PLAN
 ↓ POLICY CHECK       (allow / deny / ask)
 ↓ TOOL EXECUTION     (schema, timeout, audit)
 ↓ VERIFY
 ↓ REPAIR / RETRY
 ↓ RESPONSE
 ↓ EVENT              (recorded; not a memory)
 ↓ LEARNING           (asynchronous, batch, evaluated)
```

## 6. Layer boundaries

**Memory** holds what the system knows about the user and itself. It is written only
through the promotion pipeline, never directly from a conversation turn. `MEMORY_ARCHITECTURE.md`.

**Knowledge** holds ingested documents and code, retrieved with provenance.
Memory never queries a vector store directly; it goes through `RetrievalService`.

**Agent** plans and acts. Every tool call passes the policy gate and is audited.
`AGENT_ARCHITECTURE.md`.

**Learning** consumes events and produces candidates. It never modifies model weights
during a conversation. `LEARNING_ARCHITECTURE.md`.

**Evaluation** must use the **same** retrieval, context and prompting path as production.
A separate evaluation pipeline proves nothing about the runtime — a principle carried from
`rag-engine`, which had previously fixed exactly that split.

## 7. Hard invariants

1. `Event != Memory`. Conversations create events; memories are promoted.
2. The base model is replaceable and never hard-coded into business logic.
3. Memory and knowledge are never baked into model weights.
4. Normal conversation never modifies weights.
5. Every memory carries provenance, confidence, version and status.
6. Every tool has a schema, permission, risk level, timeout and audit record.
7. Retrieval preserves provenance to the response.
8. Context is budgeted; the knowledge base is never dumped into a prompt.
9. Runtime and evaluation share one grounding path.
10. Project business logic stays in connectors.

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
