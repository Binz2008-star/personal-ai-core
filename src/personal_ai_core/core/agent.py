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
class AgentTaskContract:
    """Caller-owned intent for one agent task."""

    task_text: str
    action_required: bool
    # The supported test command (ADR-023 §2.3, §6 decision 3), as the model
    # would send it to run_command. None: the caller names none. Read only by
    # the completion check (ADR-023 unit 3), which is off unless asked for.
    test_command: str | None = None

    def __post_init__(self) -> None:
        task_text = self.task_text.strip()
        if not task_text:
            raise ValueError("task_text must not be empty")
        if not isinstance(self.action_required, bool):
            raise TypeError("action_required must be a boolean")
        object.__setattr__(self, "task_text", task_text)
        if self.test_command is not None:
            if not isinstance(self.test_command, str):
                raise TypeError("test_command must be a string")
            test_command = self.test_command.strip()
            if not test_command:
                raise ValueError("test_command must not be empty")
            object.__setattr__(self, "test_command", test_command)


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

    `executed` is True only when the tool's `run` was called. The executor sets
    it at that one place; it is not derived from `result`, because a request
    refused for invalid arguments, or an ASK the user declined, carries a
    result that explains the refusal -- and deriving "ran" from the presence
    of a result reported those as executed.
    """

    request: ToolRequest
    risk_level: RiskLevel | None
    decision: PolicyDecision
    result: ToolResult | None = None
    confirmed_by_user: bool = False
    executed: bool = False
    at: Any = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.executed:
            return
        if self.result is None:
            raise ValueError("an executed request must carry its result")
        if self.decision.decision is Decision.DENY:
            raise ValueError("a denied request cannot have executed")
        if self.decision.decision is Decision.ASK and not self.confirmed_by_user:
            raise ValueError("an ASK the user did not confirm cannot have executed")


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
