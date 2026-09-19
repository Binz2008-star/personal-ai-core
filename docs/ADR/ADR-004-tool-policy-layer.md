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
