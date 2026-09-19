# Agent Architecture

**Status: Phase 0 — design.**

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
