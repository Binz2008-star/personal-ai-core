# Personal AI Core

An AI operating layer for a local model. Memory, knowledge, experience, learning, tools and
projects are separate layers with their own contracts; the model is a replaceable backend.

## Run it

You need a model server. The Boss model is `huihui_ai/qwen2.5-abliterate:7b` on Ollama at
`http://127.0.0.1:11434` unless you say otherwise.

```console
$ pip install -e .
$ pac
model:   huihui_ai/qwen2.5-abliterate:7b
storage: /home/you/.personal-ai-core/core.db
session: 4f3a…
         continue this later with --session 4f3a…

you> what did we decide about the budget?
core> …
```

`python -m personal_ai_core.app` does the same without installing.

**The conversation is still there tomorrow.** It is kept in one SQLite file — no server, no
daemon and no new dependency, because `sqlite3` ships with Python. Pass `--session` from an
earlier run to continue it.

| | |
|---|---|
| `--database PATH` | where the file lives. Also `$PAC_DATABASE`; the flag wins |
| `--ephemeral` | keep nothing — the conversation ends with the process |
| `--session ID` | continue an earlier conversation |
| `--language ar` | tag the turn. Left undetermined when not given, because guessing it would record a claim nothing measured |
| `--documents PATH` | answer from a file, or a directory of `.md` and `.txt` files. Repeatable |
| `--agent --workspace DIR` | each line is a task the agent carries out inside `DIR` |
| `--profile PATH` | a Markdown file about you. Also `$PAC_PROFILE`; defaults to `profile.md` beside the database |
| `--remember "..."` | add one line to your profile, and exit |

**It knows who it works for.** Write about yourself in `profile.md`, next to the database
(`~/.personal-ai-core/profile.md` by default): your work, your projects, your goals, how you
like answers. It is read into every conversation and every agent task, in every session.
It stays a file you can open and edit. `pac --remember "I prefer answers in Arabic"` adds a
line without opening it. The profile is capped at 8000 characters, because it is sent with
every turn.

Configuration is environment variables, all optional: `PAC_BOSS_MODEL`,
`PAC_BOSS_CONTEXT_WINDOW`, `PAC_OLLAMA_HOST`, `PAC_REQUEST_TIMEOUT_SECONDS`, `PAC_DATABASE`.

**Retrieval: the conversation is kept, the corpus is re-read.** With `--documents`, each
turn is grounded in passages from those files, cited by file URI and character range.
Knowledge is derived data, rebuilt by re-ingestion (ADR-010 R4), so the documents are read
again on every run and held in memory only. Continue a session without `--documents` and
the conversation is there, but nothing is retrieved. Retrieval is lexical plus a hashing
embedder: it matches surface overlap, not meaning. There is no semantic model yet.

```console
$ pac --documents ~/notes
documents: 12 file(s), 31 chunk(s) -- held in memory, read again on every run
```

**The agent: it acts, inside one directory, and asks before anything risky.** With
`--agent --workspace DIR`, each line you type is a task. The model proposes one tool call
at a time; the policy gate, the sandbox and the verifier decide what happens
(`docs/AGENT_ARCHITECTURE.md`).

| Tool | Risk | What happens |
|---|---|---|
| `read_file`, `list_directory`, `search_text` | low | runs; secrets, `.git` and pac's own database are refused |
| `write_file` | medium | runs; never a secret; can be undone |
| `run_command` | high | **asks you**, every time; an allowlist of read-only and checking commands, no shell |
| `delete_file` | critical | **asks you**, every time; can be undone |

```console
$ pac --agent --workspace ~/projects/notes
you> summarise notes.md into summary.md
  · read_file {"path": "notes.md"} -> ok
  · write_file {"path": "summary.md", ...} -> ok
core> I wrote the summary to summary.md.
         changed: summary.md
```

A task that stops -- too many failed steps, or a model that stops following the protocol --
offers to undo its file changes. Every step, allowed or not, is recorded in the database,
and the database itself is out of the agent's reach even when the workspace contains it.
A `--session` that does not exist is refused, as it is for a conversation.

