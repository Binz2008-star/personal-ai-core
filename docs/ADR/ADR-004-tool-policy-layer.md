# ADR-004 — Tool policy, audit and rollback adapted from `unified-llm-local`

**Status:** Accepted · Phase 0

## Context

The agent needs a policy gate, path containment, an audit trail and rollback. The original
plan expected Robin to supply verification and Rico to supply tool safety.

**VERIFIED SOURCE FACT.** Robin's `quality_gate.py` (496 lines, 67 video/media references,
`cv2`/`ffmpeg`) validates video files, not model output. Rico's `rico_tool_registry.py`
(256 lines) has **no tests**.

**VERIFIED SOURCE FACT.** `unified-llm-local` @ `21a36b0` `tool_security.py` is 526 lines
with **zero project-specific references** and ~962 lines of tests, providing
`validate_command(command, workspace, trusted)`, `validate_path(file_path, workspace)`,
`CommandResult`, `AuditEntry`, `get_audit_log()` and workspace-scoped file tools.
Alongside it, `rollback.py` (458 lines, ~796 test lines) and `protected_path_policy.py`
(319 lines).

The policy layer the plan sought exists — in the security modules, not the retrieval
modules the plan pointed at.

## Decision

Adapt `tool_security.py`, `rollback.py`, `protected_path_policy.py` and `path_security.py`
as the basis of `agent/policy/`, `agent/tools/` and `agent/recovery/`. Reshape their
contracts to the Core `Tool` protocol; keep the workspace-scoping and audit model.

`merge_gate.py` and `merge_lock.py` are project-specific git workflow: **DROP**.

## Consequences

The most security-sensitive layer starts from tested code rather than new code. Its
workspace model becomes the Core's sandbox boundary, which must be verified to fit the
agent's needs rather than assumed.

The generic **verifier** remains `DESIGN TO BUILD`; only Robin's gate-result *shape*
transfers.

---

## Amendment A1 — Scoped acceptance of the Agent deterministic control layer

**Amendment status:** ACCEPTED (scoped) · owner decision D-C · docs only
**Reviewed at:** `main` @ `fee1890927b5a8055e4852be8575eefece2670ef`
**Scope:** the Agent deterministic control layer, **excluding `web_search` and `shell`**
**Unresolved:** D-A (`web_search`) and D-B (`shell`), both OWNER DECISION REQUIRED
**Changes nothing in:** `src/`, `tests/`, schema, migrations, configuration, Agent behaviour

### A1.1 Why this amendment exists

ADR-004 (Accepted, Phase 0) decided the *basis* of the policy, tools and recovery layers.
It did not accept an implementation, because none existed. It left the verifier as
`DESIGN TO BUILD` and required that the workspace model "be verified to fit the agent's
needs rather than assumed".

