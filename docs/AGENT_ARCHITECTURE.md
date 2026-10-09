# Agent Architecture

**Status: Phase 0 — design. Built since (#64, #67); the deterministic control layer is
ACCEPTED (scoped) by ADR-004 Amendment A1 (owner decision D-C, #87). ADR-004 A2 records
D-A and D-B. `web_search` is HIGH/ASK, as the table below designs network calls. `shell`
is retained at HIGH/ASK under "run command", and it reaches reserved paths once the owner
approves a command. No expansion is authorized.**

---

## 1. Loop

```text
USER
 ↓ UNDERSTAND        intent, language, task shape
 ↓ CONTEXT           retrieve → rank → dedupe → compress → budget
 ↓ PLAN              steps, tools, expected evidence
 ↓ POLICY            allow / deny / ask, per tool and risk level
 ↓ EXECUTE           schema-validated, timed out, audited
 ↓ VERIFY            deterministic checks against the plan's expectations
 ↓ RECOVER           repair, retry, or roll back
 ↓ RESPOND
 ↓ RECORD EVENT      always, success or failure
```

An agent that executes without `POLICY` and `VERIFY` is a tool-calling loop, not an agent.
Both stages are mandatory and neither is bypassable.

## 2. Tool contract

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    risk_level: RiskLevel
    timeout_seconds: int
    idempotent: bool
```

```text
Agent → ToolRequest → PolicyEngine → ALLOW | DENY | ASK
                          ↓
                    Idempotency check
                          ↓
                        Tool
                          ↓
                    ToolResult → Verifier → Audit
```

## 3. Risk levels

**ARCHITECTURAL DECISION.**

| Level | Examples | Default |
|---|---|---|
| `LOW` | read file, search index, list project | auto-allow |
| `MEDIUM` | write file, create branch | allow in workspace, audit |
| `HIGH` | run command, network call | **ask** |
| `CRITICAL` | delete, force-push, external send | **ask**, always audited, rollback point first |

The gate is not advisory. A tool without a declared risk level does not execute.

## 4. Policy layer — the strongest verified asset

**VERIFIED SOURCE FACT.** `unified-llm-local/tool_security.py` @ `21a36b0`: 526 lines,
**zero project-specific references**, ~962 lines of tests across `test_security.py` (1069),
`test_tool_security.py` (295) and `test_sec05_protection.py` (368).

It already provides, in generic form:

```text
validate_command(command, workspace, trusted)   command policy gate
validate_path(file_path, workspace)             path containment
CommandResult · AuditEntry                      structured results
get_audit_log() · _audit()                      audit trail
read/write/delete/rename/copy_file(…, workspace) workspace-scoped tools
run_command_sync(...)                            bounded execution
```

This is the audit's most useful discovery: the policy/audit layer that Rico lacks and that
Robin does not supply already exists here, tested, in the security modules rather than the
retrieval modules the original plan pointed at.

**Classification: ADAPT** — the workspace-scoping model maps onto the Core's tool
sandbox; the contracts are reshaped to the `Tool` protocol above.

Alongside it: `rollback.py` (458 lines, ~796 test lines) → `agent/recovery/`;
`protected_path_policy.py` (319) and `path_security.py` (239) → `agent/policy/`.
`merge_gate.py` / `merge_lock.py` are project-specific git workflow → **DROP**.

## 5. Verifier

**DESIGN TO BUILD.**

**VERIFIED SOURCE FACT.** No source provides a generic verifier. Rico has none. Robin's
`quality_gate.py` (496 lines, 67 video/media references, `cv2`/`ffmpeg`/`_mean_luma`)
validates **video files**, not model output.

What transfers from Robin is the **shape** of a deterministic gate, not its code:

```python
@dataclass(frozen=True)
class VerificationCheck:  name: str; passed: bool; reason: str

@dataclass(frozen=True)
class VerificationResult: passed: bool; checks: list[VerificationCheck]
```

Verification layers, cheapest first:

1. **Schema** — output matches the declared contract.
2. **Grounding** — claims trace to retrieved evidence. Adapted from Rico's
   `EVIDENCE_CONTRACT` / `get_grounding_contract()`.
3. **Policy** — no forbidden action, no leaked secret, no unconfirmed external identity.
4. **Task** — the plan's stated expectations are met.

A failed verification triggers `RECOVER`, never a silent pass.

## 6. Recovery

```text
VERIFY fails → classify → repair (bounded retries)
                        → or roll back to the last safe point
                        → or stop and report
```

Bounded automation, adapted as a pattern from `upload_budget.py`
(`allowed()` → `record()` → `summary()`): every automated action has a budget, and
exhausting it stops the agent rather than letting it loop.

"Stop and report" is a legitimate outcome. Silent failure is not.

## 7. Extraction plan

| From | Take | Class |
|---|---|---|
| `unified-llm-local` `tool_security.py` | policy gate, path containment, audit log | **ADAPT** |
| `unified-llm-local` `rollback.py` | rollback mechanics | **ADAPT** |
| `unified-llm-local` `protected_path_policy.py`, `path_security.py` | path policy | **ADAPT** |
| `Rico` `src/rico_tool_registry.py` (256, **0 tests**) | registry shape | **ADAPT** + characterization first |
| `Rico` `src/rico_identity.py` | `EVIDENCE_CONTRACT`, grounding contract | **ADAPT** (structure only) |
| `Rico` `src/rico_quality.py` (95, **0 tests**) | check concepts | **ADAPT** (concepts) |
| `Rico` `src/rico_agent.py` (296, 24 domain refs, 33 tests) | orchestration reference | **REWRITE/MERGE** |
| `Rico` `src/rico_safety.py` (208, 23 domain refs) | product-coupled | **REWRITE** |
| `Robin` `quality_gate.py` | gate-result shape only | **REWRITE** (concept) |
| — | generic verifier | **BUILD** |

## Proposed opt-in exact file reads (2026-10-10)

Status: review candidate. This control is off by default and has no live Boss
capability result yet. It does not change the existing task contract's meaning:
`[action_required=false]` still permits an answer without executing a tool.

The caller can declare `AgentTaskContract(..., action_required=True,
exact_read_path="notes.txt")`, or use `pac --agent --workspace DIR --exact-read
notes.txt`. Each task line must still begin with `[action_required=true]`.
A contradictory false declaration is a usage error, not silently overridden.

In this mode the task is read-only. Only `read_file`, `list_directory`,
`search_text` and `find_files` may run. The executor audits other requests as
DENY before confirmation or execution, so a model cannot write invented text
into the source and then read it to satisfy the check. The ordinary tool policy
and confirmations remain unchanged when this mode is absent.

Before accepting an answer, the loop requires a successful, verified,
untruncated `read_file` result for the declared path and exact equality between
that text and the answer. It does not trim whitespace or translate text.
Relative path equivalence is lexical using the host's path syntax; filesystem
aliases and symbolic links are not inferred. The existing sandbox, secret
checks, context budget and failure budget still apply. Rejected answers consume
the global failure budget; exhaustion returns no answer. In this CLI mode an
unfinished task also returns exit 1. Rejection events contain counts and reasons,
not file contents or rejected answer text.

This is a check of an observed file snapshot, not a general semantic judge.
It does not establish that outside processes left the file unchanged, make a
large result fit the generation budget, or govern OpenCode's separate tool loop.
For edit/test tasks, the existing benchmark flag `--verify-completion` requires
the task's own test command to pass after the last change; it remains off by default.

### Windows validation before adoption

Use a fresh isolated worktree/venv and scratch workspace, profile and database.
Put that venv's `Scripts` directory first in this process's PATH, so commands
such as `pytest` use the same environment as the caller. Keep the Boss, context
8192, sampling, profile and Ollama launch configuration fixed.

```powershell
$env:PATH = (Join-Path $pacVerifyVenv "Scripts") + ";" + $env:PATH
$env:PYTHONPATH = Join-Path $PWD "src"
"[action_required=true] Read notes.txt and return only its exact contents." |
  & $pacVerifyPython -m personal_ai_core.app --agent --exact-read notes.txt `
    --workspace $pacScratchWorkspace --database $pacScratchDatabase --profile $pacScratchProfile
```

Before running the pilot, use five fresh file contents per language (English and
Arabic), with the same fixtures and true action declaration in the baseline and
candidate. Record code SHA, model digest/quantization, effective context,
environment executable paths, exact input, each tool result, final text and exit
code. The target is zero accepted fabricated copies. Measure useful exact-copy
success separately from budget stops and inspect every refusal for a false one;
do not call refusals a capability improvement. No adoption or reliability claim
follows solely from passing scripted tests. Command-guidance and test-completion
comparisons should be separate so their effects can be attributed.
