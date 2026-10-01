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

## Owner note (2026-10-01): the open-response behaviour is intended

The abliterated model was chosen for how it behaves, not as "the best model". It does
not refuse a question for being sensitive or uncomfortable, and it answers directly.
Where a limit is needed, it lives in Core, as policy and guards that can be read,
tested and turned off: the identity contract, ADR-018's secret redaction and ADR-019's
language guard. It is not buried in the weights.

Consequence for any model change, including a fine-tune of this model: keeping the
open-response behaviour is an evaluation requirement, not a regression to fix. A
candidate must be measured on the false-refusal set (0/15 today) alongside the contract
evaluation, and is not adopted if it refuses more.