The agent has no network tool: none of its tools sends anything anywhere. The model does
see what the agent reads -- file contents and command output go into the conversation
with the model at `PAC_OLLAMA_HOST`. That host is `127.0.0.1` by default; point it at
another machine and what the agent reads travels there.

**Status: Phase 4 — Memory-Aware Context Recall ACCEPTED / MERGED (PR #2 — MERGED, main `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`, implementation `e8062ff2fa8b6eb5a4471ac8475f29bed76fd369`).**

Phases:
- **Phase 0** — HISTORICAL / COMPLETED — core source audit, evidence freeze, extraction matrix (no separate gate; see [`docs/COMPONENT_EXTRACTION_MATRIX.md`](docs/COMPONENT_EXTRACTION_MATRIX.md))
- **Phase 1** — COMPONENTS COMPLETE / NOT ACCEPTED — core foundation vertical slice
  (User → Session → Message → ModelProvider → Response → Event).
  All five playbook components are built; identity was the last and arrived in PR #39.
  The phase is still not **accepted** — that gate ends with the owner, and the two are
  different states. See [`PROJECT_STATE.md`](PROJECT_STATE.md).
- **Phase 2** — ACCEPTED (`0a8d7986c4d6a0281f8e8d7f0f2c1c2a8d3fe511`)
  Knowledge & context foundations. In-memory contracts, retrieval, budgeting.
  Tests: 389 passed / 14 skipped.
- **Phase 3** — ACCEPTED / MERGED (`f090100e933d1a6ff18d6e546b384b2e727b2889` merge of `f44de80a5a31e079dbdef4ab7f173d40bd3bc15e` + `ac41d2799c4698960f63c00d024010d1e45f1b18`)
  Memory domain + promotion + persistence contracts & write path.
  Tests: 443 passed / 14 skipped.
- **Phase 4** — ACCEPTED / MERGED (`e8062ff2fa8b6eb5a4471ac8475f29bed76fd369` → `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`, branch `claude/phase-4-memory-aware-context`, PR #2 — MERGED)
  Session-scoped MemoryReader, deterministic retrieval, MemoryRetrievalError, hybrid document/memory context, shared token budget, Grounding (`memory_enabled`), degraded failure, ADR-009.
  Tests: 494 passed / 14 skipped. pyright: 0 errors. Architectural review: PASS. Post-commit audit: PASS.
- **Phase 5** — NOT AUTHORIZED / DESIGN NOT STARTED — UNAUTHORIZED / FUTURE DESIGN (no cross-session recall approval; no contract)

## Start here

| Document | What it holds |
|---|---|
| [`docs/COMPONENT_EXTRACTION_MATRIX.md`](docs/COMPONENT_EXTRACTION_MATRIX.md) | the evidence spine — what exists in each source, verified, with classifications |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | boundaries, modules, dependency direction, invariants |
| [`docs/ENGINEERING_PLAYBOOK.md`](docs/ENGINEERING_PLAYBOOK.md) | how components get extracted; git governance; definition of done |
| [`docs/MEMORY_ARCHITECTURE.md`](docs/MEMORY_ARCHITECTURE.md) | `Event != Memory`, promotion, provenance |
| [`docs/LEARNING_ARCHITECTURE.md`](docs/LEARNING_ARCHITECTURE.md) | event → experience → candidate → evaluation → promotion |
| [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) | agent loop, tool contracts, risk levels, verifier |
| [`docs/MODEL_ARCHITECTURE.md`](docs/MODEL_ARCHITECTURE.md) | registry, providers, Boss model, replacement |
| [`docs/PROJECT_INTEGRATION.md`](docs/PROJECT_INTEGRATION.md) | projects as connectors, anti-corruption layer |
| [`docs/TESTING_STRATEGY.md`](docs/TESTING_STRATEGY.md) | characterization first, six levels, multilingual |
| [`docs/ADR/`](docs/ADR/) | decisions with their evidence |
| [`docs/PHASE_2_CONTRACT.md`](docs/PHASE_2_CONTRACT.md) | Phase 2 knowledge & context contracts |
| [`docs/PHASE_2_IMPLEMENTATION.md`](docs/PHASE_2_IMPLEMENTATION.md) | Phase 2 implementation details |
| [`docs/PHASE_1_RECONCILIATION.md`](docs/PHASE_1_RECONCILIATION.md) | Phase 1 reconciliation notes |

## Reading the labels

Every claim is labelled, because several inherited assumptions did not survive the audit:

- **VERIFIED SOURCE FACT** — read from the source at a recorded SHA
- **ARCHITECTURAL DECISION** — a choice made in response to evidence
- **DESIGN TO BUILD** — no adequate source exists
- **DEFERRED / UNVERIFIED** — not inspected, or inspected structurally only

A filename, a README claim or a line count is not evidence.

## Boss model

```text
huihui_ai/qwen2.5-abliterate:7b
```

Operationally primary, architecturally replaceable, configured in the model registry and
never named in business logic. Distinct from `local-llm-rig`'s benchmark results — see
[`ADR-002`](docs/ADR/ADR-002-boss-model.md).

## Relationship to `local-llm-rig`

`local-llm-rig` is a separate repository owning the model runtime, Modelfiles, benchmarks
and hardware evidence. This Core sits above it and consumes its measurements as evidence.

## Request timeout and cold starts

The first `/api/chat` request after a reboot or an unloaded model can exceed the configured
`DEFAULT_REQUEST_TIMEOUT_SECONDS` (`120`), because the model has to be loaded into VRAM before
the first token and that load does not count as prompt processing. Observed live on a Windows
rig: the first request after a cold start timed out at the configured `120`s; the identical
request completed instantly once the model was resident.

The knob is the environment variable `PAC_REQUEST_TIMEOUT_SECONDS` (read at runtime by the
Ollama provider, no code change needed), e.g. `600` for a cold start. The `120`s default is
deliberately unchanged — raising it is a configuration decision the project tracks, not an
assumption to bake in silently.

## Architectural invariants (hard)

1. **Event != Memory** — conversations create events; memories are promoted
2. **Dependency direction** — memory → core, knowledge → core, context → core, conversation → core
3. **Boss model** — `huihui_ai/qwen2.5-abliterate:7b` (config only)
4. **No dead enum members** — every `RetrievalMethod` / `ExclusionReason` has a producer
5. **SealedMemoryStore** — remains sealed until explicit authorization
6. **No Neon/pgvector/Postgres/migrations** — without explicit owner authorization
7. **Source repositories never modified** — Rico, unified-llm-local, second-brain-kb are read-only

## Phase progression

```
Phase 0: source audit + evidence freeze + extraction matrix
         ↓
Phase 1: core foundation vertical slice        (PARTIAL — never closed)
         ↓
Phase 2: knowledge + context foundations
         ↓
Phase 3: memory domain + promotion + persistence contracts + write path
         ↓
Phase 4: memory READ/RECALL enrichment of conversation context (session-scoped)
         ↓
Phase 5: NOT AUTHORIZED
```

Explicit distinction:
- **Event** → conversation/event path (append-only evidence)
- **Memory** → persistent memory path (promotion gate only)
- **Memory recall** → context enrichment path (session-scoped, read-only enrichment)

## Current limitations (accepted)

- Phase 4 recall is **session-scoped** — no cross-session recall
- No semantic embedding-based memory ranking
- Memory retrieval is enrichment/degradation, not a write path
- Evidence is charged at its rendered cost (F-4, #52). The provider's own chat-template
  tokens are still an estimate inside the fixed `DEFAULT_OVERHEAD`
- `pac` retrieves only from documents named with `--documents` on that run. The corpus is
  not stored, and chunk ids in recorded events refer to the run that produced them
- Retrieval uses a hashing embedder and a lexical index, with no semantic model; memory
  recall is not wired into `pac`
- No Neon, no pgvector, no server database and no migrations. The durable store is one
  SQLite file and `sqlite3` is stdlib, so the project still has no runtime dependencies

## Current state

Phase 4 (PR #2) — **MERGED** to `main` at `bbbf4c30aad8c7064d920a68249ddd8e9bd2d43a`. Phase 5 — NOT AUTHORIZED / FUTURE DESIGN.