The Agent has since been built and merged (#64 `91796e8`, #67 `36bfa47`), and until this
amendment no accepted record covered it. This amendment:

- records what was built;
- records the owner's acceptance (D-C) of the **deterministic control layer only**;
- excludes the two capabilities whose design status is unresolved;
- states what the acceptance does not cover.

The implementation merged before an acceptance record existed. This amendment records that
gap; it does not retroactively approve it.

This amendment does **not** accept the whole Agent.

### A1.2 What exists on `main` @ `fee1890`

| Component | Location | Summary |
|---|---|---|
| Contracts | `core/agent.py`, `core/domain.py` | `RiskLevel`, `Decision`, `AuditRecord`; `EventType.AGENT_STEP`, `EventType.AGENT_FINISHED` |
| Policy | `agent/policy.py` | `RiskPolicy`, `DEFAULT_DECISIONS`: LOW/MEDIUM → ALLOW, HIGH/CRITICAL → ASK |
| Command validation | `agent/commands.py` | `validate_command` and its allowlist; characterized against `21a36b0` |
| Sandbox | `agent/sandbox.py` | `Workspace` containment; the database file and its SQLite companion files are reserved |
| File and command tools | `agent/tools.py` | `read_file`, `list_directory`, `search_text` (LOW); `write_file` (MEDIUM); `delete_file` (CRITICAL, checkpointed); `run_command` (HIGH, allowlisted, no shell) |
| Shell | `agent/tools.py` | `shell` (HIGH): arbitrary command, `shell=True`, environment stripped of secret-named variables |
| Web tools | `agent/web.py` | `web_search` (MEDIUM); `fetch_url` (HIGH); urllib; text capped at 20 000 characters |
| Executor | `agent/executor.py` | `ToolExecutor`, in-memory `AuditLog`, `executed` flag |
| Verifier | `agent/verifier.py` | Post-action verification, `SECRET_PATTERNS` |
| Recovery | `agent/recovery.py` | `ActionBudget` (at most 12 actions, at most 3 failures), `Checkpoints` |
| AgentLoop | `agent/loop.py` | Plan → policy → execute → verify → recover → respond → record event; `session_exists` enforced |
| Wiring | `conversation/factory.py::build_agent` | Model from `registry.active.name`; database reserved; default tools, `shell` and web tools registered |
| CLI | `app/cli.py` | `pac --agent --workspace DIR`; confirmation prompts; undo on stop; unknown session exits with code 2 |

### A1.3 Built risk levels against the documented design

The design is the risk table in `docs/AGENT_ARCHITECTURE.md`:

- **LOW:** auto-allow.
- **MEDIUM:** allow within the workspace, with audit.
- **HIGH:** ask. This covers running a command and any network call.
- **CRITICAL:** ask, and set a rollback point first.

| Tool | Built level → decision | Consistent with the design? |
|---|---|---|
| `read_file`, `list_directory`, `search_text` | LOW → ALLOW | Yes |
| `write_file` | MEDIUM → ALLOW (workspace, audited) | Yes |
| `delete_file` | CRITICAL → ASK, checkpointed | Yes |
| `run_command` | HIGH → ASK, allowlisted | Yes |
| `fetch_url` | HIGH → ASK (network call) | Yes |
| `web_search` | MEDIUM → ALLOW (network call) | **No: see D-A** |
| `shell` | HIGH → ASK | **Not in the design: see D-B** |

This comparison was made by reading the code, not by running it.

### A1.4 Status by component

These five statuses are distinct and must not be conflated:

- **IMPLEMENTED:** the code exists on `main`.
- **TESTED:** deterministic tests exercise it.
- **COMMITTED:** it was merged through a PR with green CI.
- **ACCEPTED:** a governance record accepts it.
- **AUTHORIZED FOR FURTHER EXPANSION:** new capability may be built on it.

| Component | IMPLEMENTED | TESTED | COMMITTED | ACCEPTED | AUTHORIZED FOR FURTHER EXPANSION |
|---|---|---|---|---|---|
| Core contracts | Yes | Yes | Yes | Yes (D-C) | No |
| `AGENT_STEP` / `AGENT_FINISHED` emission | Yes | Yes | Yes | Yes (D-C) | No |
| Policy | Yes | Yes | Yes | Yes (D-C) | No |
| Command validation | Yes | Yes (with characterization) | Yes | Yes (D-C) | No |
| Sandbox | Yes | Yes | Yes | Yes (D-C) | No |
| Reserved-database protection for file tools | Yes | Yes (with mutation evidence) | Yes | Yes (D-C) | No |
| File tools and `run_command` (not `shell`) | Yes | Yes | Yes | Yes (D-C) | No |
| Executor and audit | Yes | Yes | Yes | Yes (D-C) | No |
| Verifier | Yes | Yes | Yes | Yes (D-C) | No |
| Recovery | Yes | Yes | Yes | Yes (D-C) | No |
| AgentLoop control behaviour | Yes | Yes | Yes | Yes (D-C) | No |
| `pac --agent` wiring | Yes | Yes (integration) | Yes | Yes (D-C) | No |
| `fetch_url` | Yes | Yes | Yes | Yes (D-C); its HIGH/ASK matches the design | No |
| `web_search` | Yes | Yes | Yes | **No. Excluded; BLOCKED pending D-A** | No |
| `shell` | Yes | Yes | Yes | **No. Excluded; BLOCKED pending D-B** | No |
| Model-driven behaviour | Not evaluated | No | — | **No. Excluded** | No |
| Runtime context behaviour | Not measured | No | — | **No. Excluded** | No |

D-C is independent of D-A and D-B:

- D-A blocks only `web_search`.
- D-B blocks only `shell`.
- Neither blocks any other component.

### A1.5 Evidence

**Tests.** `tests/unit/test_agent_*.py` (9 files), `tests/integration/test_cli_agent.py`,
and `tests/characterization/`, whose source is pinned by SHA-256 to `unified-llm-local` @
`21a36b0`.

**Mutation.** 19 remediation mutants were killed for findings F-1 to F-4.

**CI.** Run #202 on `fee1890` passed `static` (ruff, pyright), `suite` (including the
dependency-direction and Event ≠ Memory checks) and `suite-windows`.

This evidence covers **deterministic control behaviour only**:

- policy decisions;
- containment;
- command validation;
- reserved-path protection for file tools;
- audit;
- verification;
- budget, checkpoints and undo;
- event emission;
- CLI wiring.

It does not cover the following:

- **Model-driven behaviour has not been evaluated.** There is no evidence about the Boss
  model's planning, tool choice or answer quality inside the loop.
