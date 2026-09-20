"""The pipeline: sole writer, correct events, and no accidental leakage.

`test_pipeline_is_the_only_module_that_writes_to_memory_store` is the
structural safeguard: if any other src/ module gains a `.write(` call on
a `MemoryStore`, the sole-writer invariant is quietly gone. This test
exists so that cannot happen.
"""
from __future__ import annotations

import ast
from pathlib import Path

from personal_ai_core.core.domain import EventType
from personal_ai_core.core.memory import (
    ExperienceRecord,
    MemoryStatus,
    PromotionDecision,
)
from personal_ai_core.memory.gate import DefaultPromotionGate
from personal_ai_core.memory.pipeline import ExperiencePipeline
from personal_ai_core.persistence.in_memory import InMemoryEventRepository
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"


def _build_pipeline():
    store = InMemoryMemoryRepository()
    events = InMemoryEventRepository()
    gate = DefaultPromotionGate()
    pipeline = ExperiencePipeline(gate=gate, store=store, events=events)
    return pipeline, store, events


def test_promoting_an_experience_writes_an_active_record_and_emits_memory_promoted():
    pipeline, store, events = _build_pipeline()
    experience = ExperienceRecord(
        session_id="s1", text="I prefer concise answers in Arabic"
    )
    outcomes = pipeline.ingest(experience)
    assert len(outcomes) == 1
    assert outcomes[0].decision is PromotionDecision.PROMOTED

    active = store.list_active()
    assert len(active) == 1
    assert active[0].status is MemoryStatus.ACTIVE
    assert active[0].session_id == "s1"

    kinds = [e.type for e in events.list_for_session("s1")]
    assert kinds == [EventType.MEMORY_PROMOTED]


def test_repeated_promotion_of_the_same_claim_is_rejected_as_idempotent():
    pipeline, store, events = _build_pipeline()
    experience = ExperienceRecord(
        session_id="s1", text="I prefer concise answers in Arabic"
    )
    pipeline.ingest(experience)
    outcomes = pipeline.ingest(experience)
    assert outcomes[0].decision is PromotionDecision.REJECTED

    kinds = [e.type for e in events.list_for_session("s1")]
    assert kinds == [EventType.MEMORY_PROMOTED, EventType.MEMORY_REJECTED]


def test_a_conflict_is_held_not_silently_overwritten():
    pipeline, store, events = _build_pipeline()
    first = ExperienceRecord(session_id="s1", text="I prefer Arabic replies only")
    pipeline.ingest(first)

    conflicting = ExperienceRecord(
        session_id="s1", text="I prefer Arabic responses always in English"
    )
    outcomes = pipeline.ingest(conflicting)
    assert outcomes[0].decision is PromotionDecision.HELD
    assert outcomes[0].conflicts_with is not None

    # A hold writes no record. The active set is unchanged.
    assert len(store.list_active()) == 1

    kinds = [e.type for e in events.list_for_session("s1")]
    assert kinds == [EventType.MEMORY_PROMOTED, EventType.MEMORY_CONFLICT_DETECTED]


def test_rejected_candidates_are_retained_with_rejected_status():
    """MEMORY_ARCHITECTURE.md: rejected candidates are retained, not discarded.

    Reject a candidate by raising every threshold above any rule's
    confidence, then verify the outcome carries the id of a stored record.
    """
    from personal_ai_core.core.memory import MemoryType

    unreachable = {member: 0.99 for member in MemoryType}
    gate = DefaultPromotionGate(thresholds=unreachable)
    store = InMemoryMemoryRepository()
    events = InMemoryEventRepository()
    pipeline = ExperiencePipeline(gate=gate, store=store, events=events)

    outcomes = pipeline.ingest(
        ExperienceRecord(session_id="s1", text="I prefer concise answers")
    )
    assert outcomes[0].decision is PromotionDecision.REJECTED
    assert outcomes[0].memory_id is not None

    # No ACTIVE record ...
    assert store.list_active() == ()
    # ... but the rejected record was retained and is retrievable by id.
    retained = store.read(outcomes[0].memory_id)
    assert retained is not None
    assert retained.status is MemoryStatus.REJECTED


