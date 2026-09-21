# ADR-011 — Identity layer contract

**Status:** PROPOSED · design only · nothing built

- Design questions: **RESOLVED** (four, below; reviewed and accepted)
- Identity contract: **PROPOSED**, not accepted
- Identity implementation: **NOT AUTHORIZED**, and gated on two prerequisites

This ADR defines a contract. It authorises no implementation, creates no package, and
does not complete Phase 1. Answering its open questions does not authorise building it:
design-complete and implementable are different states, and the two prerequisites below
are the distance between them.

## Context

Identity is the last unbuilt component of Phase 1. `PHASE_1_RECONCILIATION.md` at
`350d783` records four of five as built and `identity/` as not; `ARCHITECTURE.md` §3
lists `identity/ personality, behavioral contract, response policy` under
**DESIGN TO BUILD**.

**VERIFIED SOURCE FACT**, measured against `src/` at `350d783`:

| Claim | State |
|---|---|
| `src/personal_ai_core/identity/` | does not exist |
| `ResponsePolicy` / `BehavioralContract` anywhere in `src/` | 0 occurrences |
| `Role.SYSTEM` in `core/domain.py` | exists |
| Anything producing a `Role.SYSTEM` message | `conversation/grounding.py` only |

So every model call today sends the conversation history and, when evidence is
retrieved, one grounding message. `grounding.py` is evidence injection, not identity:
it states what was retrieved, never how to answer or what not to do.

Three facts make this a gap worth closing before anything is built on top of it.

**1. The Boss model is deliberately refusal-reduced, and nothing constrains it.**
ADR-002 selects `huihui_ai/qwen2.5-abliterate:7b` and scopes its own evidence
precisely: *"0/15 on benign-to-borderline lawful prompts means the model does not
over-refuse that set. It is not a general claim about restriction."* The Core pairs
that model with no behavioural contract at all. The ADR was careful about what its
measurement proved; the system has not yet supplied what the measurement did not.

**2. The bilingual requirement has no generation-side implementation.** ADR-006 made
language first-class for *retrieval* and required Arabic cases in every retrieval test
suite. Nothing does the same for *generation*. `send()` already carries a `language`
argument and `Message.language` is persisted, but no rule consumes either to govern the
reply.

`PHASE_1_RECONCILIATION.md` §1 identifies the adaptable asset. **VERIFIED SOURCE FACT**,
read at the pinned SHA `215c316979731eedcdf2f99bcbd97b727229abf5`,
`src/rico_identity.py` L181-197: `get_language_rule(user_lang)` requires Modern Standard
Arabic, forbids regional dialect unless asked, and forbids switching language mid-reply.
It is ~18 lines, domain-neutral, and already tested. The Core has no other source for
this rule.

**3. ADR-005 already promised identity a budget share that does not exist.** Its
Decision reads: *"apportioned across identity, conversation, memory, knowledge, tool
observations and generation."* **VERIFIED SOURCE FACT:** `ContextAllocation`
(`core/context.py`) has four fields — `context_window`, `generation_reserve`,
`overhead`, `history`. There is no `identity` share, and `evidence` is computed as
whatever survives the other three.

The claim is made twice and implemented neither time. `core/context.py`'s own module
docstring says a budget is what remains *"after identity, conversation history, tool
observations and the generation reserve have taken their share"* — describing, in the
file that defines `ContextAllocation`, a share that file does not allocate.

This matters more than a missing field. ADR-005's stated failure was *"a budget nobody
could trace"*, and here the untraceable share is asserted in two documents and the
implementation contradicts both. Adding identity tokens without a named share would fund them out of
`overhead` or silently shrink `evidence` — reproducing precisely the defect ADR-005
exists to prevent, in the act of satisfying its own text.

## Decision

Three Core-owned concepts, composed into exactly one `Role.SYSTEM` message per turn.

### `ResponsePolicy` — how to answer

Derived from the turn's `language` and the active `ModelSpec`. Governs language and
register (Modern Standard Arabic for `ar`, no regional dialect unless asked, no
mid-reply switching), and answer discipline (concision; no decorative filler).

