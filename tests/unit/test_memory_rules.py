"""Extraction rules: deterministic, side-effect-free, and one type each.

Each rule produces exactly one `MemoryType`. That mapping is a contract:
adding a type to the enum without a rule that produces it would make the
type unreachable, and adding a rule without a type would violate the
producer accountability the corrected Phase 3 scope requires.
"""
from __future__ import annotations

from personal_ai_core.core.memory import ExperienceRecord, MemoryType
from personal_ai_core.memory.rules import (
    CorrectionRule,
    ExplicitInstructionRule,
    InferenceRule,
    RepetitionRule,
    default_rules,
)


def _experience(text: str, **signals) -> ExperienceRecord:
    return ExperienceRecord(session_id="s1", text=text, signals=signals)


def test_explicit_instruction_matches_english_prefer():
    rule = ExplicitInstructionRule()
    (candidate,) = rule.propose(_experience("I prefer concise answers"))
    assert candidate.type is MemoryType.PREFERENCES
    assert candidate.confidence >= 0.75
    assert candidate.rule == "rule:explicit_instruction"


def test_explicit_instruction_matches_arabic_prefer():
    rule = ExplicitInstructionRule()
    (candidate,) = rule.propose(_experience("أفضل الردود القصيرة"))
    assert candidate.type is MemoryType.PREFERENCES


def test_explicit_instruction_ignores_unrelated_text():
    rule = ExplicitInstructionRule()
    assert rule.propose(_experience("what's the weather like")) == ()


def test_correction_rule_matches_english_only():
    rule = CorrectionRule()
    assert rule.propose(_experience("no, actually it's blue"))
    assert rule.propose(_experience("that's wrong, it's Tuesday"))
    assert rule.propose(_experience("correction: it's Rico Hunt"))
    # Arabic correction is deliberately not detected -- the rule is
    # English-only and this test documents that gap rather than papering over it.
    assert rule.propose(_experience("لا، بل الأزرق")) == ()


def test_correction_rule_produces_lessons():
    rule = CorrectionRule()
    (candidate,) = rule.propose(_experience("no, actually it's blue"))
    assert candidate.type is MemoryType.LESSONS


def test_repetition_rule_requires_threshold_and_produces_semantic():
    rule = RepetitionRule(threshold=3)
    below = _experience("Rico is a career agent", repetition_count=2)
    assert rule.propose(below) == ()

    at_threshold = _experience("Rico is a career agent", repetition_count=3)
    (candidate,) = rule.propose(at_threshold)
    assert candidate.type is MemoryType.SEMANTIC
    assert candidate.confidence >= 0.6

    at_high = _experience("Rico is a career agent", repetition_count=6)
    (higher,) = rule.propose(at_high)
    assert higher.confidence >= candidate.confidence


def test_repetition_rule_ignores_missing_signal():
    rule = RepetitionRule()
    assert rule.propose(_experience("just chatting")) == ()


def test_inference_rule_matches_first_person_self_reports():
    rule = InferenceRule()
    (a,) = rule.propose(_experience("my name is Roben"))
    assert a.type is MemoryType.EPISODIC
    (b,) = rule.propose(_experience("I live in Dubai"))
    assert b.type is MemoryType.EPISODIC
    (c,) = rule.propose(_experience("اسمي روبن"))
    assert c.type is MemoryType.EPISODIC


def test_inference_rule_ignores_unrelated_text():
    rule = InferenceRule()
    assert rule.propose(_experience("what time is it")) == ()


def test_rules_are_deterministic():
    rule = ExplicitInstructionRule()
    experience = _experience("I prefer concise answers")
    first = rule.propose(experience)
    second = rule.propose(experience)
    assert len(first) == len(second) == 1
    assert first[0].content == second[0].content
    assert first[0].type == second[0].type
    assert first[0].confidence == second[0].confidence


def test_default_rules_include_the_four_declared_rules():
    ids = {r.rule_id for r in default_rules()}
    assert ids == {
        "rule:explicit_instruction",
        "rule:correction",
        "rule:repetition",
        "rule:inference",
    }


def test_each_default_rule_declares_the_type_it_produces():
    produced = {r.rule_id: r.produces for r in default_rules()}
    assert produced == {
        "rule:explicit_instruction": MemoryType.PREFERENCES,
        "rule:correction": MemoryType.LESSONS,
        "rule:repetition": MemoryType.SEMANTIC,
        "rule:inference": MemoryType.EPISODIC,
    }


def test_every_declared_memory_type_has_a_rule_that_produces_it():
    """Producer accountability: no MemoryType member without a rule."""
    produced = {r.produces for r in default_rules()}
    for member in MemoryType:
        assert member in produced, (
            f"MemoryType.{member.name} has no rule that produces it. "
            "Either implement a producer or drop the member."
        )
