"""A long grounded session still sends its evidence (P1-3, R3).

The history window (P1-3) kept as much history as fit beside the fixed
reserves, and the evidence budget is what the history leaves. So from some
turn on -- about turn 38 in English and 19 in Arabic, measured on #231 -- a
grounded turn sent no passage at all, and said so only in CONTEXT_ASSEMBLED
(`used: 0`). Now retrieval runs before the window is cut, and the window leaves
room for the rendered cost of what retrieval returned, capped at its top two
passages. A turn whose retrieval returned no passage keeps the whole room for
its history.

That is "returned no passage", not "needs no evidence": against a populated
corpus retrieval returns passages for any message, an off-topic one included,
so such a turn reserves too. Pinned below as a known limitation, not changed.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from personal_ai_core.context import NullRedactor, ReserveBasedBudgetPolicy
from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import build_grounded_in_memory_service
from personal_ai_core.conversation.grounding import (
    EVIDENCE_RESERVE_ITEMS,
    ContextBuilder,
    RenderedEvidenceCost,
    Retrieval,
)
from personal_ai_core.conversation.service import window_history
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.knowledge import (
    Chunk,
    Document,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)

ESTIMATOR = ScriptAwareTokenEstimator()
estimate = ESTIMATOR.estimate
TURNS = 40

# --- real prose: the documents, the questions and the replies -----------------------------

NOTES_EN = """\
The footbridge over the river was built in 1968 as a three-span steel girder bridge with a concrete deck. Its two piers stand on spread footings in the riverbed, and the abutments sit on piles driven into the clay of each bank. The deck carries a footway three metres wide and a service duct for the telephone cables that cross the river.

Inspections are made every two years by walking the deck and the bearings, and every six years from a boat or a cherry picker so the underside of the girders can be seen at arm's length. Each inspection records the condition of every element on a scale from one to five, with a photograph of anything rated three or worse.

The 2019 principal inspection found section loss of up to four millimetres in the bottom flanges near both piers, where water runs off the deck joints and pools on the flanges. The paint system there had failed completely, and the inspector rated the girder ends at four, the worst score the bridge has had since it opened.

The expansion joints at both abutments are of the buried type, a strip of asphaltic plug over a steel plate. They were replaced in 2004, and by 2019 both had cracked along their length and let water through to the bearings below. Replacing them is cheap compared with the damage the water does to the steel underneath.

The bearings are steel rocker bearings at the abutments and fixed pin bearings on the piers. The rockers on the north abutment have tilted beyond their design range because the abutment has moved towards the river by about twenty millimetres since construction. The movement was first measured in 1991 and has slowed but not stopped.

The load assessment of 2015 found the bridge adequate for pedestrian crowd loading of five kilonewtons per square metre, but only after the corroded flange thicknesses were taken as measured rather than as drawn. A further millimetre of section loss at the pier ends would bring the girders below that capacity, which is why the repairs are urgent.

The proposed scheme blast cleans the girder ends back to bright steel, plates the worst areas of the bottom flange with new steel bolted through, and repaints the whole superstructure with a three-coat system. The deck joints are replaced with a continuous deck link so that no water reaches the bearings at all.

The north abutment movement is to be stopped with two ground anchors drilled through the abutment wall into the rock beneath the clay. The rocker bearings there are replaced with elastomeric bearings that can take the remaining movement, and monitoring targets are fixed to the wall so any further movement shows up at the next inspection.
"""

NOTES_AR = """\
بني جسر المشاة فوق النهر عام ألف وتسعمئة وثمانية وستين جسرا من ثلاثة أبحر بعوارض فولاذية وبلاطة خرسانية. تقوم الدعامتان على قواعد منفردة في قاع النهر، ويرتكز الكتفان على خوازيق مدقوقة في طين الضفتين. ويحمل السطح ممرا للمشاة عرضه ثلاثة أمتار وقناة لكابلات الهاتف التي تعبر النهر.

يفحص الجسر كل سنتين بالمشي على سطحه وعلى المساند، وكل ست سنوات من قارب أو رافعة سلة حتى ترى العوارض من الأسفل عن قرب. ويسجل كل فحص حالة كل عنصر على مقياس من واحد إلى خمسة، مع صورة لكل ما قيمت حالته بثلاثة أو أسوأ.