Adapted from `get_language_rule` in **substance**; the Core owns the text.

### `BehavioralContract` — what is non-negotiable

Numbered rules present in every call, independent of conversation length. The
*mechanism* — rules that cannot be pushed out of the window by a long conversation — is
what transfers from the source; the rule set is the Core's own. Three rules transfer in
substance per `PHASE_1_RECONCILIATION.md` §1: never fabricate credentials, never act
without explicit confirmation, never disclose secrets.

### `IdentityComposer` — assembly

Produces one `Message(role=Role.SYSTEM, ...)` from the policy and the contract. A
protocol, with a default implementation, so the composition is replaceable without
touching `ConversationService`.

### Four rules that follow

1. **Composed per turn, never persisted.** Identity is derived from configuration at
   call time and is not written to the message repository — the rule `grounding.py`
   already follows, for the same two reasons: persisting it would charge the budget for
   the same tokens on every later turn, and a stored identity would outlive the
   configuration change that was supposed to replace it.

2. **Identity precedes grounding.** Prompt order becomes `[identity, grounding,
   *history]`. A behavioural contract that retrieved evidence can displace is not a
   contract.

3. **Configuration, never a literal in business logic.** The same rule ADR-002 applies
   to the Boss model. Changing the identity text must not require editing
   `conversation/`.

4. **Identity gets a named budget share.** `ContextAllocation` gains an `identity`
   field, funded explicitly and counted in `spoken_for`, so the share is traceable and
   `evidence` visibly shrinks by a stated amount rather than an unexplained one.

## Consequences

`ConversationService` gains one collaborator. The dependency direction is unchanged:
`identity/` depends on `core/`, and `conversation/` composes it — no new inward edge.

**`evidence` shrinks.** Rule 4 makes identity's cost visible, which means the number is
now smaller and traceable rather than smaller and unexplained. `test_context_budget.py`
and `test_context_budget_policy.py` will need the new share; that is the correct cost.

Rule 4 is a change to a **built** contract. It is the one part of this ADR that touches
existing behaviour, and it is a prerequisite rather than part of the identity package —
it should land as its own change, before any `identity/` module exists.

**The Arabic reply rule becomes testable.** Today there is no artefact to test; after
this contract there is a `ResponsePolicy` whose Arabic and English outputs can be
asserted. ADR-006 required Arabic cases in every retrieval suite; this is the
generation-side counterpart and should carry the same requirement.

A related gap stays open and is **not** addressed here: `memory/rules.py` records that
`CorrectionRule` inspects English markers only, so Arabic corrections are not detected.
That is a memory-rules defect, not an identity one.

## What this ADR does not decide

- **No implementation.** No `identity/` package, no module, no test.
- **The rule text is not authored here.** This fixes the shape, not the words.
- **`ContextAllocation` is not extended here.** Rule 4 is recorded as a prerequisite;
  changing it requires its own authorisation.
- **Phase 1 is not completed or authorised** by this ADR.
- **No source repository is modified.** Rico at `215c316` is read as evidence only.

## Resolved design questions

Answered from the code at `761accc` and reviewed; each records what was rejected, so a
later reader sees the alternative that was considered rather than only the survivor.

### 1. Is the behavioural contract fixed or per-session? — **FIXED**

`Session` carries `user_id`, `id`, `status`, `created_at`. There is no configuration
field of any kind. Per-session identity would extend a Phase 1 built domain type to
serve a consumer that does not exist, and would put the contract in two places at once
-- configuration and session state -- which is the defect class PRs #8-#13 and #23-#26
closed elsewhere.

It also contradicts the property the contract exists for: present in every call,
*independent of conversation length*. A contract that varies per session is a default
with extra steps.

**Rejected:** per-session override. Revisit only for a concrete case, as its own ADR.

### 2. Does identity text vary by model? — **CONSTANT**

ADR-002 states the Boss model is "operationally primary and architecturally
replaceable", and that "changing the Boss model is a configuration change plus an
evaluation gate, not a code change". Identity text that varied by model would make a
model swap silently change behaviour -- that replaceability claim failing quietly.

