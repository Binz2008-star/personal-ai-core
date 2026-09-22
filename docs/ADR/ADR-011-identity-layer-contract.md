# ADR-011 — Identity layer contract

**Status:** PROPOSED · design only · nothing built

- Design questions 1-4: **RESOLVED** (reviewed and accepted)
- Contract questions 5-8: **DECIDED HERE**, not yet reviewed
- Identity contract: **PROPOSED**, not accepted
- Prerequisites A and B: **BOTH DONE**
- Identity implementation: **NOT AUTHORIZED** — and no longer blocked by anything
  but that decision

This ADR defines a contract. It authorises no implementation, creates no package, and
does not complete Phase 1. Answering its design questions does not authorise building it:
design-complete and implementable are different states, and the prerequisites below were
the distance between them. Both are now closed, which removes the obstacles and supplies
no authorisation: those were never the same thing.

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

The implementation showed that the problem was earlier than that: the generation reserve
was accounting-only.

**As measured when this ADR was written:** `DEFAULT_GENERATION_RESERVE = 1024` represented
reserved generation capacity, but the generation path did not enforce it as a provider
output limit. There was no `max_tokens`, `num_predict` or equivalent cap anywhere in
`src/`. `OllamaProvider` accepted provider options; nothing populated them with the
reserve.

The finding stood as:

> `generation_reserve` is currently accounting-only and is not enforced by the provider.

**That is no longer the state.** Prerequisite B closed it: `ConversationService` now
derives the limit from the budget policy and sends it as `num_predict` on every turn,
grounded or not. The finding is kept rather than deleted, because the *reasoning* below
is what makes the three-way separation necessary, and that reasoning does not expire with
the defect that exposed it.

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
      (prerequisite B — done)
