"""Context layer: budget policy, token estimation and budgeted assembly.

Mirrors the `core.context` types the way `knowledge` mirrors `core.knowledge`:
the types and contracts live in the Core, the implementations live here, and
the Core never learns which implementation is in use.
"""
from .assembler import GreedyContextAssembler
from .budget import (
    DEFAULT_GENERATION_RESERVE,
    DEFAULT_OVERHEAD,
    ReserveBasedBudgetPolicy,
)
from .token_estimator import ScriptAwareTokenEstimator

__all__ = [
    "DEFAULT_GENERATION_RESERVE",
    "DEFAULT_OVERHEAD",
    "GreedyContextAssembler",
    "ReserveBasedBudgetPolicy",
    "ScriptAwareTokenEstimator",
]
