# ADR-008 — The Second Brain repositories are one lineage

**Status:** Accepted · Phase 0

## Context

The original plan listed `second-brain-kb`, `unified-llm-local` and `code-it` as separate
sources, with their retrieval implementations to be merged into one engine.

**VERIFIED SOURCE FACT.** They are one project in three snapshots:

- Both brain repositories carry the byte-identical README title
  `Second Brain v4 — Autonomous Self-Evolving Code Knowledge Base (Code It)`.
- Several files are hash-identical (`Mcp-All.json`, `HEIMDALL-SETUP.md`).
- `unified-llm-local` is a strict superset — adds `Makefile`, `context_builder.py`,
  `HARDENING-PLAN.md` and ~24 more Python files — and is newer (2026-09-07 vs 2026-09-04).
- `code-it` is the React dashboard named in that README; the brain repositories' own
  `ai-dashboard/` is packaged as `code-it-intelligence-console`.

## Decision

`unified-llm-local @ 21a36b0` is the single extraction source for this lineage.
`second-brain-kb @ c65dbce` and `code-it @ c72aa0a` are historical reference material.

There is no three-way retrieval merge.

## Consequences

One source to characterize instead of three, and no risk of merging three copies of the
same code into one engine — the outcome the plan was trying to avoid.

If a later audit finds capability present in an older snapshot and absent from
`unified-llm-local`, that specific component may be recovered. No such case has been found.
