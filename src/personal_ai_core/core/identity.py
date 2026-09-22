"""Identity contract types — ADR-011, ADR-012.

The **types** live here and the **text** does not. `core` depends on nothing
and states contracts; product wording is not a contract (ADR-011 question 8).
So this module defines what a response policy and a behavioural contract *are*,
and `personal_ai_core.identity` supplies what they *say*.

Values, not state: both are frozen, like every other domain type, and both
validate at construction. An empty contract is the failure ADR-011 records
having suffered once -- a rewrite left a "behavioural contract" with no rules
in it -- so a contract with no rules is rejected rather than composed into an
empty section of every prompt.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResponsePolicy:
    """How to answer: language, register, and answer discipline.

    Two fields rather than one string, because they answer different failures
    and ADR-012 requires each rule to name the failure it answers. Collapsing
    them would make it impossible to change one without restating the other.
    """

    language_and_register: str
    answer_discipline: str

    def __post_init__(self) -> None:
        for name in ("language_and_register", "answer_discipline"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")

    def render(self) -> str:
        return f"{self.language_and_register.strip()}\n\n{self.answer_discipline.strip()}"


@dataclass(frozen=True, slots=True)
class BehavioralContract:
    """Numbered rules, present in every model call.

    Numbered because a rule that cannot be cited cannot be pointed at. The
    numbering is produced from the order of `rules`, not written into them, so
    inserting a rule cannot leave two rules sharing a number -- the kind of
    defect that survives review because nobody reads a list for arithmetic.
    """

    rules: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError(
                "a behavioural contract with no rules is not a contract: it "
                "would compose an empty section into every prompt and read as "
                "though rules were present"
            )
        for index, rule in enumerate(self.rules, start=1):
            if not rule.strip():
                raise ValueError(f"rule {index} must not be empty")

    def render(self) -> str:
        return "\n".join(
            f"{index}. {rule.strip()}" for index, rule in enumerate(self.rules, start=1)
        )
