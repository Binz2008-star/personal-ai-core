"""The agent layer -- AGENT_ARCHITECTURE.md.

Built in stages, each a reviewed pull request:

  1. contracts, the policy gate, the workspace sandbox and command validation
  2. tools, the executor and the audit trail (this package so far)
  3. the verifier and bounded recovery
  4. the loop: plan -> policy -> execute -> verify -> respond, in `pac`

Depends on `core` only.
"""
from .commands import CommandRejected, validate_command
from .executor import AuditLog, ToolExecutor
from .policy import DEFAULT_DECISIONS, RiskPolicy
from .sandbox import SandboxError, Workspace, is_protected
from .tools import (
    ListDirectory,
    ReadFile,
    RunCommand,
    SearchText,
    WriteFile,
    default_tools,
)

__all__ = [
    "AuditLog",
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
