# Personal AI Core

An AI operating layer for a local model. Memory, knowledge, experience, learning, tools and
projects are separate layers with their own contracts; the model is a replaceable backend.

**Status: Phase 4 — Memory-Aware Context Recall ACCEPTED / MERGED (PR #2 — MERGED, main `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`, implementation `e8062ff2fa8b6eb5a4471ac8475f29bed76fd369`).**

Phases:
- **Phase 0** — HISTORICAL / COMPLETED — core source audit, evidence freeze, extraction matrix (no separate gate; see [`docs/COMPONENT_EXTRACTION_MATRIX.md`](docs/COMPONENT_EXTRACTION_MATRIX.md))
- **Phase 1** — PARTIAL / NOT COMPLETE — core foundation vertical slice
  (User → Session → Message → ModelProvider → Response → Event).
  [`docs/PHASE_1_RECONCILIATION.md`](docs/PHASE_1_RECONCILIATION.md) records that two of its five
  playbook components are unbuilt. Later phases proceeded on the parts that exist; Phase 1 itself
  was never closed.
- **Phase 2** — ACCEPTED (`0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511`)
  Knowledge & context foundations. In-memory contracts, retrieval, budgeting.
  Tests: 389 passed / 14 skipped.
- **Phase 3** — ACCEPTED / MERGED (`f090100e933d1a6ff18d6e546b384b2e727b2889` merge of `f44de80a5a31e079dbdef4ab7f173d40bd3bc15e` + `ac41d2799c4698960f63c00d024010d1e45f1b18`)
  Memory domain + promotion + persistence contracts & write path.
  Tests: 443 passed / 14 skipped.
- **Phase 4** — ACCEPTED / MERGED (`e8062ff2fa8b6eb5a4471ac8475f29bed76fd369` → `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`, branch `claude/phase-4-memory-aware-context`, PR #2 — MERGED)
  Session-scoped MemoryReader, deterministic retrieval, MemoryRetrievalError, hybrid document/memory context, shared token budget, Grounding (`memory_enabled`), degraded failure, ADR-009.
  Tests: 494 passed / 14 skipped. pyright: 0 errors. Architectural review: PASS. Post-commit audit: PASS.
- **Phase 5** — NOT AUTHORIZED / DESIGN NOT STARTED — UNAUTHORIZED / FUTURE DESIGN (no cross-session recall approval; no contract)

## Start here

| Document | What it holds |
|---|---|
| [`docs/COMPONENT_EXTRACTION_MATRIX.md`](docs/COMPONENT_EXTRACTION_MATRIX.md) | the evidence spine — what exists in each source, verified, with classifications |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | boundaries, modules, dependency direction, invariants |
| [`docs/ENGINEERING_PLAYBOOK.md`](docs/ENGINEERING_PLAYBOOK.md) | how components get extracted; git governance; definition of done |
| [`docs/MEMORY_ARCHITECTURE.md`](docs/MEMORY_ARCHITECTURE.md) | `Event != Memory`, promotion, provenance |
| [`docs/LEARNING_ARCHITECTURE.md`](docs/LEARNING_ARCHITECTURE.md) | event → experience → candidate → evaluation → promotion |
| [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) | agent loop, tool contracts, risk levels, verifier |
| [`docs/MODEL_ARCHITECTURE.md`](docs/MODEL_ARCHITECTURE.md) | registry, providers, Boss model, replacement |
| [`docs/PROJECT_INTEGRATION.md`](docs/PROJECT_INTEGRATION.md) | projects as connectors, anti-corruption layer |
| [`docs/TESTING_STRATEGY.md`](docs/TESTING_STRATEGY.md) | characterization first, six levels, multilingual |
| [`docs/ADR/`](docs/ADR/) | decisions with their evidence |
| [`docs/PHASE_2_CONTRACT.md`](docs/PHASE_2_CONTRACT.md) | Phase 2 knowledge & context contracts |
| [`docs/PHASE_2_IMPLEMENTATION.md`](docs/PHASE_2_IMPLEMENTATION.md) | Phase 2 implementation details |
| [`docs/PHASE_1_RECONCILIATION.md`](docs/PHASE_1_RECONCILIATION.md) | Phase 1 reconciliation notes |

## Reading the labels

Every claim is labelled, because several inherited assumptions did not survive the audit:

- **VERIFIED SOURCE FACT** — read from the source at a recorded SHA
- **ARCHITECTURAL DECISION** — a choice made in response to evidence
- **DESIGN TO BUILD** — no adequate source exists
- **DEFERRED / UNVERIFIED** — not inspected, or inspected structurally only

A filename, a README claim or a line count is not evidence.

## Boss model

```text
huihui_ai/qwen2.5-abliterate:7b
```

Operationally primary, architecturally replaceable, configured in the model registry and
never named in business logic. Distinct from `local-llm-rig`'s benchmark results — see
[`ADR-002`](docs/ADR/ADR-002-boss-model.md).

## Relationship to `local-llm-rig`

`local-llm-rig` is a separate repository owning the model runtime, Modelfiles, benchmarks
and hardware evidence. This Core sits above it and consumes its measurements as evidence.

## Request timeout and cold starts

The first `/api/chat` request after a reboot or an unloaded model can exceed the configured
`DEFAULT_REQUEST_TIMEOUT_SECONDS` (`120`), because the model has to be loaded into VRAM before
the first token and that load does not count as prompt processing. Observed live on a Windows
rig: the first request after a cold start timed out at the configured `120`s; the identical
request completed instantly once the model was resident.

The knob is the environment variable `PAC_REQUEST_TIMEOUT_SECONDS` (read at runtime by the
Ollama provider, no code change needed), e.g. `600` for a cold start. The `120`s default is
deliberately unchanged — raising it is a configuration decision the project tracks, not an
assumption to bake in silently.

## Architectural invariants (hard)

1. **Event != Memory** — conversations create events; memories are promoted
2. **Dependency direction** — memory → core, knowledge → core, context → core, conversation → core
3. **Boss model** — `huihui_ai/qwen2.5-abliterate:7b` (config only)
4. **No dead enum members** — every `RetrievalMethod` / `ExclusionReason` has a producer
5. **SealedMemoryStore** — remains sealed until explicit authorization
6. **No Neon/pgvector/Postgres/migrations** — without explicit owner authorization
7. **Source repositories never modified** — Rico, unified-llm-local, second-brain-kb are read-only

## Phase progression

```
Phase 0: source audit + evidence freeze + extraction matrix
         ↓
Phase 1: core foundation vertical slice        (PARTIAL — never closed)
         ↓
Phase 2: knowledge + context foundations
         ↓
Phase 3: memory domain + promotion + persistence contracts + write path
         ↓
Phase 4: memory READ/RECALL enrichment of conversation context (session-scoped)
         ↓
Phase 5: NOT AUTHORIZED
```

Explicit distinction:
- **Event** → conversation/event path (append-only evidence)
- **Memory** → persistent memory path (promotion gate only)
- **Memory recall** → context enrichment path (session-scoped, read-only enrichment)

## Current limitations (accepted)

- Phase 4 recall is **session-scoped** — no cross-session recall
- No semantic embedding-based memory ranking
- Memory retrieval is enrichment/degradation, not a write path
- Phase 2 rendering overhead limitation remains out of scope
- No production database persistence changes introduced (in-memory only)

## Current state

Phase 4 (PR #2) — **MERGED** to `main` at `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`. Phase 5 — NOT AUTHORIZED / FUTURE DESIGN.
