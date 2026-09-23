"""The agent layer -- AGENT_ARCHITECTURE.md.

Built in stages, each a reviewed pull request:

  1. contracts, the policy gate, the workspace sandbox and command validation
  2. tools, the executor and the audit trail
  3. the verifier and bounded recovery
  4. the loop: plan -> policy -> execute -> verify -> respond, in `pac --agent`

Depends on `core` only.
"""
from .commands import CommandRejected, validate_command
from .executor import AuditLog, ToolExecutor
from .policy import DEFAULT_DECISIONS, RiskPolicy
from .recovery import ActionBudget, Checkpoints
from .sandbox import SandboxError, Workspace, is_protected
from .tools import (
    DeleteFile,
    ListDirectory,
    ReadFile,
    RunCommand,
    SearchText,
    WriteFile,
    default_tools,
)
from .verifier import Expectation, Verifier, find_secrets

__all__ = [
    "ActionBudget",
    "AuditLog",
    "Checkpoints",
    "DeleteFile",
    "Expectation",
    "Verifier",
    "find_secrets",
    "CommandRejected",
    "DEFAULT_DECISIONS",
    "RiskPolicy",
    "SandboxError",
    "Workspace",
    "ListDirectory",
    "ReadFile",
    "RunCommand",
    "SearchText",
    "ToolExecutor",
    "WriteFile",
    "default_tools",
    "is_protected",
    "validate_command",
]