وجد الفحص الرئيسي عام ألفين وتسعة عشر فقدا في المقطع يصل إلى أربعة مليمترات في الشفاه السفلية قرب الدعامتين، حيث يسيل الماء من فواصل السطح ويتجمع على الشفاه. وكان نظام الطلاء هناك قد انهار تماما، فقيم الفاحص نهايات العوارض بأربعة، وهي أسوأ درجة نالها الجسر منذ افتتاحه.

فواصل التمدد عند الكتفين من النوع المدفون، شريط من سدادة إسفلتية فوق صفيحة فولاذية. استبدلت عام ألفين وأربعة، وبحلول عام ألفين وتسعة عشر كانت كلتاهما قد تشققت على طولها وصارت تمرر الماء إلى المساند تحتها. واستبدالها رخيص مقارنة بما يفعله الماء بالفولاذ تحتها.

المساند عند الكتفين مساند هزازة فولاذية، وعلى الدعامتين مساند مفصلية ثابتة. وقد مالت المساند الهزازة عند الكتف الشمالي أكثر من مداها التصميمي لأن الكتف تحرك نحو النهر نحو عشرين مليمترا منذ الإنشاء. وقيست الحركة أول مرة عام ألف وتسعمئة وواحد وتسعين، وقد تباطأت لكنها لم تتوقف.

وجد تقييم الأحمال عام ألفين وخمسة عشر أن الجسر كاف لحمل حشود المشاة بخمسة كيلونيوتن للمتر المربع، لكن فقط بعد أخذ سماكات الشفاه المتآكلة كما قيست لا كما رسمت. ومليمتر آخر من فقد المقطع عند نهايات الدعامات ينزل بالعوارض دون هذه القدرة، ولهذا فالإصلاح عاجل.

يقضي المخطط المقترح بتنظيف نهايات العوارض بالسفع حتى يظهر الفولاذ اللامع، وتقوية أسوأ مناطق الشفة السفلية بصفائح فولاذية جديدة مثبتة بالبراغي، وإعادة طلاء البنية العلوية كلها بنظام من ثلاث طبقات. وتستبدل فواصل السطح بوصلة سطح مستمرة حتى لا يصل أي ماء إلى المساند.

