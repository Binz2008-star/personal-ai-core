"""Assembly — one `Role.SYSTEM` message per turn.

Implements `core.contracts.IdentityComposer`.

The composed text is constant for the life of the composer, so it is built
once and reused. That is not a micro-optimisation: `tokens` is the number the
budget is funded with, and a text rebuilt per turn could be funded with a
figure that no longer describes it.
"""
from __future__ import annotations

from ..core.contracts import TokenEstimator
from ..core.domain import UNDETERMINED_LANGUAGE, Message, Role
from ..core.identity import BehavioralContract, ResponsePolicy
from .text import (
    BEHAVIORAL_CONTRACT,
    CONTRACT_HEADING,
    POLICY_HEADING,
    PROFILE_HEADING,
    RESPONSE_POLICY,
)


class DefaultIdentityComposer:
    """Policy and contract, in that order, in one system message.

    Order is not arbitrary. The policy describes how to answer; the contract
    says what is never done whatever the answer. Reading the constraints last
    leaves them nearest the turn they govern.
    """

    def __init__(
        self,
        *,
        policy: ResponsePolicy = RESPONSE_POLICY,
        contract: BehavioralContract = BEHAVIORAL_CONTRACT,
        profile: str | None = None,
    ) -> None:
        self._policy = policy
        self._contract = contract
        profile = (profile or "").strip()
        self._profile = profile or None
        parts = [POLICY_HEADING, policy.render()]
        if profile:
            # Before the contract, so the contract stays nearest the turn.
            parts += [PROFILE_HEADING, profile]
        parts += [CONTRACT_HEADING, contract.render()]
        self._text = "\n\n".join(parts)

    @property
    def profile(self) -> str | None:
        """The owner's profile as composed, or None when there is none."""
        return self._profile

    @property
    def text(self) -> str:
        """The composed text, so its cost can be measured rather than guessed."""
        return self._text

    def tokens(self, estimator: TokenEstimator) -> int:
        """What this identity costs, by the same estimator the budget uses.

        ADR-005: a budget must be derived and every input recorded. The
        identity share was funded at zero while `identity/` did not exist,
        because any other number would have been invented. This is the real
        one -- measured from the text that is actually sent, with the
        estimator that measures everything else.
        """
        return estimator.estimate(self._text)

    def compose(self, *, session_id: str) -> Message:
        # UNDETERMINED_LANGUAGE for the same reason the grounding message uses
        # it: the message is instruction, not user-facing prose, and tagging
        # it with a language would claim something about text that governs
        # every language at once.
        return Message(
            session_id=session_id,
            role=Role.SYSTEM,
            content=self._text,
            language=UNDETERMINED_LANGUAGE,
        )
