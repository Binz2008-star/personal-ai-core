# Project Integration

**Status: Phase 0 — design. Phase 6 work; not started.**

---

## 1. Rule

**ARCHITECTURAL DECISION.** Project business logic never enters the Core. Projects attach
as connectors behind an anti-corruption layer.

```text
Rico implementation
        ↓
   RicoAdapter              translates
        ↓
  Core interface            owns the vocabulary
```

The Core must never contain `from rico_memory import RicoMemory`. It imports
`core.memory.MemoryStore`, and an adapter satisfies it. The test of success: deleting a
project connector must leave the Core working.

## 2. Connector shape

```text
projects/connectors/<project>/
├── connector.py     lifecycle, registration
├── adapter.py       project types ↔ Core contracts
├── tools.py         project tools, each with schema + risk level
├── knowledge.py     what to ingest and how
└── schemas.py       project-side types, never leaked inward
```

The Core knows a project has *tools*, *knowledge*, *state* and *provenance*. It knows
nothing about job boards, video pipelines or billing.

## 3. Excluded from the Core

**VERIFIED SOURCE FACT** — identified by domain-coupling measurement during the audit.

| Source | Excluded |
|---|---|
| Rico | UAE/job-search logic, CV/job matching and scoring, job boards, application workflows, LinkedIn/Indeed, Telegram, Jotform, Paddle/billing, SaaS assumptions, the `RICO_IDENTITY` text |
| Robin | all video/ffmpeg/OpenCV code, publishing and upload pipelines |
| `unified-llm-local` | `merge_gate.py`, `merge_lock.py` git workflow |

Coupling was measured, not guessed. `rico_identity.py` carries 54 domain references in 237
lines — the densest in any candidate — which is why its *content* is excluded while its
*contract structure* is adapted.

## 4. Import lifecycle

```text
register project → index sources → declare tools → declare knowledge
   → record provenance → evaluate → enable
```

Every imported item keeps its origin: repository, path, commit SHA, ingested-at. A
retrieval result that cannot name its source is a bug.

## 5. Order

**ARCHITECTURAL DECISION.** No connector is built before the Core is stable through
Phase 5. Connectors are the last phase because a connector written against an unstable
Core encodes that instability.

Expected first connectors, in likely order of value: **Second Brain** (closest to Core
concerns), **Rico** (richest tool surface), **Robin** (bounded automation patterns).

## 6. Relationship to `local-llm-rig`

`local-llm-rig` is **not** a connector. It is the model runtime layer beneath the Core: a
separate repository owning Modelfiles, benchmarks and hardware evidence. The Core consumes
its measurements as evidence and never absorbs its scripts wholesale.
