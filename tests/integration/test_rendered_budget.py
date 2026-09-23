"""The grounding message fits the budget it was assembled against -- F-4.

Through the production composition root, so the test fails if the factory
stops handing the assembler a `RenderedCost`, not only if the cost model is
wrong.

Measured before the fix, with a 2000-token window: the evidence budget was
398 tokens and the evidence message the model received estimated 650. Every
passage had been admitted on its bare text, and the boundary lines and the
preamble around them were paid for by nobody.
"""
from __future__ import annotations

from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import build_grounded_in_memory_service
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.knowledge import Document

from .test_grounded_conversation import Recorder, evidence_message

# Small enough that the budget binds: some passages must be left out.
WINDOW = 2000


def grounded_turn():
    transport = Recorder()
    slice_ = build_grounded_in_memory_service(
        Settings(boss_context_window=WINDOW), transport=transport
    )
    for d in range(8):
        text = "\n\n".join(
            f"Rank fusion note {d}-{i}: reciprocal rank fusion combines rankings."
            for i in range(3)
        )
        slice_.ingestion.ingest(
            Document(source_uri=f"file:///notes/n{d}.md", declared_language="en"), text
        )
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="reciprocal rank fusion")
    [assembled] = [
        e for e in slice_.events.all() if e.type is EventType.CONTEXT_ASSEMBLED
    ]
    return assembled.payload, evidence_message(transport)["content"]


def test_the_grounding_message_fits_the_budget_it_was_assembled_against():
    payload, evidence = grounded_turn()

    # The budget actually bound: otherwise this proves nothing about fitting.
    assert payload["dropped"] >= 1
    assert payload["used"] >= 1

    rendered = ScriptAwareTokenEstimator().estimate(evidence)
    assert rendered <= payload["budget_tokens"], (
        f"the evidence message estimates {rendered} tokens against a budget of "
        f"{payload['budget_tokens']}: rendering spent tokens nobody charged"
    )


def test_the_recorded_evidence_tokens_are_what_was_charged():
    """`evidence_tokens` in CONTEXT_ASSEMBLED now includes the rendering, so the
    audit record accounts for the message the model received, not for a
    subset of it."""
    payload, evidence = grounded_turn()
    assert payload["evidence_tokens"] >= ScriptAwareTokenEstimator().estimate(evidence)
    assert payload["evidence_tokens"] <= payload["budget_tokens"]