What *is* model-relative is identity's **cost**, through `context_window`, which Rule 4
already handles.

`ModelSpec.metadata: dict[str, str]` exists and nothing reads it for behaviour. It is
the escape hatch, usable only when an eval demonstrates a specific model needs different
phrasing. Evidence first, not anticipation.

**Rejected:** registry-driven identity text.

### 3. Characterization tests against Rico first? — **NO**

**VERIFIED SOURCE FACT**, at the pinned SHA `215c316`:
`tests/test_rico_identity_guardrails.py` has 32 tests. Six mention `get_language_rule`;
**two** test it. Both assert substrings of Rico's own English wording -- `"الفصحى" in
rule`, `"Modern Standard Arabic" in rule`, `"Reply in English" in rule`. The other
thirty test `get_rico_system_prompt`: pricing, the product domain, auto-apply, job
listings -- all dropped by this Core.

A characterization test characterises behaviour the new implementation intends to
preserve. This ADR already decided the Core owns its own text, so those assertions would
pin strings the Core will never use: they would pass by copying Rico's words, creating
textual coupling to a source repository the hard invariants keep at arm's length. That is
the opposite of what the technique is for.

**What transfers instead is the incident**, recorded in that suite's own docstring as an
owner escalation dated 2026-07-21: a Jordanian user addressed in hardcoded Gulf dialect
(`"Use natural, professional Gulf Arabic"` had been living in the runtime language rule),
replies rambling with emoji menus. The Core encodes that **behavioural requirement** in
its own tests against its own text. Transfer the failure, not the fixture.

**Rejected:** characterization tests against the pinned SHA, and with them
`PHASE_1_RECONCILIATION.md` §4 item 1's suggestion to write them first.

### 4. Does the response policy own answer length? — **THE PREMISE WAS INCOMPLETE**

The question assumed the policy and the budget might disagree about a number. They
cannot currently disagree, because **neither enforces anything**.

`DEFAULT_GENERATION_RESERVE = 1024` is documented as "room the model needs to write its
answer. Reserved first." **VERIFIED SOURCE FACT:** there is no `max_tokens`, no
`num_predict` and no equivalent anywhere in `src/`; `runtime/ollama/provider.py` sends
`{model, messages, stream}` and caps nothing.

**`generation_reserve` is currently accounting-only and is not enforced by the
provider.** The budget subtracts it during context assembly so evidence cannot crowd out
the answer's room, and no code path passes it to the generation call as a request
option. With no `num_predict` equivalent, nothing constrains how much the model may
write; a completion that exceeds the reserve does so with no Core code observing it.
That is the ADR-005 budget shape -- an unenforced number -- entirely independent of
identity, and recorded here as a reported gap rather than an observed truncation.

**Decision, in three layers that must not be collapsed into one abstraction:**

```text
identity policy      -> tells the model how to respond   (register, concision)
generation budget    -> determines how much it may generate (the number)
provider enforcement -> makes that number real            (missing today)
```

The policy owns the instruction. The budget owns the number. Enforcement does not exist
and is recorded as **prerequisite B** rather than assumed.

## Prerequisites before any implementation

Both touch built contracts, both are small, and **neither is identity work**. Identity
stays untouched until both are settled.

```text
ADR-011 identity implementation
        |
        +-- prerequisite A
        |   ContextAllocation gains an explicit identity share
        |
        +-- prerequisite B
            generation_reserve becomes an enforced provider limit
            (num_predict or equivalent)
        |
        v
    Identity layer implementation
```

**A** is Rule 4 above. **B** is the Q4 finding. Each is its own authorisation and its own
change; answering the four questions authorises neither.

## Scope guardrails

The Q4 finding is a narrow defect, and a narrow defect is not a licence to reshape the
generation path. Nothing here authorises:

- a new provider abstraction
- persistence changes, Neon or pgvector
- model-specific identity templates
- a new session configuration mechanism
- any change to the Boss model

Implementing `identity/` because the four questions are answered would be premature: the
questions were the design gate, not the implementation gate.
