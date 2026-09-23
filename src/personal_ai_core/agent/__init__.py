"""The agent layer -- AGENT_ARCHITECTURE.md.

Built in stages, each a reviewed pull request:

  1. contracts, the policy gate, the workspace sandbox and command validation
     (this package so far)
  2. tools, the executor and the audit trail
  3. the verifier and bounded recovery
  4. the loop: plan -> policy -> execute -> verify -> respond, in `pac`

Depends on `core` only.
"""
from .commands import CommandRejected, validate_command
from .policy import DEFAULT_DECISIONS, RiskPolicy
from .sandbox import SandboxError, Workspace, is_protected

__all__ = [
    "CommandRejected",
    "DEFAULT_DECISIONS",
    "RiskPolicy",
    "SandboxError",
    "Workspace",
    "is_protected",
    "validate_command",
]
