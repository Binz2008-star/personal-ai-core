"""The policy gate: ALLOW, DENY or ASK before anything executes.

AGENT_ARCHITECTURE.md section 3, as a table the code reads rather than a
paragraph it paraphrases:

    LOW       auto-allow
    MEDIUM    allow in workspace, audit
    HIGH      ask
    CRITICAL  ask, always audited, rollback point first

"The gate is not advisory." Two consequences are enforced here:

- A request naming no registered tool is DENIED. There is no fallback tool
  and no "closest match".
- A per-tool override may only make a decision STRICTER. An override that
  would turn ASK into ALLOW is refused when the policy is built, not
  discovered when the tool runs: the table above is the floor, and the only
  way below it is to change this module in a reviewed pull request.
"""
from __future__ import annotations

from typing import Mapping

from ..core.agent import Decision, PolicyDecision, RiskLevel, ToolRequest, ToolSpec

DEFAULT_DECISIONS: Mapping[RiskLevel, Decision] = {
    RiskLevel.LOW: Decision.ALLOW,
    RiskLevel.MEDIUM: Decision.ALLOW,
    RiskLevel.HIGH: Decision.ASK,
    RiskLevel.CRITICAL: Decision.ASK,
}

# Strictness order. DENY is strictest: nothing a user says makes it run.
_STRICTNESS = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


class RiskPolicy:
    """`core.contracts.ToolPolicy`, driven by each tool's declared risk level."""

    def __init__(self, overrides: Mapping[str, Decision] | None = None) -> None:
        self._overrides = dict(overrides or {})

    def check_overrides(self, specs: Mapping[str, ToolSpec]) -> None:
        """Refuse any override that loosens the table, or names no tool.

        Called by whoever registers the tools, once, before the agent runs.
        """
        for name, decision in self._overrides.items():
            spec = specs.get(name)
            if spec is None:
                raise ValueError(f"policy override for a tool that does not exist: {name}")
            floor = DEFAULT_DECISIONS[spec.risk_level]
            if _STRICTNESS[decision] < _STRICTNESS[floor]:
                raise ValueError(
                    f"override would loosen {name} ({spec.risk_level.value}) from "
                    f"{floor.value} to {decision.value}; overrides may only tighten"
                )

    def decide(self, request: ToolRequest, spec: ToolSpec | None) -> PolicyDecision:
        if spec is None:
            return PolicyDecision(Decision.DENY, f"no such tool: {request.tool}")
        if spec.name != request.tool:
            return PolicyDecision(
                Decision.DENY,
                f"request names {request.tool!r} but was matched to {spec.name!r}",
            )
        floor = DEFAULT_DECISIONS[spec.risk_level]
        override = self._overrides.get(spec.name)
        if override is not None and _STRICTNESS[override] > _STRICTNESS[floor]:
            return PolicyDecision(override, f"{spec.name}: tightened by policy override")
        reasons = {
            Decision.ALLOW: f"{spec.risk_level.value}-risk tool, allowed",
            Decision.ASK: f"{spec.risk_level.value}-risk tool, needs your confirmation",
            Decision.DENY: f"{spec.risk_level.value}-risk tool, denied",
        }
        return PolicyDecision(floor, reasons[floor])
