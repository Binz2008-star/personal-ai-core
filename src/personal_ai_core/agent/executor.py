"""The one path from a proposed request to a result -- AGENT_ARCHITECTURE.md section 2.

    ToolRequest -> PolicyEngine -> ALLOW | DENY | ASK
                        |
                  argument validation
                        |
                ASK -> the user confirms, or nothing runs
                        |
                      Tool
                        |
                  ToolResult -> Audit

Invariants, each pinned in tests/unit/test_agent_executor.py:

- EVERY request is audited, whatever happened to it. A denied request that
  leaves no trace is indistinguishable from one never made.
- DENY runs nothing. ASK runs nothing unless the confirmation callback says
  yes FOR THIS REQUEST; with no callback wired, ASK means no.
- Invalid arguments run nothing, even when the policy allowed the tool.
- A tool that raises does not take the agent down with it: the failure
  becomes a failed ToolResult with the reason, and is audited like any other.

Idempotency (the design's "idempotency check") is recorded per tool in its
spec. Retrying is RECOVER's decision, stage 3; this executor never retries.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping

from ..core.agent import (
    AuditRecord,
    Decision,
    PolicyDecision,
    ToolRequest,
    ToolResult,
    ToolSpec,
)
from ..core.contracts import Tool, ToolPolicy
from .commands import CommandRejected
from .sandbox import SandboxError
from .schema import check_schema, validate_arguments

Confirm = Callable[[ToolRequest, ToolSpec], bool]


class AuditLog:
    """Every request the executor saw, in order. Append-only."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)


class ToolExecutor:
    def __init__(
        self,
        tools: Iterable[Tool],
        policy: ToolPolicy,
        *,
        confirm: Confirm | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            name = tool.spec.name
            if name in self._tools:
                raise ValueError(f"two tools are named {name!r}")
            check_schema(tool.spec.input_schema)
            self._tools[name] = tool
        check = getattr(policy, "check_overrides", None)
        if callable(check):
            check(self.specs)
        self._policy = policy
        self._confirm = confirm
        self.audit = audit if audit is not None else AuditLog()

    @property
    def specs(self) -> Mapping[str, ToolSpec]:
        return {name: tool.spec for name, tool in self._tools.items()}

    def execute(self, request: ToolRequest) -> AuditRecord:
        tool = self._tools.get(request.tool)
        spec = tool.spec if tool is not None else None
        decision = self._policy.decide(request, spec)
        record = self._decide_and_run(request, tool, spec, decision)
        self.audit.append(record)
        return record

    def _decide_and_run(
        self,
        request: ToolRequest,
        tool: Tool | None,
        spec: ToolSpec | None,
        decision: PolicyDecision,
    ) -> AuditRecord:
        risk = spec.risk_level if spec is not None else None
        if decision.decision is Decision.DENY or tool is None or spec is None:
            return AuditRecord(request=request, risk_level=risk, decision=decision)

        problems = validate_arguments(request.arguments, spec.input_schema)
        if problems:
            return AuditRecord(
                request=request,
                risk_level=risk,
                decision=decision,
                result=ToolResult(ok=False, error="invalid arguments: " + "; ".join(problems)),
            )

        confirmed = False
        if decision.decision is Decision.ASK:
            confirmed = bool(self._confirm and self._confirm(request, spec))
            if not confirmed:
                return AuditRecord(
                    request=request,
                    risk_level=risk,
                    decision=decision,
                    result=ToolResult(ok=False, error="not confirmed by the user; nothing ran"),
                )

        started = time.monotonic()
        try:
            result = tool.run(request.arguments)
        except (SandboxError, CommandRejected) as exc:
            result = ToolResult(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 -- contained and audited, never raised
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        duration_ms = int((time.monotonic() - started) * 1000)
        result = ToolResult(
            ok=result.ok,
            output=result.output,
            error=result.error,
            duration_ms=duration_ms,
            truncated=result.truncated,
        )
        return AuditRecord(
            request=request,
            risk_level=risk,
            decision=decision,
            result=result,
            confirmed_by_user=confirmed,
            executed=True,
        )
