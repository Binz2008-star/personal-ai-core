# Memory Architecture

**Status: Phase 0 — design. DESIGN TO BUILD unless a section says otherwise.**

---

## 1. The invariant: `Event != Memory`

**ARCHITECTURAL DECISION, forced by a VERIFIED SOURCE FACT.**

Rico has **no event module** in `src/`. `rico_memory.py` exposes `add_memory(...)`, which a
conversation turn can call directly. A user saying *"I prefer X"* becomes a stored memory
with no intermediate step. That is the defect this architecture exists to prevent: memory
written at conversation speed is memory polluted at conversation speed.

```text
conversation turn
      ↓
   EVENT              immutable, append-only, always recorded
      ↓
  EXPERIENCE          event + outcome + feedback, normalized
      ↓
 MEMORY CANDIDATE     proposed, not yet trusted
      ↓
  PROMOTION GATE      rules + confidence threshold
      ↓
PERSISTENT MEMORY     provenance, confidence, version, status
```

No path writes persistent memory except the promotion gate.

## 2. Memory types

| Type | Holds | Lifetime |
|---|---|---|
| `working` | current task state | session |
| `episodic` | what happened, when | long, decayable |
| `semantic` | facts the system believes | long |
| `preferences` | how the user wants things done | long, versioned |
| `decisions` | choices made and their reasons | permanent |
| `lessons` | what failed and what was learned | permanent |
| `patterns` | recurring structures observed | long |

`decisions` and `lessons` are never silently overwritten — they are superseded (§5).

## 3. Record contract

Every persistent memory:

```json
{
  "id": "uuid",
  "type": "preference",
  "content": "...",
  "language": "ar|en|und",
  "source": "conversation|document|observation|import",
  "confidence": 0.94,
  "status": "active|superseded|rejected|expired",
  "version": 3,
  "supersedes": "uuid|null",
  "provenance": {
    "session_id": "...",
    "message_id": "...",
    "event_id": "...",
    "promoted_by": "rule:explicit_preference",
    "promoted_at": "..."
  },
  "links": ["uuid"],
  "created_at": "...",
  "updated_at": "..."
}
```

**VERIFIED SOURCE FACT.** The audited `rico_memory.py` (343 lines, 17 test files, 1 domain
reference) carries **none** of `provenance`, `confidence`, `version`, `status` or
`supersedes`, and persists to files via `Path`. Its *interface* is the cleanest in any
source and is being adapted; its *storage model* is replaced entirely.

## 4. Promotion

**DESIGN TO BUILD.** No source implements this.

A candidate is promoted when a rule fires and confidence clears the type's threshold.
Rules are explicit and auditable — never "the model decided to remember".

| Signal | Rule | Starting confidence |
|---|---|---|
| Explicit statement ("remember that…") | `explicit_instruction` | high |
| Correction of a prior answer | `correction` | high |
| Repeated behaviour across sessions | `repetition` (needs N≥3) | medium, grows |
| Single inferred preference | `inference` | low — candidate only |
| Contradicts an active memory | `conflict` → supersession review | held |

Rejected candidates are **retained** with `status: rejected`. A rejection is evidence, and
re-proposing the same candidate repeatedly is itself a signal.

## 5. Versioning and supersession

Memories are never destructively updated. A change writes a new version and marks the old
one `superseded`, linked by `supersedes`. This preserves *when the system believed what* —
required to explain past behaviour and to replay evaluations honestly.

Conflict between two active memories is not resolved by recency alone: the higher-confidence
record wins, and an unresolved conflict is surfaced rather than silently collapsed.

## 6. Retrieval boundaries

**ARCHITECTURAL DECISION.**

```text
Memory  →  MemoryRepository  →  RetrievalService  →  vector / lexical
```

Memory never touches a vector store or SQL driver directly. This keeps the storage engine
replaceable and keeps one retrieval path shared with knowledge and with evaluation.

Memory retrieval is **language-aware**: a stored Arabic preference must be retrievable from
an Arabic query. The audited lexical search is English-hardcoded and cannot be inherited —
see `ADR/ADR-006`.

## 7. What memory must never do

- Be written directly by a conversation turn.
- Be baked into model weights.
- Store a fact without provenance.
- Be destructively overwritten.
- Hold project-specific business records — those belong to connectors.

## 8. Extraction plan

| From | Take | Class |
|---|---|---|
| `Rico src/rico_memory.py` @ `215c3169` | interface shape: profile, chat history, `add_memory`, `search_memories`, `summarize_recent_memory`, context get/set | **ADAPT** |
| `Rico` | file-backed persistence | **rejected** — replaced by the §3 contract |
| `unified-llm-local` @ `21a36b0` | pgvector/HNSW schema pattern for memory embedding | **ADAPT** |
| — | event, experience, candidate, promotion, supersession | **BUILD** |

Characterization tests are written against `rico_memory.py` at its recorded SHA before any
extraction.
