# ADR-002 — Boss model and its separation from benchmark results

**Status:** Accepted · Phase 0

## Context

`local-llm-rig` measured nine local models on a GTX 1060 6GB. Against a 15-question
false-refusal set, `qwen2.5:7b` and `huihui_ai/qwen2.5-abliterate:7b` both scored **0
refusals**; the stock model produced roughly twice the answer length at equal budget. That
is a benchmark result about a benchmark set.

A benchmark result and a product decision are different things, and conflating them would
let a measurement silently redefine the system.

## Decision

The Personal AI Core Boss model is **`huihui_ai/qwen2.5-abliterate:7b`**.

It is operationally primary and architecturally replaceable. `qwen2.5:7b` remains a
`local-llm-rig` benchmark result and is **not** substituted for the Boss model.

The model name appears only in `ModelRegistry` configuration, never in business logic.

## Consequences

Changing the Boss model is a configuration change plus an evaluation gate, not a code
change. If swapping it ever requires editing application code, the abstraction has failed
and that is a bug.

## Notes

The refusal measurement is scoped: 0/15 on benign-to-borderline lawful prompts means the
model does not over-refuse that set. It is not a general claim about restriction.
