# ADR-011 — Identity layer contract

**Status:** PROPOSED · design only · nothing built

- Design questions: **RESOLVED** (four, below; reviewed and accepted)
- Identity contract: **PROPOSED**, not accepted
- Identity implementation: **NOT AUTHORIZED**, gated on two prerequisites

This ADR defines a contract. It authorises no implementation, creates no package, and
does not complete Phase 1. Answering its design questions does not authorise building it:
design-complete and implementable are different states, and the two prerequisites below
are the distance between them.

## Context

Phase 1 still has one unbuilt component: the identity foundation.

The intended identity layer provides:

- a response policy,
- a behavioural contract,
- and per-turn prompt composition.

The contract must be:

1. present in every model call,
2. independent of conversation length,
3. owned by the Core rather than copied as a runtime dependency from a source repository,
4. compatible with the model registry and context-budget architecture.

**Phase 1 is not completed or authorised by this ADR.**

**No source repository is modified.** Rico at
`215c316979731eedcdf2f99bcbd97b727229abf5` is read as evidence only.

## Resolved design questions

The four design questions were resolved from the implementation and source evidence
available at `761accc06c0a925499fc26447a3d2cebc6d7e805`.

Each decision records the rejected alternative so a later reader can distinguish an
evidence-backed decision from an unconsidered option.

### 1. Is the behavioural contract fixed or per-session? — **FIXED**

`Session` carries:

- `user_id`
- `id`
- `status`
- `created_at`

There is no configuration field.

A per-session identity contract would therefore extend an existing Phase 1 domain type
to serve a consumer that does not exist. It would also create a second potential source
of truth for the behavioural contract: configuration and session state.

The contract exists specifically to be present in every call, independent of conversation
length. Making the contract vary per session would introduce configuration semantics that
the current architecture does not require.

**Decision:** the behavioural contract is fixed.

**Rejected:** per-session override.

Revisit only if a concrete use case requires it, as a separate ADR.

---

### 2. Does identity text vary by model? — **CONSTANT**

ADR-002 defines the Boss model as operationally primary but architecturally replaceable,
and treats changing the Boss model as a configuration change plus an evaluation gate,
not a code change.

If identity text varied automatically by model, changing the model could silently change
behaviour. That would undermine the architectural replaceability claim.

Identity **cost** is already model-relative through `context_window`; identity **text**
does not need to be.

`ModelSpec.metadata: dict[str, str]` exists as an extension point, but there is currently
no behavioural read of that metadata.

Therefore model-specific identity phrasing is not introduced speculatively. It may be
considered later only when an evaluation demonstrates that a specific model requires
different phrasing.

**Decision:** identity text is constant across models.

**Rejected:** registry-driven/model-specific identity text.

---

### 3. Characterization tests against Rico first? — **NO**

At the pinned Rico SHA `215c316979731eedcdf2f99bcbd97b727229abf5`:

- `tests/test_rico_identity_guardrails.py` contains 32 tests.
- Six mention `get_language_rule`.
- Only two actually test `get_language_rule`.
- Those tests assert substrings of Rico's own English wording, including:
  - `"الفصحى"`
  - `"Modern Standard Arabic"`
  - `"NEVER use a regional dialect"`
  - `"Reply in English"`
- The remaining tests concern `get_rico_system_prompt` behaviour that is specific to the
  Rico product, including pricing, `ricohunt.com`, auto-apply, and job listings.

The Core owns its own identity text. Therefore copying Rico's exact string assertions
into the Core would couple Core tests to wording the Core is not intended to preserve.

That would be textual coupling to a source repository rather than characterization of
the Core's intended behaviour.

The useful evidence from Rico is the behavioural failure, not the source fixture.

The Rico test suite documents an owner escalation dated 2026-07-21 involving a Jordanian
user being addressed in hardcoded Gulf dialect and responses becoming verbose with emoji
menus. That incident transfers as a behavioural requirement for the Core's own tests.

**Decision:** do not create characterization tests against Rico's exact identity text.

**Rejected:** characterization tests against the pinned Rico SHA.

