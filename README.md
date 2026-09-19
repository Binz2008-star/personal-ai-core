# Personal AI Core

An AI operating layer for a local model. Memory, knowledge, experience, learning, tools and
projects are separate layers with their own contracts; the model is a replaceable backend.

**Status: Phase 0 — Core Source Audit: sufficiently verified for architecture drafting.**
No implementation yet. Deferred source audits remain (see the extraction matrix §6).

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
