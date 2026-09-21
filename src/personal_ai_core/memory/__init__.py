"""The memory subsystem: promotion (Phase 3) and recall (Phase 4).

The pipeline in this package is the sole writer to `MemoryStore` in the
codebase. It is deliberately not wired into `ConversationService`
(ADR-003): the invariant `Event != Memory` holds because the conversation
path has no way to reach memory, not because a convention says it should
not.
"""
from .gate import DefaultPromotionGate
from .pipeline import ExperiencePipeline
from .retriever import MemoryRetrievalFailure, SimpleMemoryRetriever
from .rules import (
    CorrectionRule,
    ExplicitInstructionRule,
    ExtractionRule,
    InferenceRule,
    RepetitionRule,
    default_rules,
)

__all__ = [
    "CorrectionRule",
    "DefaultPromotionGate",
    "ExperiencePipeline",
    "ExplicitInstructionRule",
    "ExtractionRule",
    "InferenceRule",
    "MemoryRetrievalFailure",
    "RepetitionRule",
    "SimpleMemoryRetriever",
    "default_rules",
]