The corresponding instruction in `PHASE_1_RECONCILIATION.md` §4 to write those
characterization tests first is superseded by this decision. The reconciliation document
must reflect this decision so the two architectural records do not disagree.

---

### 4. Does the response policy own answer length? — **THE PREMISE WAS INCOMPLETE**

The original question assumed that the response policy and generation budget might
disagree about the answer-length number.

The current implementation shows that the problem is earlier than that: the generation
reserve is currently accounting-only.

`DEFAULT_GENERATION_RESERVE = 1024` represents reserved generation capacity, but the
generation path does not currently enforce that value as a provider output limit.

At the current implementation there is no `max_tokens`, `num_predict`, or equivalent
generation cap in `src/`. `OllamaProvider` supports provider options, but the normal
generation path does not currently use those options to enforce `generation_reserve`.

The directly established architectural fact is therefore:

> `generation_reserve` is currently accounting-only and is not enforced by the provider.

The responsibilities must remain separate:

```text
identity policy
    → tells the model how to respond
      (register, concision)

generation budget
    → determines how much generation is allowed
      (the number)

provider enforcement
    → makes that number real
      (currently missing)
```

The policy owns the instruction.

The budget owns the number.

Provider enforcement makes the numeric limit operational.

These responsibilities must not be collapsed into a single identity abstraction.

**Decision:** answer-length guidance belongs to the response policy as an instruction;
the numeric generation limit belongs to the context/generation budget; enforcement is a
separate prerequisite because it does not currently exist.

---

## Prerequisites before identity implementation

The four design questions are now resolved, but identity implementation remains
unauthorised.

Two prerequisites must be handled as separate changes and separately authorised.

```text
ADR-011 identity implementation
        |
        +-- prerequisite A
        |   ContextAllocation gains an explicit identity share
        |   (Rule 4)
        |
        +-- prerequisite B
            generation_reserve becomes an enforced provider limit
            (Q4 finding)
        |
        v
    Identity layer implementation
```

### Prerequisite A — explicit identity budget share

ADR-011 Rule 4 requires identity to have a named share in `ContextAllocation`.

The current `ContextAllocation` does not contain an identity field.

Adding that field is therefore a change to a built contract and must be reviewed and
authorised independently.

This ADR does **not** implement that change.

### Prerequisite B — enforce generation reserve

The current generation path reserves `generation_reserve` for accounting but does not
pass an equivalent output limit to the provider.

Making the reserve enforceable is therefore a separate generation-path change.

The existing `OllamaProvider` already accepts provider options; this does not authorise
creating a new provider abstraction.

This ADR does **not** implement that change.

## Scope guardrails

The prerequisites and this ADR do not authorise unrelated architectural work.

Nothing in this ADR authorises:

* creating `src/personal_ai_core/identity/`,
* persistence changes,
* Neon or pgvector changes,
* database migrations,
* model-specific identity templates,
* a new session configuration mechanism,
* a new provider abstraction,
* changing the Boss model,
* Phase 5,
* modifying any source repository.

In particular, answering the four design questions does **not** authorise identity
implementation.

The questions were the **design gate**, not the **implementation gate**.

## Implementation boundary

When implementation is eventually authorised, the identity layer must remain consistent
with these established rules:

1. Identity is composed per turn.
2. Identity is not persisted as conversation memory.
3. Identity precedes grounding and conversation history.
4. Identity is configuration-driven rather than embedded as scattered business logic.
5. Identity has an explicit context-budget share.
6. Behavioural rules remain independent of conversation length.
7. The Core owns the identity contract; Rico is evidence, not a runtime dependency.
8. The Boss model remains `huihui_ai/qwen2.5-abliterate:7b`.

Until the prerequisites are separately authorised and completed, no identity package or
identity implementation should be created.

## Status

**Design questions:** RESOLVED

**Identity contract:** PROPOSED, not accepted

**Identity implementation:** NOT AUTHORIZED

**Phase 1:** NOT COMPLETE

**Phase 5:** NOT AUTHORIZED