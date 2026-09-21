# Phase 1 Reconciliation

**Status: Phase 1 is NOT complete.** One of its five playbook components is unbuilt.
Nothing here is marked done that is not done.

> Written at `03f2287`, when two were unbuilt. Corrected at `bb2e953`, because the
> memory foundation was built in Phase 3 and this document went on saying it was
> missing -- a claim written in one place with nothing watching it go stale, the
> same defect class PRs #8-#13 closed elsewhere. Section 2's source analysis is
> kept: it is still valid research about the two source repositories, and only its
> *status* was wrong. The identity row is unchanged, because it is still true.

`ENGINEERING_PLAYBOOK.md` defines Phase 1 as
`core + runtime + identity + conversation + memory foundation`.

| Component | State at `03f2287` | State at `bb2e953` |
|---|---|---|
| core | **built** | **built** — domain, contracts, config, errors |
| runtime | **built** | **built** — `ModelRegistry`, `ModelProvider`, `OllamaProvider` |
| conversation | **built** | **built** — sessions, messages, events, the vertical slice |
| **identity** | **NOT BUILT** | **NOT BUILT** — §1. `src/personal_ai_core/identity/` does not exist |
| memory foundation | **NOT BUILT** | **built** — Phase 3; §2 |

Verified against `src/` at `bb2e953`, not from the record.

At `03f2287`, Phase 2 had not begun: Phase 2 domain types added in error during that
session were reverted before any commit, and the tree at `03f2287` is unchanged.

Phases 2, 3 and 4 have since been accepted and merged, plus PRs #3-#22 of correction
and hardening on top. See `PROJECT_STATE.md`. **This does not complete Phase 1.** The
later phases were authorised and built over a Phase 1 that is still missing its
identity foundation; that gap is not closed by anything above it.

---

## 1. Missing identity foundation — exact

`identity/` does not exist. The slice sends raw message history with no system prompt, no
grounding contract and no response policy.

Required by `ARCHITECTURE.md` §3: `personality`, `behavioral contract`, `response policy`.

### Adaptable — Rico @ `215c316979731eedcdf2f99bcbd97b727229abf5`

**VERIFIED SOURCE FACT.** `src/rico_identity.py` is 237 lines with 54 domain references and
9 test files. It is a split component: the content is job-search specific, the structure is
generic and is what the Core needs.

| Asset | Lines | Disposition |
|---|---|---|
| `get_language_rule(user_lang)` | ~18 | **ADAPT — highest value** |
| `EVIDENCE_CONTRACT` / `get_grounding_contract()` | ~25 | **ADAPT** |
| `IDENTITY_INTEGRITY_RULE` | ~9 | **ADAPT** |
| `UNTRUSTED_METADATA_RULE` | ~13 | **ADAPT** |
| `SAFETY_CONSTRAINTS_RULE` | ~12 | **ADAPT** (shape) |
| `get_rico_system_prompt()` composition pattern | ~50 | **REWRITE** (structure only) |
| `RICO_IDENTITY` text | ~88 | **DROP** |

`get_language_rule` is the strongest single asset found for the Core's bilingual
requirement. It is domain-neutral, already tested, and enforces Modern Standard Arabic over
regional dialect while forbidding mid-reply language switching — a rule the Core needs and
has no other source for. It is the identity-layer counterpart to ADR-006.

Of the five numbered safety rules in `get_rico_system_prompt`, three transfer in substance
(never fabricate credentials; never act without explicit confirmation; never disclose
secrets) and two are job-listing specific. The *pattern* — non-negotiable numbered rules
embedded in every model call, independent of conversation length — transfers whole.

### Must be built

A `ResponsePolicy` and `BehavioralContract` the Core owns, and prompt composition driven by
the model registry rather than a hard-coded template.

---

## 2. Memory foundation — built in Phase 3

**Was:** *"`memory/` does not exist. `MemoryStore` is a contract plus `SealedMemoryStore`,
which refuses every write. That is a deliberate Phase 1 decision enforcing ADR-003, not an
omission — but it is also not a memory foundation."*

**Now, verified at `bb2e953`:** `src/personal_ai_core/memory/` exists —
`gate.py`, `pipeline.py`, `retriever.py`, `rules.py`. `MemoryRecord`
(`core/memory.py`) carries every field this section recorded as having no home:
`provenance`, `confidence`, `version`, `supersedes`, `status` and `language`.
`MemoryStatus` models the `ACTIVE` / `REJECTED` / `SUPERSEDED` lifecycle, and
`ExperiencePipeline` is the sole writer, statically enforced. `SealedMemoryStore`
remains on the conversation path and still refuses every write — the ADR-003 guard
was kept, not traded away for the foundation.

The source analysis below is unchanged and still stands: it is what was measured
against the two source repositories, and the Core built its own contract rather than
adopting either.

### Neither memory implementation carries the Core contract

**VERIFIED SOURCE FACT.** Measured directly against both sources:

