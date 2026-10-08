"""Native messages and tool schemas consume PAC's real 8192-token budget."""
from dataclasses import dataclass

from personal_ai_core.context.budget import ReserveBasedBudgetPolicy
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.identity.composer import DefaultIdentityComposer
from personal_ai_core.integrations.opencode.budget import (
    OpenCodeContextBudget,
    canonical_json,
)
from personal_ai_core.integrations.opencode.protocol import (
    OpenCodeMessage,
    OpenCodeTool,
    OpenCodeToolCall,
)


@dataclass(frozen=True)
class Spec:
    context_window: int = 8192
    name: str = "boss"
    provider: str = "ollama"


def budget(profile=""):
    estimator = ScriptAwareTokenEstimator()
    identity = DefaultIdentityComposer(profile=profile)
    policy = ReserveBasedBudgetPolicy(
        identity_reserve=identity.tokens(estimator), generation_reserve=1024,
        overhead=256, guard_reserve=100,
    )
    return OpenCodeContextBudget(estimator, policy), estimator, identity


def schema(description="Read a file."):
    return OpenCodeTool(name="read_file", description=description, parameters={
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }, strict=True)


def exchange(output="# README"):
    return (
        OpenCodeMessage(role="user", content="Read README.md."),
        OpenCodeMessage(role="assistant", content=None, tool_calls=(
            OpenCodeToolCall(id="call_1", name="read_file", arguments='{"path":"README.md"}'),
        )),
        OpenCodeMessage(role="tool", content=output, tool_call_id="call_1", name="read_file"),
    )


def test_ordinary_request_has_visible_production_identity_and_output_reserves():
    meter, estimator, identity = budget()
    messages = (OpenCodeMessage(role="user", content="Hello there."),)
    measured = meter.measure(Spec(), messages, ())
    assert measured.identity_tokens == identity.tokens(estimator)
    assert measured.transcript_tokens > estimator.estimate(messages[0].content or "")
    assert measured.tool_schema_tokens == 0
    assert measured.allocation.context_window == 8192
    assert measured.allocation.generation_reserve == 1024
    assert measured.allocation.guard == 100
    assert measured.allocation.history == measured.transcript_tokens
    assert measured.allocation.overcommitted is False
    assert measured.to_dict()["spoken_for"] == measured.allocation.spoken_for


def test_full_schema_and_native_call_result_metadata_are_measured():
    meter, estimator, _ = budget()
    messages, tools = exchange(), (schema(),)
    measured = meter.measure(Spec(), messages, tools)
    assert measured.transcript_tokens == estimator.estimate(canonical_json(
        [message.to_dict() for message in messages]
    ))
    assert measured.tool_schema_tokens == estimator.estimate(canonical_json(
        [tool.to_dict() for tool in tools]
    ))
    content_only = sum(estimator.estimate(message.content or "") for message in messages)
    assert measured.transcript_tokens > content_only
    assert measured.allocation.history == measured.transcript_tokens + measured.tool_schema_tokens


def test_oversized_tool_output_is_preserved_and_signals_overcommitment():
    meter, _, _ = budget()
    output = "Arabic output: هذا ناتج كامل للأداة. " * 1200
    messages = exchange(output)
    measured = meter.measure(Spec(), messages, (schema(),))
    assert measured.allocation.overcommitted
    assert measured.allocation.evidence == 0
    assert messages[-1].content == output
    assert measured.to_dict()["overcommitted"] is True


def test_oversized_schema_is_not_cut_to_make_the_request_fit():
    meter, _, _ = budget()
    definition = schema(description="Documentation " * 3000)
    measured = meter.measure(Spec(), exchange(), (definition,))
    assert measured.allocation.overcommitted
    assert measured.tool_schema_tokens > 8192
    assert definition.description == "Documentation " * 3000


def test_arabic_profile_consumes_identity_reserve_at_script_aware_cost():
    arabic_meter, _, _ = budget("أنا أعمل على مشروعات البرمجيات وأفضل الإجابات الدقيقة. " * 120)
    plain_meter, _, _ = budget()
    messages = exchange()
    plain = plain_meter.measure(Spec(), messages, ())
    arabic = arabic_meter.measure(Spec(), messages, ())
    assert arabic.identity_tokens > plain.identity_tokens + 3000
    assert arabic.transcript_tokens == plain.transcript_tokens
    assert arabic.allocation.spoken_for > plain.allocation.spoken_for
    assert arabic.allocation.evidence < plain.allocation.evidence


def test_complete_history_can_cross_8192_without_silent_windowing():
    meter, _, _ = budget()
    messages = tuple(OpenCodeMessage(role=role, content="Detailed message " * 80)
                     for _ in range(20) for role in ("user", "assistant"))
    measured = meter.measure(Spec(), messages, ())
    assert measured.allocation.overcommitted
    assert len(messages) == 40
    assert measured.transcript_tokens > 8192


def test_schema_addition_can_change_an_8192_request_from_fit_to_overflow():
    meter, _, _ = budget()
    definition = schema(description="Read and inspect files. " * 50)
    found = False
    for size in range(10_000, 18_000, 100):
        messages = (OpenCodeMessage(role="user", content="x" * size),)
        plain = meter.measure(Spec(), messages, ())
        with_schema = meter.measure(Spec(), messages, (definition,))
        if not plain.allocation.overcommitted and with_schema.allocation.overcommitted:
            found = True
            assert with_schema.allocation.context_window == 8192
            assert plain.transcript_tokens == with_schema.transcript_tokens
            break
    assert found, "the test must exercise the 8192-token boundary"
