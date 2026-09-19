"""Context layer: token estimation and budgeted assembly.

Mirrors the `core.context` types the way `knowledge` mirrors `core.knowledge`:
the types and contracts live in the Core, the implementations live here, and
the Core never learns which implementation is in use.
"""
from .assembler import GreedyContextAssembler
from .token_estimator import ScriptAwareTokenEstimator

__all__ = ["GreedyContextAssembler", "ScriptAwareTokenEstimator"]
