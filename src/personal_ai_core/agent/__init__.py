"""The agent layer -- AGENT_ARCHITECTURE.md.

Built in stages, each a reviewed pull request:

  1. contracts, the policy gate, the workspace sandbox and command validation
  2. tools, the executor and the audit trail
  3. the verifier and bounded recovery
  4. the loop: plan -> policy -> execute -> verify -> respond, in `pac --agent`
  5. reach: the web (search, read a page) and the owner's shell, each asked for
     where it could send data out or change things beyond undo

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
    Shell,
    WriteFile,
    default_tools,
)
from .web import FetchUrl, WebSearch
from .verifier import Expectation, Verifier, find_secrets

__all__ = [
    "FetchUrl",
    "Shell",
    "WebSearch",
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
