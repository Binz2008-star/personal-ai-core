"""Agent domain types -- AGENT_ARCHITECTURE.md sections 2, 3 and 5.

Types only. The policy engine, the sandbox and the tools live in `agent/`; this
module is what they and their callers agree on, so it depends on nothing but
the standard library.

Two rules from the design are enforced by construction here rather than by
convention:

- **A tool without a declared risk level does not execute** (section 3). A
  `ToolSpec` cannot be built without a `RiskLevel`, so a tool that has not
  declared one cannot reach the policy engine at all.
- **An agent that executes without POLICY is a tool-calling loop** (section 1).
  A `PolicyDecision` is the only thing an executor may act on, and ASK is a
  decision of its own -- not a flavour of ALLOW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .domain import new_id, utcnow


class RiskLevel(str, Enum):
    """What a tool can do to the world, and so how much permission it needs.

    Defaults, from AGENT_ARCHITECTURE.md section 3:

    | Level    | Examples                                  | Default        |
    |----------|-------------------------------------------|----------------|
    | LOW      | read file, search index, list project     | auto-allow     |
    | MEDIUM   | write file, create branch                 | allow in workspace, audit |
    | HIGH     | run command, network call                 | ask            |
    | CRITICAL | delete, force-push, external send         | ask, always audited, rollback point first |
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    # Not an ALLOW that logs louder: the executor must stop and obtain the
    # user's explicit confirmation in this turn (BehavioralContract rule 2).
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What a tool declares about itself before it may run.

    `input_schema` is a JSON-Schema-shaped mapping. It is validated against
    by the executor, not trusted: a model's arguments are untrusted input.
    """

    name: str
    description: str
    risk_level: RiskLevel
    input_schema: Mapping[str, Any]
    timeout_seconds: int
    idempotent: bool
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("a tool must have a name")
        if not isinstance(self.risk_level, RiskLevel):
            raise TypeError(
                f"tool {self.name!r} declares no RiskLevel; a tool without a "
                "declared risk level does not execute"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(f"tool {self.name!r} needs a positive timeout")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "output_schema", MappingProxyType(dict(self.output_schema)))


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One proposed call. Proposed by a planner, which may be a model."""

    tool: str
    arguments: Mapping[str, Any]
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool did. `ok` is the tool's own account; VERIFY decides whether
    it is believed."""

    ok: bool
    output: str = ""
    error: str | None = None
    duration_ms: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Every request, whatever happened to it: executed, denied or asked.

    RECORD EVENT is "always, success or failure" (section 1). A denied request
    that leaves no record is indistinguishable from one never made.
    """

    request: ToolRequest
    risk_level: RiskLevel | None
    decision: PolicyDecision
    result: ToolResult | None = None
    confirmed_by_user: bool = False
    at: Any = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """A failed verification triggers RECOVER, never a silent pass (section 5)."""

    checks: tuple[VerificationCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)
