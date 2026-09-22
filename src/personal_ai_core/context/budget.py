"""Context budget policy.

Implements `core.contracts.ContextBudgetPolicy`.

**A budget is not a context window.** The Boss model's window is a property of
the model, recorded in the registry. The budget is what this system chooses to
spend on retrieved evidence once the generation reserve, the prompt overhead
and the conversation history have taken their shares.

ADR-005 records the audited source hard-coding 24000 for a 32K model while the
Boss model runs at 8192 -- a threefold overflow whose symptom is truncation,
with no error and no log line. The corrective is not a better constant. It is
that the number is *derived*, every input to it is recorded on the
`ContextAllocation`, and the result carries a `source` string naming where it
came from.
"""
from __future__ import annotations

from ..core.contracts import ModelSpecLike
from ..core.context import ContextAllocation

# Room the model needs to write its answer. Reserved first, because an answer
# that cannot be finished is a worse failure than one passage fewer.
DEFAULT_GENERATION_RESERVE = 1024

# Chat scaffolding the provider adds around the messages -- role markers,
# template tokens, the grounding preamble. Small, but not zero, and assuming
# zero is how a budget that looks safe overflows by a little on every turn.
DEFAULT_OVERHEAD = 256

# ADR-011 prerequisite A. Zero because `identity/` is not built: a reserve
# invented for a component that does not exist would shrink `evidence` today
# for no benefit, and the number would be fabricated. The parameter exists so
# the share can be funded the moment something supplies a real figure -- a
# field nothing can set is decoration, not a budget line.
DEFAULT_IDENTITY_RESERVE = 0


class ReserveBasedBudgetPolicy:
    """Window minus reserves minus measured history, floored at zero.

    Deliberately *not* a percentage of the window. A proportional split gives
    a larger absolute generation reserve to a larger model, which is backwards:
    the answer does not get longer because the window did.
    """

    def __init__(
        self,
        *,
        generation_reserve: int = DEFAULT_GENERATION_RESERVE,
        overhead: int = DEFAULT_OVERHEAD,
        identity_reserve: int = DEFAULT_IDENTITY_RESERVE,
    ) -> None:
        if generation_reserve < 0:
            raise ValueError("generation_reserve must be non-negative")
        if overhead < 0:
            raise ValueError("overhead must be non-negative")
        if identity_reserve < 0:
            raise ValueError("identity_reserve must be non-negative")
        self._generation_reserve = generation_reserve
        self._overhead = overhead
        self._identity_reserve = identity_reserve

    @property
    def source(self) -> str:
        """Names this policy and its settings, for `ContextBudget.source`.

        So a budget can never be traced back to "someone hard-coded it": the
        string says which policy produced it and with what reserves.
        """
        return (
            f"reserve-based(generation={self._generation_reserve},"
            f"overhead={self._overhead},"
            f"identity={self._identity_reserve})"
        )

    def allocate(
        self, *, model: ModelSpecLike, history_tokens: int
    ) -> ContextAllocation:
        if history_tokens < 0:
            raise ValueError("history_tokens must be non-negative")
        return ContextAllocation(
            context_window=model.context_window,
            generation_reserve=self._generation_reserve,
            overhead=self._overhead,
            history=history_tokens,
            identity=self._identity_reserve,
        )
