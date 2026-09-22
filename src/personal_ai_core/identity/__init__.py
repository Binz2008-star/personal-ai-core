"""Identity — the contract's text and its assembly (ADR-011, ADR-012).

A layer of its own rather than part of `core`, because `core` states contracts
and product wording is not a contract; and rather than part of `conversation`,
because `conversation` may import `core` only. It reaches
`ConversationService` the way the budget policy does: injected by the
composition root (ADR-011 question 8).

Nothing here is persisted, and nothing here varies by model or by session.
"""
from .composer import DefaultIdentityComposer
from .text import BEHAVIORAL_CONTRACT, RESPONSE_POLICY, RULES

__all__ = [
    "BEHAVIORAL_CONTRACT",
    "DefaultIdentityComposer",
    "RESPONSE_POLICY",
    "RULES",
]
