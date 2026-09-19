# ADR-005 — Context budget is derived from the active model

**Status:** Accepted · Phase 0

## Context

**VERIFIED SOURCE FACT.** `unified-llm-local` @ `21a36b0` `context_builder.py` provides a
real budget contract — `ContextChunk.token_estimate`, `ContextBuilder` — but hard-codes
`DEFAULT_TOKEN_BUDGET = 24000`, documented as sized for "qwen2.5:7b 32K", and estimates
with `CHARS_PER_TOKEN = 4`.

Neither constant is safe here. The Boss model runs at an **8192** context, so the inherited
budget is roughly triple what fits. And 4-chars-per-token is an English heuristic; Arabic
tokenizes differently, so the estimate is wrong in a language the product treats as
first-class.

## Decision

Adapt the contract; reject the constants.

The budget is read from the active model's `ModelRegistry` entry (`context_window`) and
apportioned across identity, conversation, memory, knowledge, tool observations and
generation. Token counting is tokenizer-backed, not character-derived, and correct for
Arabic and English.

## Consequences

Swapping the Boss model automatically re-sizes the budget. A miscount can no longer
silently overflow the context — which fails as truncation, the quietest failure mode there
is. Tests assert the budget is never exceeded, in both languages.