```

The policy owns the instruction.

The budget owns the number.

Provider enforcement makes the numeric limit operational.

These responsibilities must not be collapsed into a single identity abstraction.

**Decision:** answer-length guidance belongs to the response policy as an instruction;
the numeric generation limit belongs to the context/generation budget; enforcement is a
separate prerequisite because it does not currently exist.

---

## The contract — three Core-owned concepts

Composed into exactly one `Role.SYSTEM` message per turn. The `Role.SYSTEM` role already
exists in `core/domain.py`; `conversation/grounding.py` is currently its only producer,
and what it emits is retrieved evidence, not identity.

### `ResponsePolicy` — how to answer

Derived from the turn's `language` and the active `ModelSpec`. Governs:

- **language and register** — Modern Standard Arabic for `ar`, no regional dialect unless
  the user asks for one, and no switching language mid-reply;
- **answer discipline** — concision; no decorative filler.

Adapted from `get_language_rule` in **substance only**; per design question 2 the text is
constant across models, and per design question 3 the Core owns that text rather than
inheriting Rico's wording.

### `BehavioralContract` — what is non-negotiable

Numbered rules present in every model call, independent of conversation length. The
*mechanism* is what transfers from the source — rules that a long conversation cannot
push out of the window; the rule set is the Core's own. Three rules transfer in substance
per `PHASE_1_RECONCILIATION.md` §1:

1. never fabricate credentials,
2. never act without explicit confirmation,
3. never disclose secrets.

### `IdentityComposer` — assembly

Produces one `Message(role=Role.SYSTEM, ...)` from the policy and the contract. A
protocol with a default implementation, so the composition is replaceable without
modifying `ConversationService`.

Naming these three concepts fixes the shape of the contract. It authorises no module, no
class and no file: see **Scope guardrails**, which forbids creating
`src/personal_ai_core/identity/`.

---

## Contract questions the shape did not answer

Naming `ResponsePolicy`, `BehavioralContract` and `IdentityComposer` fixes the contract's
**shape**. Four questions are not answered by a shape, and each of them decides something
the first line of code would otherwise decide by accident:

| | |
|---|---|
| where the text lives | boundary rule 4 says "configuration-driven" and not where |
| what happens on conflict | the only conflict resolved so far is answer length |
| how the text may change | ADR-002 gates a model change; nothing gates a rule change |
| which layer holds it | `conversation` may import `core` only, and this is enforced |

They are decided here, before the classes, for the reason stated above: classes built
before the words exist are empty containers.

### 5. Where does the identity text live? — **IN CODE, IN THE CORE'S TREE**

Boundary rule 4 requires identity to be "configuration-driven rather than embedded as
scattered business logic". That is a rule against *scattering*, not a requirement to
*externalise*: one named place that the composer reads satisfies it.

An external file (YAML, TOML, environment) would add a load path, a parse failure mode
and a second source of truth for behaviour. It would also let the behavioural contract
change **without a diff**, which removes the review gate that question 7 below depends
on. Against that it buys deployment flexibility, and the recorded deployment constraint
is one user, one process, local machine: there is no deployment to be flexible for.

A durable store is worse on both counts, and would couple identity to ADR-010, which is
PROPOSED with no option selected. Boundary rule 2 already forbids persisting identity as
conversation memory; this decision says the text is not persisted at all.

**Decision:** the identity text lives in the Core's source tree, in one named module,
read through the `IdentityComposer` protocol.

**Rejected:** external configuration file; persistence in a durable store.

**Consequence, and the point of the decision:** changing a rule is a reviewable diff.

### 6. What happens on conflict? — **TWO CONFLICTS, ONE ANSWERED**

**A user instruction against the contract.** `BehavioralContract` does not yield. A rule
a user can talk the system out of is not a rule, and the three rules that transfer are
exactly the ones whose failure is unrecoverable: fabricated credentials, action without
explicit confirmation, disclosed secrets.

`ResponsePolicy` is different and **does** yield, inside its own scope. The dialect
exception already written into the policy — Modern Standard Arabic *unless the user asks
for a regional dialect* — is the model for the whole of it: the policy governs register,
and register is the user's to ask about.

This is an instruction-level guarantee. Nothing in `src/` enforces it, and this ADR does
not claim otherwise. Whether any contract rule deserves a code-level guard in addition to
its sentence is a question for the first implementation, not for this section.

**An identity share larger than the window.** Today `ContextAllocation` reports
`overcommitted` and `evidence` falls to zero — observed, and asserted by
`test_identity_can_overcommit_the_window_visibly`. That is the right *signal*. Whether it
is the right *ending* — a turn that runs with no evidence rather than failing — is a
different question, and identity is funded at zero, so nothing can reach it.

**Decision:** deferred, with its trigger named. The first non-zero `identity_reserve`
must arrive together with the decision about what the conversation path does with
`overcommitted`. Writing that rule now would put a sentence in this file that no code
can reach and nothing can check, which is the defect class this repository keeps closing.

### 7. How may the identity text change? — **ONLY AGAINST A STATED FAILURE**

ADR-002 treats replacing the Boss model as a configuration change **plus an evaluation
gate**. The behavioural text deserves a gate for the same reason: it is the product's
behaviour, and a repository that treats an untraceable `24000` as a defect worth an ADR
must not leave the rules that shape every reply ungoverned.

**Decision:** a change to a numbered `BehavioralContract` rule, or to the substance of
`ResponsePolicy`, is admissible only in a pull request that states the behavioural
failure it answers. The escalation recorded in design question 3 — a Jordanian user
addressed in hardcoded Gulf dialect, replies turning verbose with emoji menus, dated
2026-07-21 — is the template: an observed, dated failure, not a preference.

**Rejected:** an evaluation-suite gate on ADR-002's model. ADR-002 can require an
evaluation because model replacement has a measurable comparison; this repository has no
evaluation harness, and naming one as a gate would make the gate fictional. **Revisit
this decision when evals exist** — that is the change that should reopen it.

**Rejected:** no gate. The text is the behaviour.

### 8. Which layer holds it? — **PROTOCOL IN `core`, TEXT OUTSIDE IT**

`conversation` may import `core` only. This is not a convention: `LAYER_MAY_IMPORT` in
`tests/unit/test_dependency_direction.py` enforces it, and `conversation/factory.py` is
the single named composition root.

Prerequisite B settled the same problem for the budget and is the precedent to follow:
`ContextBudgetPolicy` is a protocol in `core/contracts.py`, implemented in `context/`,
and injected by the factory. `ConversationService` never names the implementation.

The text stays out of `core` for a separate reason: `core` depends on nothing and states
contracts. Product wording is not a contract.

**Decision:** the contract types and the `IdentityComposer` protocol belong in `core`;
the identity text and the default composer belong in their own package, injected by the
composition root.

**Rejected:** text in `core`; `identity/` imported directly by `conversation`, which the
layering guard would reject.

Already guarded, and worth knowing before the package exists: that test **fails rather
than skips** for any package with no `LAYER_MAY_IMPORT` entry, and names the starting
rule — `{"identity": {"core"}}`. The first identity package cannot go unchecked.

---

**None of the four authorises implementation.** They remove the objection that the
contract's shape was settled while its text was not. The contract remains **PROPOSED**.

---

## Prerequisites before identity implementation

The four design questions are now resolved, but identity implementation remains
unauthorised.

Each prerequisite was a separate change, separately authorised. Both are closed.

```text
ADR-011 identity implementation
        |
        +-- prerequisite A   DONE
        |   ContextAllocation has an explicit identity share, funded at zero
        |   (Implementation boundary: "explicit context-budget share")
        |
        +-- prerequisite B   DONE
            generation_reserve is an enforced provider limit
            (Q4 finding)
        |
        v
    Identity layer implementation   NOT AUTHORIZED
