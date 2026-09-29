"""What each feedback label commits to downstream (ADR-017 §3.2).

The ADR states a rule this repository enforces mechanically: *"a feedback label
with no effect is a bug"*. It also claims *"the effect table lives in `src/` so
the enum-producer guard sees a producer for each"*. That claim was false — the
table existed only as markdown, so all eleven `FeedbackOutcome` members were
declared with no producer anywhere in `src/`, and the guard could not have
caught it because `FeedbackOutcome` was not in its target set at all.

This module is that table, as code.

**What this is.** A total function from `FeedbackOutcome` to a stable effect
CODE, transcribed from the ADR's own §3.2 table — the same five rows, the same
grouping, nothing added and nothing removed. `FeedbackRecorder` resolves the
code and puts it on the `FeedbackRecord`, so it is persisted with the feedback
and travels in the `FEEDBACK_RECORDED` payload like any other field.

**What this is not.** None of these effects are implemented. The observation
builder, the extraction rules, the `CorrectionRule` strengthening path and the
supersede directive all belong to the rest of Phase 7 and are unbuilt, exactly
as the ADR says. What exists here is the *disposition a label commits to*, not
the machinery that carries it out. The distinction matters: a persisted code
with no consumer yet is inert data, whereas a rule that claims to have run
would be a false claim in the repository's most trusted file. Nothing here
observes, decides, promotes or writes memory.

**Why codes and not sentences.** The value is persisted. A prose effect would
become part of the durable payload and would have to be treated as frozen
forever, so the ADR's wording could never be clarified without a data
migration. A code is a vocabulary with a defined resolution point; the prose
lives here, next to the code it explains.

**Why this lives in `learning/` and not in `core/`.** The enum-producer guard
deliberately excludes an enum's own defining module when counting producers, so
a table in `core/feedback.py` would not count and the eleven members would
still read as dead. The effect vocabulary is also not a domain primitive — it is
a Phase 7 concept about what a judgement is *for*, which is the layer's subject.
The boundary is unaffected: `learning` may import `core`, never the reverse.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ..core.feedback import FeedbackOutcome

# Stable effect codes. Persisted in the FEEDBACK_RECORDED payload, so the
# string values are part of the durable contract and are not renamed lightly.
EVALUATION_SIGNAL = "evaluation_signal"
CANDIDATE_STRENGTHEN = "candidate_strengthen"
CANDIDATE_PREFERENCES = "candidate_preferences"
CANDIDATE_SUPERSEDE = "candidate_supersede"
KNOWLEDGE_GAP_OBSERVATION = "knowledge_gap_observation"

# ADR-017 §3.2, row for row. Every declared member appears here; that totality
# is asserted by test_effect_table_covers_every_declared_outcome, so a member
# added to the enum without a row here fails rather than shipping inert.
OUTCOME_EFFECTS: Mapping[FeedbackOutcome, str] = MappingProxyType(
    {
        # "evaluation signals only -- refine candidate confidence, never create
        # memory directly"
        FeedbackOutcome.GOOD: EVALUATION_SIGNAL,
        FeedbackOutcome.BAD: EVALUATION_SIGNAL,
        FeedbackOutcome.WRONG: EVALUATION_SIGNAL,
        FeedbackOutcome.WRONG_SOURCE: EVALUATION_SIGNAL,
        FeedbackOutcome.TOO_SLOW: EVALUATION_SIGNAL,
        FeedbackOutcome.FALSE_REFUSAL: EVALUATION_SIGNAL,
        # "strengthens a candidate proposed by the existing CorrectionRule"
        FeedbackOutcome.CORRECTION: CANDIDATE_STRENGTHEN,
        # "propose a high-confidence preference candidate through the existing
        # rules path"
        FeedbackOutcome.REMEMBER_THIS: CANDIDATE_PREFERENCES,
        FeedbackOutcome.PREFERENCE: CANDIDATE_PREFERENCES,
        # "propose supersession of a named active memory THROUGH THE PIPELINE"
        # (review point 5). The code records that commitment; it does not
        # perform it. Nothing in this module may call MemoryStore.supersede,
        # and nothing here does.
        FeedbackOutcome.FORGET_THIS: CANDIDATE_SUPERSEDE,
        # "produce a gap observation for later (gated) research/ingest; no
        # auto-ingest in this phase"
        FeedbackOutcome.KNOWLEDGE_GAP: KNOWLEDGE_GAP_OBSERVATION,
    }
)


def effect_for(outcome: FeedbackOutcome) -> str:
    """The effect code `outcome` commits to, or `ValueError` if it commits to none.

    Raises rather than returning a default, because a label with no effect is
    the exact defect ADR-017 §3.2 forbids, and a silent fallback would convert
    that defect into a persisted code that means nothing.
    """
    try:
        return OUTCOME_EFFECTS[outcome]
    except KeyError:
        raise ValueError(
            f"FeedbackOutcome.{outcome.name} has no entry in OUTCOME_EFFECTS. "
            "ADR-017 §3.2: a feedback label with no effect is a bug. Either "
            "declare the effect here or drop the member from the enum."
        ) from None
