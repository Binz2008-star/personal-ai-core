"""Secret redaction: the value types (ADR-018).

The protocol is `core.contracts.SecretRedactor`; the implementation lives in
`context.redaction`. This module holds only what both sides must agree on:
the result of one redaction and the one exception a redactor may raise.

Nothing here knows which detectors exist. A count is keyed by whatever kind
name the implementation reports, so adding a detector never touches the Core.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .errors import CoreError


@dataclass(frozen=True, slots=True)
class Redaction:
    """What a redactor produced for one text.

    `text` is the text to render. `counts` is how many values each detector
    kind withheld -- a count and nothing else. It never carries a value or a
    value's length (ADR-018 section 3.7), because it is destined for an event
    payload. Only kinds that withheld something appear; a text with nothing
    withheld has empty `counts`.

    `counts` is copied into a read-only mapping on construction, so neither
    the caller's dict nor a later caller can change a recorded result.
    """

    text: str
    counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        copied: dict[str, int] = {}
        for kind, count in self.counts.items():
            if not isinstance(kind, str) or not kind:  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValueError("a redaction kind must be a non-empty string")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValueError("a redaction count must be a positive integer")
            copied[kind] = count
        object.__setattr__(self, "counts", MappingProxyType(copied))

    @property
    def total(self) -> int:
        """How many values were withheld, across every kind."""
        return sum(self.counts.values())


class RedactionError(CoreError):
    """A redactor failed. Carries a stable classification and nothing else.

    ADR-018 section 3.8: the turn fails rather than rendering raw text, and
    the error must never quote the text it failed on -- an exception message
    holding the passage would put the secret into an event payload, which is
    the defect PR #5 fixed. The constructor therefore accepts only one of the
    declared classifications; free text is refused outright.
    """

    INVALID_INPUT = "invalid_input"
    INTERNAL = "internal"
    CLASSIFICATIONS = frozenset({INVALID_INPUT, INTERNAL})

    def __init__(self, classification: str) -> None:
        if classification not in self.CLASSIFICATIONS:
            raise ValueError("unknown redaction error classification")
        super().__init__(classification)
        self.classification = classification
