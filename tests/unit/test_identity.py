"""Identity reaches every model call, first, and is never persisted.

ADR-011 fixed the contract's shape and its governance; ADR-012 wrote its text.
This file checks the three claims that would otherwise be sentences in a
document with nothing behind them:

  present in every call     including the path that retrieves nothing, which
                            is the one the live smoke test exercises
  first in the prompt       ahead of evidence, because rule 5 decides which
                            of the two wins and it has to be read first
  never persisted           identity is composed per turn; storing it would
                            charge the budget for the same text every turn

Assertions are on the payload the transport received wherever the claim is
about what was sent. Asserting on the composer would re-check the composer.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from personal_ai_core.context.budget import ReserveBasedBudgetPolicy
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.conversation.grounding import GROUNDING_PREAMBLE
from personal_ai_core.conversation.service import ConversationService
from personal_ai_core.core.config import Settings
from personal_ai_core.core.contracts import IdentityComposer
from personal_ai_core.core.domain import Role
from personal_ai_core.core.identity import BehavioralContract, ResponsePolicy
from personal_ai_core.core.knowledge import Document
from personal_ai_core.identity import (
    BEHAVIORAL_CONTRACT,
    RESPONSE_POLICY,
    RULES,
    DefaultIdentityComposer,
)
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from personal_ai_core.runtime.model_registry import ModelRegistry
from personal_ai_core.runtime.ollama.provider import OllamaProvider


class RecordingTransport:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": "ok"}}

    @property
    def last(self) -> Mapping[str, Any]:
        assert self.payloads, "the provider was never called"
        return self.payloads[-1]


def _turn(service, transport, content="hello"):
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content=content)
    return transport.last


# --- the types refuse to be empty -----------------------------------------


def test_a_contract_with_no_rules_is_rejected():
    """The failure ADR-011 records having suffered: a rewrite left a
    "behavioural contract" with no rules in it. An empty contract composes an
    empty section into every prompt and reads as though rules were present."""
    with pytest.raises(ValueError, match="not a contract"):
        BehavioralContract(rules=())


def test_a_blank_rule_is_rejected():
    with pytest.raises(ValueError, match="rule 2"):
        BehavioralContract(rules=("something", "   "))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"language_and_register": "  ", "answer_discipline": "x"},
        {"language_and_register": "x", "answer_discipline": ""},
    ],
)
def test_a_blank_policy_field_is_rejected(kwargs):
    with pytest.raises(ValueError, match="must not be empty"):
        ResponsePolicy(**kwargs)


def test_rule_numbers_come_from_the_order_not_the_text():
    """Numbering written into the rules would let two rules share a number
    after an insertion -- a defect that survives review because nobody reads
    a list for arithmetic."""
    rendered = BehavioralContract(rules=("first", "second", "third")).render()
    assert rendered.splitlines() == ["1. first", "2. second", "3. third"]

    inserted = BehavioralContract(rules=("first", "new", "second")).render()
    assert inserted.splitlines() == ["1. first", "2. new", "3. second"]


# --- the text is tied to the failures it answers ---------------------------


def test_the_contract_carries_every_rule_ADR_012_states():
    """Five rules, not three. Rules 4 and 5 close an omission: ADR-011 carried
    three of the five assets PHASE_1_RECONCILIATION marks ADAPT, and
    EVIDENCE_CONTRACT and UNTRUSTED_METADATA_RULE appeared in it zero times.

    A count, not a wording match: ADR-011 question 7 governs wording changes,
    and a test that pins every word would fail for a typo fix while missing a
    deleted rule."""
    assert len(RULES) == 5
    assert BEHAVIORAL_CONTRACT.rules == RULES


def test_the_policy_answers_the_dialect_escalation():
    """The 2026-07-21 escalation is the reason this rule exists: a Jordanian
    user addressed in hardcoded Gulf dialect. If the rule that answers it ever
    disappears, this says so."""
    text = RESPONSE_POLICY.language_and_register
    assert "Modern Standard Arabic" in text
    assert "dialect" in text


def test_the_rule_that_answers_prompt_injection_is_present():
    """Rule 5 answers a surface in built code: grounding.py puts retrieved
    user documents into a Role.SYSTEM message, the same role the contract
    arrives in."""
    assert any("data, not" in rule and "instructions" in rule for rule in RULES)


def test_no_answer_length_number_appears_in_the_text():
    """The budget owns the number and num_predict enforces it. A number here
    would be a second source for it, and the two would disagree the moment
    either changed."""
    assert not any(character.isdigit() for character in RESPONSE_POLICY.render())
    assert not any(character.isdigit() for rule in RULES for character in rule)


# --- the composer ----------------------------------------------------------


def test_the_default_composer_satisfies_the_protocol():
    assert isinstance(DefaultIdentityComposer(), IdentityComposer)


def test_the_composed_message_is_a_system_message_for_this_session():
    message = DefaultIdentityComposer().compose(session_id="s-1")
    assert message.role is Role.SYSTEM
    assert message.session_id == "s-1"
    assert message.content


def test_the_composed_text_contains_both_halves():
    composer = DefaultIdentityComposer()
    assert RESPONSE_POLICY.render() in composer.text
    assert BEHAVIORAL_CONTRACT.render() in composer.text


# --- what actually reaches the provider ------------------------------------


def test_the_ungrounded_path_sends_identity_first():
    """The path with no retrieval wired -- the one the live smoke test uses,
    and the one where a contract conditional on retrieval would go missing."""
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    payload = _turn(service, transport)

    messages = payload["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == DefaultIdentityComposer().text
    assert [m["role"] for m in messages] == ["system", "user"]


def test_the_grounded_path_sends_identity_before_the_evidence():
    """Order is the claim (ADR-011 implementation boundary, rule 3): the rules
    that decide what to do with a retrieved document are read before it."""
    transport = RecordingTransport()
    slice_ = build_grounded_in_memory_service(transport=transport)
    slice_.ingestion.ingest(
        Document(source_uri="file:///n.md", declared_language="en"),
        "reciprocal rank fusion combines ranked lists.",
    )
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    messages = transport.last["messages"]
    assert messages[0]["content"] == DefaultIdentityComposer().text
    assert GROUNDING_PREAMBLE in messages[1]["content"]


def test_identity_is_present_even_when_nothing_is_retrieved():
    """A grounded service whose index is empty produces no evidence block. The
    contract is not conditional on there being evidence."""
    transport = RecordingTransport()
    slice_ = build_grounded_in_memory_service(transport=transport)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="anything")

    messages = transport.last["messages"]
    assert messages[0]["content"] == DefaultIdentityComposer().text
    assert not any(GROUNDING_PREAMBLE in m["content"] for m in messages)


def test_identity_is_sent_on_every_turn_not_just_the_first():
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="one")
    service.send(session_id=session.id, content="two")

    for payload in transport.payloads:
        assert payload["messages"][0]["content"] == DefaultIdentityComposer().text


# --- and is never written down ---------------------------------------------


def test_identity_is_never_persisted():
    """ADR-011 implementation boundary, rule 2: identity is not conversation
    memory. Persisting it would also charge the budget for the same text on
    every later turn -- the defect the grounding message avoids the same way."""
    transport = RecordingTransport()
    messages = InMemoryMessageRepository()
    # Wired by hand so the repository is the test's own object: reaching into
    # `service._messages` would make this test depend on a private attribute
    # and stop checking the wiring the moment the wiring changed shape.
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=messages,
        events=InMemoryEventRepository(),
        provider=OllamaProvider("http://unused", transport=transport),
        registry=ModelRegistry.from_settings(Settings()),
        budget_policy=ReserveBasedBudgetPolicy(),
        identity=DefaultIdentityComposer(),
    )
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="one")
    service.send(session_id=session.id, content="two")

    stored = messages.list_for_session(session.id)
    assert [m.role for m in stored] == [
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.ASSISTANT,
    ]
    assert all(Role.SYSTEM is not m.role for m in stored)


# --- the budget share is measured, not invented ----------------------------


def test_the_identity_share_is_funded_from_the_text_that_is_sent():
    """ADR-005: a budget is derived and its inputs recorded. The share was
    zero while identity/ did not exist, because any other figure would have
    been invented. Now it is the measured cost of the composed text -- and a
    constant would be an invented figure again."""
    expected = ScriptAwareTokenEstimator().estimate(DefaultIdentityComposer().text)
    assert expected > 0

    transport = RecordingTransport()
    slice_ = build_grounded_in_memory_service(transport=transport)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="anything")

    assembled = next(
        e for e in slice_.events.all() if e.type.value == "context.assembled"
    )
    assert assembled.payload["identity_reserve"] == expected


def test_the_budget_source_names_the_funded_identity_share():
    """A share absent from `source` would be one nobody could see in the
    record -- ADR-005's untraceable budget in miniature."""
    transport = RecordingTransport()
    slice_ = build_grounded_in_memory_service(transport=transport)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="anything")

    expected = ScriptAwareTokenEstimator().estimate(DefaultIdentityComposer().text)
    assembled = next(
        e for e in slice_.events.all() if e.type.value == "context.assembled"
    )
    assert f"identity={expected}" in assembled.payload["budget_source"]
