"""Measure native transcripts and complete tool schemas without truncating them."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ...core.context import ContextAllocation
from ...core.contracts import ContextBudgetPolicy, ModelSpecLike, TokenEstimator
from .protocol import OpenCodeMessage, OpenCodeTool


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class OpenCodeBudget:
    allocation: ContextAllocation
    identity_tokens: int
    transcript_tokens: int
    tool_schema_tokens: int
    _diagnostic_estimator: TokenEstimator | None = field(default=None, repr=False, compare=False)

    def estimate_text(self, text: str) -> int:
        """Use the serving budget's estimator for diagnostic components."""
        if self._diagnostic_estimator is None:
            raise ValueError("this budget has no diagnostic estimator")
        return self._diagnostic_estimator.estimate(text)

    def describe_messages(self, messages: Sequence[OpenCodeMessage]) -> dict[str, Any]:
        if self._diagnostic_estimator is None:
            raise ValueError("this budget has no diagnostic estimator")
        return _describe_messages(self._diagnostic_estimator, messages)

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "context_window": self.allocation.context_window,
            "identity_tokens": self.identity_tokens,
            "transcript_tokens": self.transcript_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "generation_reserve": self.allocation.generation_reserve,
            "overhead": self.allocation.overhead,
            "guard_reserve": self.allocation.guard,
            "spoken_for": self.allocation.spoken_for,
            "remaining_tokens": self.allocation.evidence,
            "overcommitted": self.allocation.overcommitted,
        }


class OpenCodeContextBudget:
    """The factory funds identity/output/overhead/guard through the policy.

    `messages` is the complete client transcript, before PAC's separately
    funded identity is prepended. Tool schemas and calls are measured as
    their complete canonical objects, including metadata and arguments.
    """

    def __init__(self, estimator: TokenEstimator, policy: ContextBudgetPolicy) -> None:
        self._estimator = estimator
        self._policy = policy

    def describe(self, messages: Sequence[OpenCodeMessage]) -> dict[str, Any]:
        """Numeric diagnostics only; the allocation still uses the full array."""
        return _describe_messages(self._estimator, messages)

    def measure(
        self,
        model: ModelSpecLike,
        messages: Sequence[OpenCodeMessage],
        tools: Sequence[OpenCodeTool],
    ) -> OpenCodeBudget:
        transcript_tokens = self._estimator.estimate(canonical_json(
            [message.to_dict() for message in messages]
        ))
        tool_schema_tokens = (
            self._estimator.estimate(canonical_json([tool.to_dict() for tool in tools]))
            if tools else 0
        )
        allocation = self._policy.allocate(
            model=model, history_tokens=transcript_tokens + tool_schema_tokens
        )
        return OpenCodeBudget(
            allocation=allocation,
            identity_tokens=allocation.identity,
            transcript_tokens=transcript_tokens,
            tool_schema_tokens=tool_schema_tokens,
            _diagnostic_estimator=self._estimator,
        )


def _describe_messages(estimator: TokenEstimator, messages: Sequence[OpenCodeMessage]) -> dict[str, Any]:
    full = estimator.estimate(canonical_json([message.to_dict() for message in messages]))
    systems = [message.to_dict() for message in messages if message.role == "system"]
    system_cost = estimator.estimate(canonical_json(systems)) if systems else 0
    roles = {role: sum(message.role == role for message in messages)
             for role in ("system", "user", "assistant", "tool")}
    return {
        "canonical_transcript_tokens": full,
        "system_canonical_tokens": system_cost,
        "non_system_increment_tokens": full - system_cost,
        "role_counts": roles,
        "per_message": [{
            "index": index, "role": message.role if message.role in roles else "unknown_role",
            "estimated_tokens": estimator.estimate(canonical_json(message.to_dict())),
        } for index, message in enumerate(messages)],
        "standalone_estimates_are_additive": False,
    }