- **ADR-013 (evaluation harness) remains PROPOSED and unbuilt.**
- **Runtime context behaviour is unmeasured.** Agent events record neither
  `prompt_tokens` nor tool-output sizes, so Agent context remains MEASUREMENT
  INSUFFICIENT. No measurement is implied, and this amendment authorizes no
  instrumentation.

This amendment is not a production behavioural acceptance of the Agent and must not be
cited as one.

### A1.6 Hard invariants (unchanged)

The following were confirmed by reading the code at `fee1890`:

1. **Event ≠ Memory.** The Agent writes only through `EventRepository.append`. It has no
   path to memory stores, SQLite adapters or `SealedMemoryStore`.
2. **Dependency direction.** memory→core, knowledge→core, context→core, learning→core.
   `agent/` imports only `core.agent`, `core.contracts`, `core.domain` and the standard
   library. No lower layer imports a higher one.
3. **Boss model.** It remains exactly `huihui_ai/qwen2.5-abliterate:7b` and is never
   substituted. The Agent takes its model from `registry.active.name` and hard-codes
   none.
4. **No dead enum members.** `AGENT_STEP` and `AGENT_FINISHED` each have exactly one
   producer (`agent/loop.py`), and every `RiskLevel` and `Decision` member is used. This
   amendment adds no enum, event, schema or memory contract.
5. **`SealedMemoryStore` boundary.** Unchanged. It remains the protected memory-writing
   boundary on the conversation path, and the Agent path does not touch it.
6. **Persistence.** No Neon, Postgres, pgvector, schema or migration change was made, and
   none is authorized.
7. **Legacy source repositories are immutable.** The `21a36b0` lineage is read-only and
   pinned by hash.

Two indirect paths are recorded here; they are not classified as defects:

- Reserved-path protection applies to the **file tools**. It does not apply to `shell`
  once the user has approved a command (D-B).
- `web_search` makes outbound network calls without asking (D-A).

### A1.7 Unresolved decisions (OWNER DECISION REQUIRED)

This amendment takes no side on either decision. It does not change the implementation to
match the design, and it does not adopt the implementation as the design. Each decision
blocks only the capability it names.

**D-A: `web_search` risk level.** The design classifies network calls as HIGH/ASK. The
implementation classifies `web_search` as MEDIUM, which `DEFAULT_DECISIONS` resolves to
ALLOW, so it makes outbound network calls with no per-call confirmation.
Status: **`web_search` is BLOCKED. OWNER DECISION REQUIRED.**

**D-B: `shell` tool.** The design's command concept is an allowlisted `run_command` with
no shell. The implementation also provides `shell`. It accepts an arbitrary command, runs
it with `shell=True`, is classified HIGH/ASK and removes secret-named environment
variables. Once the user approves a command, it can read or modify any file the process
can reach, including the database that is reserved from the file tools.
Status: **`shell` is BLOCKED. OWNER DECISION REQUIRED.**

Recording either decision may require a separately authorized change. This amendment
authorizes none.

### A1.8 Documentation drift (recorded, not reconciled here)

The following documents describe the Agent as unbuilt or differ from `fee1890`:

- `docs/AGENT_ARCHITECTURE.md`
- `docs/ENGINEERING_PLAYBOOK.md`
- this ADR's original body (planned `agent/policy/`, `agent/tools/` and
  `agent/recovery/` packages; verifier `DESIGN TO BUILD`)
- ADR-011
- ADR-012
- `PROJECT_STATE.md`

Reconciling them is separate docs-only work. The parts that describe risk levels wait for
D-A and D-B. The original body of this ADR is left unchanged by design.

### A1.9 Out of scope

This amendment does not cover or authorize any of the following:

- observation or learning implementation (ADR-017 remains PROPOSED);
- reflection, goal decomposition, scratchpad or summarization;
- any context-efficiency or context-optimization change;
- evaluation implementation (ADR-013 remains PROPOSED);
- training;
- Postgres or Neon work;
- any change to Agent behaviour;
- any new tool, risk level, enum, event, schema or memory contract;
- any expansion of the Agent.

### A1 errata — S-1 wording correction (owner-accepted S-1 ruling)

§A1.6 item 5 reads: "It remains the protected memory-writing boundary on the
conversation path." That wording is inaccurate and is corrected here; the text above is
left unchanged as the accepted record. `SealedMemoryStore` has never been on the
conversation path. `ConversationService` has no memory collaborator, and that absence is
what enforces Event ≠ Memory (ADR-003). The item should read: "Unchanged.
`ConversationService` has no memory collaborator, `SealedMemoryStore` remains the
refusing contract double, and the Agent path touches neither."

This erratum changes no acceptance boundary, no invariant and no code.
