"""Learning components (ADR-017 Phase 7): feedback recording and observation.

`learning/` imports only `core` (review point 4): the recorder talks to a
`core.contracts.FeedbackRepository`, and every durable form flows through the
event store shared with the conversation. Nothing in this package can reach
the memory store.
"""
from .feedback import FeedbackRecorder
from .observations import derive_observations
from .outcomes import OUTCOME_EFFECTS, effect_for

__all__ = [
    "OUTCOME_EFFECTS",
    "FeedbackRecorder",
    "derive_observations",
    "effect_for",
]