| Contract field | Rico `rico_memory.py` | `unified-llm-local/memory.py` |
|---|---|---|
| `provenance` | absent | **0 occurrences** |
| `confidence` | absent | **0 occurrences** |
| `version` | absent | **0 occurrences** |
| `supersedes` | absent | **0 occurrences** |
| `status` | absent | **0 occurrences** |
| candidate / promotion / rejection | absent | **0 occurrences** |

Rico persists to files via `Path`. `unified-llm-local` persists to Neon. Neither models the
promotion lifecycle. The Core contract in `MEMORY_ARCHITECTURE.md` §3 remains authoritative
and is not replaced by either.

### The Neon substrate — verified

**VERIFIED SOURCE FACT**, read directly from Neon project `misty-sun-80388989`
(`second-brain`, PostgreSQL 18) during this reconciliation. Read-only; nothing was modified.

`public.memory` columns: `id`, `type`, `content`, `project_id`, `embedding`, `created_at`,
`updated_at`, `source_file` (default `'memory/LESSONS.md'`), `lesson_id`, `tags`,
`linked_chunk_id`, `content_hash`.

Indexes include a UNIQUE `content_hash`, a UNIQUE `lesson_id`, and a GIN index on `tags`.

**Reusable as-is:** `content_hash` with its uniqueness constraint (dedup), `embedding`
(semantic memory search), `tags` (GIN), `linked_chunk_id` (the memory↔knowledge link that
maps onto the Core's `links`), `lesson_id` (stable external identity).

**Has nowhere to live in the current schema:** `provenance` (no `session_id`,
`message_id` or `event_id` — `source_file` is file-level only), `confidence`, `version`,
`status`, `supersedes`, and `language`.

**Consequence.** The Neon table is a **substrate, not the Core contract**. An adapter can
read and write it, but six contract fields have no column. Resolving that is an
architectural decision — additive migration, a Core-owned side table, or Core-owned tables
alongside — and it is **not** taken here. No migration is proposed and no Neon object is
touched.

### Adaptable — `unified-llm-local` @ `21a36b0765d89390eed63da094977e4bb8e5b4c2`

| Asset | Disposition | Note |
|---|---|---|
| `_fingerprint(content)` — whitespace-normalised sha256 | **ADAPT** | stable across runs; matches the `content_hash` UNIQUE index already in Neon |
| `auto_learn_from_conversation()` | **ADAPT as a candidate generator** | see below |
| `MemoryManager` | **REWRITE** | file-watcher and Markdown-file model; no promotion lifecycle |
| `MEMORY.md` / `PATTERNS.md` / `LESSONS.md` / `CONTEXT.md` | **evidence** | the memory *taxonomy* is real prior thinking; the file format is not the Core's storage |

**The significant finding.** `auto_learn_from_conversation(user_msg, assistant_msg,
tool_results)` **returns** a list of lessons — it does not persist them. It is already a
candidate generator, not a memory writer, which makes it compatible with the Core's
`candidate → promotion gate → memory` pipeline rather than a violation of it.

Its two heuristics map directly onto rules already specified in `MEMORY_ARCHITECTURE.md` §4:

- a failing tool result → a lesson candidate
- correction markers in the user message → a preference candidate (`correction` rule)

**Limitation to carry forward:** the correction markers are English-only —
`"actually"`, `"no, "`, `"should be"`, `"prefer"`. For a bilingual Core this silently
produces no Arabic candidates. Same failure shape as ADR-006: it does not error, it
under-detects.

### Built (was: must be built)

Event → experience → candidate → promotion gate → memory, with provenance, confidence,
versioning, supersession and rejection retention. No source provided it; it was recorded
as `DESIGN TO BUILD` in the extraction matrix §4, and Phase 3 built it against the Core
contract as specified.

**One limitation carried forward and still open.** The English-only correction markers
noted above did carry forward: `memory/rules.py` states that `CorrectionRule` inspects
English markers only, and that Arabic corrections are therefore not detected. That module
records the gap rather than papering over it — `ExplicitInstructionRule` does handle both
languages — but the gap is real and unclosed.

---

## 3. What this reconciliation does not do

No implementation. No Phase 2. No Neon migration or data change. No source repository
modified. No Phase 0 document modified. No component marked complete.

## 4. Ordered remainder of Phase 1

1. **Identity foundation** — **STILL OPEN.** Adapt `get_language_rule` and the
   grounding/integrity contracts behind Core-owned interfaces. The prior instruction to
   write characterization tests against Rico at its pinned SHA first is **superseded**:
   ADR-011, design question 3, resolved **NO** — the Core owns its own text, so those
   tests would pin strings it will never use. The incident transfers, not the fixture.
   This is the whole of what remains of Phase 1.
2. **Memory foundation** — **DONE (Phase 3).** The promotion pipeline was built against
   the Core contract with an in-memory store, and `SealedMemoryStore` was kept as the
   guard on the conversation path, as specified here.
3. **Neon memory adapter** — **STILL OPEN, and now governed by ADR-010.** That ADR
   compares four persistence options and selects none; the six-fields-have-no-column
   problem recorded in §2 is one of its inputs. No option is selected, no schema exists,
   and no Neon object has been touched.

Phase 2 was subsequently authorised, built and accepted, as were Phases 3 and 4.
