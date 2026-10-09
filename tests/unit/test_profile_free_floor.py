"""The room kept free beside the owner's profile holds what a grounded turn
needs: two full-size retrieved passages, as rendered, in either language.

F-A capped the profile so that `PROFILE_FREE_FLOOR` tokens stay free for the
history and the evidence. P1-3 (R3) reserves, on a grounded turn, the rendered
cost of the top two passages. With 2048 free, a profile at the cap left no
room for the second of two Arabic passages; this measures the floor with the
production renderer and the production estimator rather than a constant.
"""
from __future__ import annotations

import pytest

from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import PROFILE_FREE_FLOOR
from personal_ai_core.conversation.grounding import RenderedEvidenceCost
from personal_ai_core.core.knowledge import Chunk, RetrievalProvenance, RetrievalResult
from personal_ai_core.context.redaction import NullRedactor

CHUNK_CHARS = 1000  # knowledge/chunking.py FixedSizeChunker's default size
ARABIC = "تتطلب صيانة الجسور فحصاً دورياً للمفاصل والدعامات، وتسجيل كل ملاحظة في التقرير. "
ENGLISH = "Footbridge inspection checks every joint and bearing, and records each finding. "
QUESTION_TOKENS = 64  # a short question, beside the two passages


def passage(sentence: str, ordinal: int) -> RetrievalResult:
    text = (sentence * (CHUNK_CHARS // len(sentence) + 1))[:CHUNK_CHARS]
    chunk = Chunk(document_id="manual", version_id="v1", text=text, ordinal=ordinal,
                  start=ordinal * CHUNK_CHARS, end=(ordinal + 1) * CHUNK_CHARS)
    return RetrievalResult(chunk=chunk, provenance=RetrievalProvenance(
        document_id="manual", version_id="v1", chunk_id=chunk.id,
        start=chunk.start, end=chunk.end))


def two_passages(sentence: str) -> int:
    cost = RenderedEvidenceCost(ScriptAwareTokenEstimator(), NullRedactor())
    return cost.document_section() + sum(cost.document(passage(sentence, i)) for i in (0, 1))


@pytest.mark.parametrize("sentence", [ARABIC, ENGLISH], ids=["arabic", "english"])
def test_the_free_floor_holds_two_full_passages_and_a_question(sentence):
    assert two_passages(sentence) + QUESTION_TOKENS <= PROFILE_FREE_FLOOR


def test_the_old_floor_did_not_hold_two_arabic_passages():
    # Why the floor moved: the measurement that F-A's 2048 failed.
    assert two_passages(ARABIC) + QUESTION_TOKENS > 2048