توقف حركة الكتف الشمالي بمرساتين أرضيتين تحفران عبر جدار الكتف إلى الصخر تحت الطين. وتستبدل المساند الهزازة هناك بمساند مطاطية تتحمل ما بقي من الحركة، وتثبت أهداف رصد على الجدار حتى تظهر أي حركة أخرى في الفحص التالي.
"""

QUESTIONS = {
    "en": [
        "What did the 2019 inspection find on the girders near the piers?",
        "Why are the expansion joints such a problem for the bearings?",
        "How far has the north abutment moved, and is it still moving?",
        "Is the bridge still strong enough for a crowd of pedestrians?",
        "What does the proposed scheme do about the corroded flanges?",
        "How will the movement of the north abutment be stopped?",
        "How often is the bridge inspected, and how is the condition scored?",
        "What kind of bearings does the bridge have at each support?",
    ],
    "ar": [
        "ماذا وجد فحص عام ألفين وتسعة عشر على العوارض قرب الدعامتين؟",
        "لماذا تشكل فواصل التمدد مشكلة للمساند؟",
        "كم تحرك الكتف الشمالي، وهل ما زال يتحرك؟",
        "هل ما زال الجسر قويا بما يكفي لحشد من المشاة؟",
        "ماذا يفعل المخطط المقترح بالشفاه المتآكلة؟",
        "كيف ستوقف حركة الكتف الشمالي؟",
        "كم مرة يفحص الجسر، وكيف تقيم حالته؟",
        "ما نوع المساند في كل موضع من مواضع الارتكاز؟",
    ],
}

REPLIES = {
    "en": (
        "From the notes: the worst damage is at the girder ends near the piers, where "
        "water from the failed deck joints pools on the bottom flanges and the paint has "
        "gone. The section loss there reaches four millimetres, which is why the load "
        "assessment only passes when the measured thicknesses are used, and why one more "
        "millimetre would take the girders below the crowd loading. The repair plates the "
        "flanges, repaints the steel, and replaces the joints so water no longer reaches "
        "the bearings."
    ),
    "ar": (
        "بحسب الملاحظات: أسوأ الضرر عند نهايات العوارض قرب الدعامتين، حيث يتجمع ماء "
        "الفواصل التالفة على الشفاه السفلية وقد زال الطلاء. ويصل فقد المقطع هناك إلى أربعة "
        "مليمترات، ولهذا لا ينجح تقييم الأحمال إلا بالسماكات المقيسة، ومليمتر آخر ينزل "
        "بالعوارض دون حمل الحشود. ويقضي الإصلاح بتقوية الشفاه وإعادة الطلاء واستبدال "
        "الفواصل حتى لا يصل الماء إلى المساند."
    ),
}


class Transport:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": self.reply}}


def _assembled(events, session_id) -> list[Mapping[str, Any]]:
    return [e.payload for e in events.list_for_session(session_id)
            if e.type is EventType.CONTEXT_ASSEMBLED]


def _session(language: str, *, documents: bool = True, turns: int = TURNS):
    transport = Transport(REPLIES[language])
    grounded = build_grounded_in_memory_service(transport=transport)
    if documents:
        notes = NOTES_EN if language == "en" else NOTES_AR
        grounded.ingestion.ingest(
            Document(source_uri="file:///notes/footbridge.md", declared_language=language),
            notes,
        )
    service = grounded.service
    session = service.start_session(service.create_user().id)
    questions = QUESTIONS[language]
    for turn in range(turns):
        service.send(session_id=session.id, content=questions[turn % len(questions)],
                     language=language)
    return service, grounded.events, session, transport


def _fixed(payload: Mapping[str, Any]) -> int:
    return (payload["generation_reserve"] + payload["overhead"]
            + payload["identity_reserve"] + payload["guard_reserve"])


# --- the session ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "ar"])
def test_turn_40_of_a_long_grounded_session_still_sends_two_passages(language):
    service, events, session, transport = _session(language)
    assembled = _assembled(events, session.id)
    assert len(assembled) == TURNS and len(transport.payloads) == TURNS
    last = assembled[-1]

    # The history is windowed -- the session is long enough to need it ...
    assert last["history_left_out"] > 0
    # ... and the evidence is still sent: at least two passages, or every one
    # retrieval returned when it returned fewer.
    assert last["retrieved"] >= 2
    assert last["used"] >= min(EVIDENCE_RESERVE_ITEMS, last["retrieved"])
    assert last["evidence_reserve"] > 0
    assert last["budget_tokens"] >= last["evidence_reserve"]
    # Every grounded turn of the session, not only the last one.
    for payload in assembled:
        assert payload["used"] >= min(EVIDENCE_RESERVE_ITEMS, payload["retrieved"]), payload
        assert payload["overcommitted"] is False
    # And the passages reached the model.
    prompt = transport.payloads[-1]["messages"]
    assert prompt[1]["role"] == "system" and prompt[1]["content"].count("<<<passage ") >= 2
    # The store keeps every message.
    assert len(service.history(session.id)) == 2 * TURNS


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_history_gives_up_only_the_room_the_evidence_needs(language):
    service, events, session, _ = _session(language)
    last = _assembled(events, session.id)[-1]
    seen = list(service.history(session.id))[:-1]  # all but the last reply
    window = last["context_window"] - _fixed(last)

    def left_out(reserve: int) -> int:
        return window_history(seen, estimate=estimate,
                              fits=lambda tokens: tokens + reserve <= window).left_out

    # Exactly the window the reserve gives: the newest messages that fit
    # beside it, and not one message fewer.
    assert last["history_left_out"] == left_out(last["evidence_reserve"])
    # More than without the reserve: the reserve is what made the room.
    assert last["history_left_out"] > left_out(0)


@pytest.mark.parametrize("language", ["en", "ar"])
def test_a_long_turn_whose_retrieval_returns_nothing_keeps_the_whole_room_for_its_history(
    language,
):
    # No documents: retrieval returns nothing, so nothing is reserved. An empty
    # corpus is how to get there -- a populated one returns passages for any
    # message (see the off-topic test below).
    service, events, session, _ = _session(language, documents=False)
    last = _assembled(events, session.id)[-1]
    assert last["retrieved"] == 0 and last["evidence_reserve"] == 0
    assert last["history_left_out"] > 0
    seen = list(service.history(session.id))[:-1]
    room = last["context_window"] - _fixed(last)
    expected = window_history(seen, estimate=estimate, fits=lambda tokens: tokens <= room)
    assert last["history_left_out"] == expected.left_out
    assert last["history_tokens"] == sum(estimate(m.content) for m in expected.sent)


OFF_TOPIC = {"en": "ok, thanks!", "ar": "شكراً"}


@pytest.mark.parametrize("language", ["en", "ar"])
def test_an_off_topic_turn_over_a_populated_corpus_still_reserves(language):
    # KNOWN LIMITATION, characterized, not endorsed. The vector arm has no
    # similarity floor, so a message that needs no evidence still retrieves
    # passages from a populated corpus, and its history gives up their room.
    # A change that makes this turn reserve nothing (a similarity floor) is a
    # retrieval change and must update this test on purpose, measured against
    # the retrieval-only bench gate.
    service, events, session, _ = _session(language)
    service.send(session_id=session.id, content=OFF_TOPIC[language], language=language)
    last = _assembled(events, session.id)[-1]
    assert last["retrieved"] > 0
    assert last["evidence_reserve"] > 0
    seen = list(service.history(session.id))[:-1]
    room = last["context_window"] - _fixed(last)

    def left_out(reserve: int) -> int:
        return window_history(seen, estimate=estimate,
                              fits=lambda tokens: tokens + reserve <= room).left_out

    # The history gave up the reserve's room for passages nobody asked about.
    assert last["history_left_out"] == left_out(last["evidence_reserve"])
    assert last["history_left_out"] > left_out(0)


# --- the reserve ---------------------------------------------------------------------------


def _passage(text: str, *, chunk_id: str) -> RetrievalResult:
    chunk = Chunk(document_id="d1", version_id="v1", text=text, ordinal=0,
                  start=0, end=len(text), id=chunk_id)
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id="d1", version_id="v1", chunk_id=chunk_id, start=0,
            end=len(text), methods=(RetrievalMethod.LEXICAL,),
            source_uri="file:///notes/footbridge.md",
        ),
    )


def _builder() -> ContextBuilder:
    policy = ReserveBasedBudgetPolicy()
    redactor = NullRedactor()

    class Unused:
        def retrieve(self, query):  # pragma: no cover - the reserve never retrieves
            raise AssertionError("not called")

    return ContextBuilder(
        retriever=Unused(),
        assembler=HybridContextAssembler(
            ESTIMATOR, rendered_cost=RenderedEvidenceCost(ESTIMATOR, redactor)),
        budget_policy=policy, estimator=ESTIMATOR, redactor=redactor,
    )


PARAGRAPHS = {
    language: [p for p in notes.split("\n\n") if p.strip()]
    for language, notes in (("en", NOTES_EN), ("ar", NOTES_AR))
}


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_reserve_is_what_came_back_capped_at_the_top_two(language):
    builder = _builder()
    cost = RenderedEvidenceCost(ESTIMATOR, NullRedactor())
    passages = [_passage(text, chunk_id=f"c{i}")
                for i, text in enumerate(PARAGRAPHS[language][:5])]
    top_two = cost.document_section() + cost.document(passages[0]) + cost.document(passages[1])

    assert builder.evidence_reserve(Retrieval()) == 0
    assert builder.evidence_reserve(Retrieval(skipped=True)) == 0
    assert builder.evidence_reserve(Retrieval(results=tuple(passages[:1]))) == (
        cost.document_section() + cost.document(passages[0]))
    assert builder.evidence_reserve(Retrieval(results=tuple(passages[:2]))) == top_two
    for n in range(1, 6):
        assert builder.evidence_reserve(Retrieval(results=tuple(passages[:n]))) <= top_two
    assert builder.evidence_reserve(Retrieval(results=tuple(passages))) == top_two
    # A duplicate of the first is not one of the two: the assembler drops it.
    duplicate = _passage(passages[0].chunk.text, chunk_id="copy")
    assert builder.evidence_reserve(
        Retrieval(results=(passages[0], duplicate, passages[1]))) == top_two


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_reserved_room_admits_the_top_two_passages(language):
    """The reserve is priced as the production assembler charges, so the
    first two passages fit in exactly that room."""
    builder = _builder()
    passages = tuple(_passage(text, chunk_id=f"c{i}")
                     for i, text in enumerate(PARAGRAPHS[language][:5]))
    reserve = builder.evidence_reserve(Retrieval(results=passages))
    from personal_ai_core.core.context import ContextBudget

    context = HybridContextAssembler(
        ESTIMATOR, rendered_cost=RenderedEvidenceCost(ESTIMATOR, NullRedactor()),
    ).assemble(results=passages, memories=(), budget=ContextBudget(reserve))
    assert [r.chunk.id for r in context.document_context.selected] == ["c0", "c1"]
    assert context.document_context.token_estimate == reserve