```

**Both prerequisites being done does not authorise identity implementation.** They were
obstacles, not permission. What remains is a decision, and it is the owner's.

The thing to settle before the shape is the contract's **text**. Questions 5-8 settle how
that text is *governed* -- where it lives, what overrides it, how it may change, which
layer holds it. They do not author the rules, and this ADR still does not: what
`ResponsePolicy` and `BehavioralContract` **say** remains unwritten. Building the classes
before the words exist would produce empty containers -- the failure this ADR already
suffered once, when a rewrite left a "behavioural contract" with no rules in it.

### Prerequisite A — explicit identity budget share

The **Implementation boundary** below requires identity to have an *explicit
context-budget share*. That means a named `identity` field on `ContextAllocation`, funded
explicitly and counted in `spoken_for`, so the share is traceable and `evidence` shrinks
by a stated amount rather than an unexplained one.

(An earlier draft cited this as "Rule 4", from a numbered list the clean rewrite replaced.
The reference is to the boundary item by its wording, not its position: an index breaks
silently when a list is renumbered.)

**Now:** `ContextAllocation` carries `identity: int = 0`, validated non-negative and
counted in `spoken_for`. `ReserveBasedBudgetPolicy` takes `identity_reserve`, the share
appears in `source`, and `summarize()` records it.

The substantive decision was the default, not the field. `identity/` is not built, so a
reserve invented for it would shrink `evidence` today for no benefit and the number would
be fabricated -- ADR-005's hard-coded 24000 in a smaller costume. Zero is the honest
value until something supplies a real one. The share is *fundable* now and *funded at
zero*, which is why it is a budget line rather than decoration.

`spoken_for` now sums `ContextAllocation.SHARES`, and a test walks the dataclass fields to
assert every non-window field is counted. That guard is for the next share, not this one:
a field added and left out would exist, be recorded, and cost nothing.

### Prerequisite B — enforce generation reserve — **DONE**

**Was:** the generation path reserved `generation_reserve` for accounting and passed no
equivalent output limit to the provider.

**Now:** `ConversationService` takes a `ContextBudgetPolicy` and derives the turn's limit
from it, sending it as `num_predict`. Verified against the payload the transport
received, not against the allocation — asserting on the allocation would re-check the
accounting that was already true.

Two points of the closed change are worth keeping, because they were decisions rather
than mechanics:

- **Enforcement is unconditional.** The reserve is unreachable on the ungrounded path:
  `_ground` returns `None` without a `context_builder`, so there is no allocation to
  read. Enforcing only where an allocation exists would have made the limit conditional
  on whether retrieval happened to be wired, which is not a limit. The policy is
  therefore a required constructor argument: no service can exist without one.
- **An explicit caller value wins.** A caller naming `num_predict` has said something
  more specific than the default, and silently overriding it would make the `options`
  parameter a lie. The limit is recorded on `GENERATION_REQUESTED` either way.

No new provider abstraction was created: `OllamaProvider` already accepted options and
is untouched.

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

**Design questions 1-4:** RESOLVED

**Contract questions 5-8:** DECIDED, awaiting review

**Identity contract:** PROPOSED, not accepted

**Identity implementation:** NOT AUTHORIZED

**Phase 1:** NOT COMPLETE

**Phase 5:** NOT AUTHORIZED