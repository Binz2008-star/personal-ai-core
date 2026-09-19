# ADR-001 — `personal-ai-core` is a separate repository

**Status:** Accepted · Phase 0

## Context

`local-llm-rig` is an established repository owning the local model runtime, Modelfiles,
benchmarks, refusal probes and hardware evidence. The Personal AI Core needs that runtime
but is a much larger system: memory, knowledge, learning, agents and project connectors.

## Decision

Personal AI Core lives in its own repository, `personal-ai-core`. `local-llm-rig` stays a
separate model-infrastructure project and is **not** redesigned into the assistant.
Modelfiles remain there. The Core consumes its measurements as evidence.

## Consequences

Each repository keeps one purpose and its own test suite and release cadence. A model
runtime change does not churn Core history. The cost is cross-repository coordination of
evidence, handled by citing source repository, path and SHA.

## Alternatives rejected

**Build the Core inside `local-llm-rig`** — would mix a measurement harness with an
application and repeat the duplication seen across the Rico and Second Brain repositories.