def test_pipeline_is_the_only_module_that_writes_to_memory_store():
    """Sole-writer invariant, statically enforced.

    Scans every src/ file for a call whose receiver is a name bound to a
    MemoryStore. Only pipeline.py may contain one.
    """
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        binds_memory_store: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in [*node.args.args, *node.args.kwonlyargs]:
                    if arg.annotation is None:
                        continue
                    if _annotation_names(arg.annotation) & {
                        "MemoryStore", "InMemoryMemoryRepository"
                    }:
                        binds_memory_store.add(arg.arg)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.annotation is not None and _annotation_names(
                    node.annotation
                ) & {"MemoryStore", "InMemoryMemoryRepository"}:
                    binds_memory_store.add(node.target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "write":
                continue
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in binds_memory_store:
                offenders.append(str(path.relative_to(SRC_ROOT)))
            elif (
                isinstance(receiver, ast.Attribute)
                and receiver.attr in {"_store", "store"}
            ):
                offenders.append(str(path.relative_to(SRC_ROOT)))

    allowed = {"memory/pipeline.py"}
    unexpected = [o for o in offenders if o not in allowed]
    assert not unexpected, (
        "MemoryStore.write is called outside memory/pipeline.py:\n  "
        + "\n  ".join(unexpected)
        + "\n\nExperiencePipeline is the sole writer. If a second writer is needed, "
        "the invariant is being lost; add the new caller to `allowed` only after "
        "the design has been reconsidered."
    )


def _annotation_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def test_conversation_service_has_no_memory_collaborator_after_phase_3():
    """The `Event != Memory` invariant survives the Phase 3 landing.

    ConversationService still exposes zero attributes with 'memory' in the
    name -- adding memory alongside the conversation path did not wire it in.
    """
    from personal_ai_core.conversation.factory import build_in_memory_service

    def fake_transport(url, payload, timeout):
        return {
            "model": payload["model"],
            "message": {"content": "ok"},
            "done_reason": "stop",
        }

    service, _ = build_in_memory_service(transport=fake_transport)
    assert not any("memory" in attr.lower() for attr in vars(service))


def test_conversation_turn_emits_no_memory_events_even_on_a_shared_repo():
    """Structural proof of `Event != Memory` under a shared EventRepository.

    Wires ConversationService and ExperiencePipeline to the SAME
    InMemoryEventRepository, runs a real conversation turn, and asserts
    every event that turn produced is a conversation event. If a future
    change ever routed a memory write through the conversation path, this
    test would find MEMORY_* in the session's event list.

    A weaker version of this test that hands the pipeline a second,
    unwired repository could not distinguish a genuinely isolated pipeline
    from a broken one, so it is deliberately replaced with the shared
    setup here.
    """
    from personal_ai_core.conversation.factory import build_in_memory_service

    def fake_transport(url, payload, timeout):
        return {
            "model": payload["model"],
            "message": {"content": "ok"},
            "done_reason": "stop",
        }

    service, events = build_in_memory_service(transport=fake_transport)
    # The pipeline shares the very same repository the conversation writes
    # into. This is the setup where a wiring regression would leak.
    pipeline = ExperiencePipeline(
        gate=DefaultPromotionGate(),
        store=InMemoryMemoryRepository(),
        events=events,
    )

    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="I prefer Arabic replies")
    service.send(session_id=session.id, content="my name is Roben")

    for event in events.list_for_session(session.id):
        assert not event.type.value.startswith("memory."), (
            f"conversation path emitted a memory event: {event.type.value}"
        )

    # A separate pipeline call on the same shared repository still keeps
    # its events out of the conversation event stream when nothing wired
    # ConversationService to the pipeline. This confirms the pipeline can
    # coexist without the invariant relying on repository separation.
    pipeline.ingest(ExperienceRecord(
        session_id="another-session", text="I prefer concise answers"
    ))
    # The pipeline's memory events land in the shared repo but under a
    # different session; the conversation session's stream is unaffected.
    conversation_stream = events.list_for_session(session.id)
    for event in conversation_stream:
        assert not event.type.value.startswith("memory.")
