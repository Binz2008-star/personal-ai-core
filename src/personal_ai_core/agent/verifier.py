"""VERIFY -- deterministic checks, cheapest first (AGENT_ARCHITECTURE.md section 5).

BUILD, not extracted: no source has a generic verifier (Robin's quality_gate
validates video files). Only the gate-result SHAPE transfers, and it is
`core.agent.VerificationResult`.

Layers, per the design:
  1. Schema   -- did the step actually run, and report success?
  2. Grounding-- claims trace to evidence. NOT here: it needs a model's claims
                 and the evidence they rest on, and a mechanical check of that
                 is ADR-013's harness. Named rather than faked.
  3. Policy   -- no secret in anything the agent produced.
  4. Task     -- the plan's stated expectation is met.

"A failed verification triggers RECOVER, never a silent pass." The verifier
only reports; it never retries, repairs or rolls back. That is recovery.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.agent import AuditRecord, Decision, VerificationCheck, VerificationResult

# Shapes that are secrets whatever file they came from. Deliberately
# high-precision: a pattern that fires on ordinary text trains the reader to
# ignore it. Each has a test with a real-shaped example and a near miss.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "credential assignment",
        re.compile(
            # A placeholder is not a secret: `<redacted>`, `${API_KEY}`,
            # `{{token}}`, `********` are what documentation looks like.
            r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?"
            r"(?![<${*])[^\s'\"]{8,}"
        ),
    ),
    ("connection string", re.compile(r"\b[a-z+]+://[^\s:/@]+:[^\s@/]+@[^\s/]+")),
)


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the plan said a step should produce. Optional: not every step has
    a checkable expectation, and inventing one would be a check that means
    nothing."""

    output_contains: str | None = None


def find_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


class Verifier:
    def verify(
        self, record: AuditRecord, expectation: Expectation | None = None
    ) -> VerificationResult:
        checks = [self._ran(record)]
        result = record.result
        if result is not None:
            checks.append(
                VerificationCheck(
                    "succeeded",
                    result.ok,
                    "the tool reported success" if result.ok else (result.error or "failed"),
                )
            )
            checks.append(self._no_secret("no secret in output", result.output))
            if expectation is not None and expectation.output_contains is not None:
                found = expectation.output_contains in result.output
                checks.append(
                    VerificationCheck(
                        "expectation",
                        found,
                        f"output {'contains' if found else 'lacks'} "
                        f"{expectation.output_contains!r}",
                    )
                )
        return VerificationResult(tuple(checks))

    def verify_response(self, text: str) -> VerificationResult:
        """The policy layer, applied to what the agent is about to say."""
        return VerificationResult((self._no_secret("no secret in response", text),))

    @staticmethod
    def _ran(record: AuditRecord) -> VerificationCheck:
        if record.decision.decision is Decision.DENY:
            return VerificationCheck("ran", False, f"denied: {record.decision.reason}")
        if record.result is None:
            return VerificationCheck("ran", False, "no result was recorded")
        if record.decision.decision is Decision.ASK and not record.confirmed_by_user:
            return VerificationCheck("ran", False, "not confirmed by the user")
        if not record.executed:
            return VerificationCheck(
                "ran", False, f"refused before the tool started: {record.result.error}"
            )
        return VerificationCheck("ran", True, "executed")

    @staticmethod
    def _no_secret(name: str, text: str) -> VerificationCheck:
        found = find_secrets(text)
        if found:
            return VerificationCheck(name, False, "looks like: " + ", ".join(found))
        return VerificationCheck(name, True, "nothing secret-shaped")
