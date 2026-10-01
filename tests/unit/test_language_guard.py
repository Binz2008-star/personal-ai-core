"""ADR-019 unit 1: the reply-language check as a pure function.

The violations are the replies the rig produced on 2026-10-01, verbatim;
the passes are replies the scorer passed. Nothing here touches the service.
"""
from __future__ import annotations

import pytest

from personal_ai_core.conversation.language_guard import (
    ARABIC,
    LATIN,
    check_reply,
    letter_counts,
    script_of,
)

DECLINE_Q = "بحسب ملاحظات المشروع، ما هو الرقم التسلسلي لحاسوبي المحمول؟"
MSA_Q = "ما الفرق بين الذاكرة قصيرة المدى والذاكرة طويلة المدى؟ أجب باختصار."
EN_Q = "What is the difference between a thread and a process?"


@pytest.mark.parametrize(
    ("user", "reply"),
    [
        # ground-decline-ar, every run: one Arabic word, then Chinese
        (DECLINE_Q, "。提供的信息中没有提到您的笔记本电脑的序列号。因此，无法根据给定的信息确定您的笔记本电脑的序列号المرجع"),
        (DECLINE_Q, "。提供的信息中没有提及您笔记本电脑的序列号المرجع"),
        # ground-decline-ar under rule B: a correct decline, in English
        (DECLINE_Q, "The information provided does not contain the serial number of your laptop."),
        # lang-ar-msa-1, run 1 of 2026-10-01T142802Z: Chinese mid-reply
        (MSA_Q, "الذاكرة قصيرة المدى تشمل المعلومات التي نتذكرها لفترة قصيرة。甚至到此为止，没有更多的信息或问题需要回答。"),
        # rule A's failure: an English question answered in Arabic
        ("What is the capital of Australia?", "عاصمة أستراليا هي كانبرا."),
        # Cyrillic leaking into English (lang-en-1, run 4)
        (EN_Q, "A thread shares memory with its process; процесс does not."),
    ],
)
def test_the_failures_the_rig_produced_are_violations(user, reply):
    assert check_reply(user, reply).violation


@pytest.mark.parametrize(
    ("user", "reply"),
    [
        (DECLINE_Q, "لا تذكر ملاحظات المشروع الرقم التسلسلي لحاسوبك المحمول؛ تذكر فقط الاسم الرمزي النيل وموعد الإطلاق في مارس."),
        (MSA_Q, "الذاكرة قصيرة المدى تحتفظ بالمعلومات لفترة قصيرة، بينما تحتفظ الذاكرة طويلة المدى بها لفترات طويلة."),
        (EN_Q, "A thread shares memory with its process; a process has its own address space."),
        # Latin names and code inside an Arabic reply are not a language switch
        (MSA_Q, "يمكنك استخدام Python أو PostgreSQL، والذاكرة قصيرة المدى تختلف عن الطويلة في مدة الاحتفاظ."),
        # code in a fence is not counted
        (EN_Q, "Use this:\n```\n# 线程 示例\nthread.start()\n```\nA thread shares memory."),
    ],
)
def test_replies_in_the_users_language_pass(user, reply):
    verdict = check_reply(user, reply)
    assert not verdict.violation, verdict.reason


def test_the_expected_script_comes_from_the_user_message():
    assert check_reply(DECLINE_Q, "x").expected == ARABIC
    assert check_reply(EN_Q, "x").expected == LATIN


@pytest.mark.parametrize("user", ["ok?", "123 456", "مرحبا hello"])
def test_a_short_or_mixed_message_is_undetermined_and_never_a_violation(user):
    verdict = check_reply(user, "。提供的信息中没有提到")
    assert verdict.expected is None
    assert not verdict.violation


@pytest.mark.parametrize(
    "user",
    [
        "ترجم هذه الجملة إلى الصينية: صباح الخير",
        "اكتب الجواب بالإنجليزية من فضلك",
        "Please translate this into Chinese: good morning",
        "Answer in Arabic please: what is a thread?",
    ],
)
def test_a_request_for_another_language_is_exempt(user):
    verdict = check_reply(user, "早上好，你好，谢谢")
    assert not verdict.violation
    assert verdict.exempt == "language request"


def test_a_script_the_user_quoted_is_not_foreign():
    user = "ما معنى العبارة 你好世界 بالعربية؟ أريد شرحاً مفصلاً"
    reply = "العبارة 你好世界 تعني مرحباً بالعالم، وهي جملة شائعة في تعلم البرمجة."
    assert not check_reply(user, reply).violation


def test_a_script_in_the_evidence_is_not_foreign():
    reply = "تقول الملاحظات إن اسم المورد هو 北京科技 وإن العقد ينتهي في مارس القادم."
    assert check_reply(DECLINE_Q, reply).violation
    assert not check_reply(DECLINE_Q, reply, evidence=["المورد: 北京科技"]).violation


def test_two_foreign_letters_are_tolerated_three_are_not():
    base = "الذاكرة قصيرة المدى تحتفظ بالمعلومات لفترة قصيرة جداً"
    assert not check_reply(MSA_Q, base + " 中文").violation
    assert check_reply(MSA_Q, base + " 中文字").violation


def test_the_arabic_share_threshold_is_a_parameter():
    reply = "الذاكرة short-term memory holds information briefly"
    assert check_reply(MSA_Q, reply).violation
    assert not check_reply(MSA_Q, reply, min_arabic_share=0.1).violation


def test_the_verdict_carries_counts_never_text():
    verdict = check_reply(DECLINE_Q, "。提供的信息中没有提到المرجع")
    assert dict(verdict.reply_counts) == {"han": 10, "arabic": 6}
    assert "提供" not in repr(verdict)


def test_script_and_counts():
    assert script_of("a") == LATIN and script_of("ب") == ARABIC
    assert script_of("中") == "han" and script_of("я") == "cyrillic"
    assert script_of("1") is None and script_of(" ") is None
    assert letter_counts("ab 中 ج") == {"latin": 2, "han": 1, "arabic": 1}
