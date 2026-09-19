# Phase 1 Reconciliation

**Status: Phase 1 is NOT complete.** Two of its five playbook components are unbuilt.
Nothing here is marked done that is not done.

`ENGINEERING_PLAYBOOK.md` defines Phase 1 as
`core + runtime + identity + conversation + memory foundation`.

| Component | State at `03f2287` |
|---|---|
| core | **built** — domain, contracts, config, errors |
| runtime | **built** — `ModelRegistry`, `ModelProvider`, `OllamaProvider` |
| conversation | **built** — sessions, messages, events, the vertical slice |
| **identity** | **NOT BUILT** — §1 |
| **memory foundation** | **NOT BUILT** — §2 |

Phase 2 has not begun. Phase 2 domain types added in error during this session were
reverted before any commit; the tree at `03f2287` is unchanged.

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

## 2. Missing memory foundation — exact

`memory/` does not exist. `MemoryStore` is a contract plus `SealedMemoryStore`, which
refuses every write. That is a deliberate Phase 1 decision enforcing ADR-003, not an
omission — but it is also not a memory foundation.

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

### Must be built

Event → experience → candidate → promotion gate → memory, with provenance, confidence,
versioning, supersession and rejection retention. No source provides it; this was already
recorded as `DESIGN TO BUILD` in the extraction matrix §4 and this reconciliation confirms
it against a second source.

---

## 3. What this reconciliation does not do

No implementation. No Phase 2. No Neon migration or data change. No source repository
modified. No Phase 0 document modified. No component marked complete.

## 4. Ordered remainder of Phase 1

1. **Identity foundation** — adapt `get_language_rule` and the grounding/integrity
   contracts behind Core-owned interfaces; write characterization tests against Rico at its
   pinned SHA first.
2. **Memory foundation** — build the promotion pipeline against the Core contract, with an
   in-memory store, keeping `SealedMemoryStore` as the guard on the conversation path.
3. **Neon memory adapter** — only after 2, and only once the six missing contract fields
   have an agreed home. A decision, not an implementation detail.

Phase 2 remains unauthorised and unstarted.
