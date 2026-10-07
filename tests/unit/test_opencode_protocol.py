"""The native client protocol keeps tool identities and rejects lossy input."""
import copy

import pytest

from personal_ai_core.integrations.opencode.protocol import (
    OpenCodeChatRequest,
    OpenCodeMessage,
    ProtocolError,
)


def tool(name="read_file"):
    return {"type": "function", "function": {
        "name": name, "description": "Read a UTF-8 file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False},
    }}


def call(identifier="call_1", name="read_file", arguments='{"path": "README.md"}'):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": arguments,
    }}


def request(messages=None, **kwargs):
    return {"model": "pac", "messages": messages if messages is not None else [
        {"role": "user", "content": "Explain this repository."}
    ], **kwargs}


def exchange():
    return [
        {"role": "system", "content": "Read the file before answering."},
        {"role": "user", "content": "What is in README.md?"},
        {"role": "assistant", "content": None, "tool_calls": [call()]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "# Project"},
    ]


def test_complete_native_exchange_and_schema_round_trip_without_changing_roles():
    data = request(exchange(), tools=[tool()], stream=False)
    original = copy.deepcopy(data)
    parsed = OpenCodeChatRequest.from_dict(data)
    assert parsed.to_dict() == original
    assert data == original
    assert parsed.messages[2].tool_calls[0].arguments == '{"path": "README.md"}'
    assert parsed.messages[3].role == "tool"
    assert parsed.messages[3].tool_call_id == "call_1"
    declaration = parsed.tools[0].declaration()
    assert declaration.name == "read_file"
    assert declaration.parameters == tool()["function"]["parameters"]


def test_parallel_tool_calls_keep_ids_with_results_in_a_different_order():
    data = request([
        {"role": "user", "content": "Read both files."},
        {"role": "assistant", "content": "Reading files.", "tool_calls": [
            call("call_a"), call("call_b", arguments='{"path":"pyproject.toml"}'),
        ]},
        {"role": "tool", "tool_call_id": "call_b", "content": "[project]"},
        {"role": "tool", "tool_call_id": "call_a", "content": "# Project"},
    ], parallel_tool_calls=True)
    parsed = OpenCodeChatRequest.from_dict(data)
    assert parsed.to_dict()["messages"] == data["messages"]
    assert parsed.options["parallel_tool_calls"] is True


def test_text_parts_are_supported_and_other_content_is_rejected():
    message = OpenCodeMessage.from_dict({"role": "user", "content": [
        {"type": "text", "text": "Read README.md."},
        {"type": "text", "text": "Explain it."},
    ]})
    assert message.content == "Read README.md.\nExplain it."
    with pytest.raises(ProtocolError, match="text"):
        OpenCodeMessage.from_dict({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]})


@pytest.mark.parametrize("choice", ["auto", "none", "required", {
    "type": "function", "function": {"name": "read_file"},
}])
def test_tool_choice_is_preserved_exactly(choice):
    parsed = OpenCodeChatRequest.from_dict(request(tools=[tool()], tool_choice=choice))
    assert parsed.tool_choice == choice
    assert parsed.to_dict()["tool_choice"] == choice


def test_schema_strictness_and_nested_values_are_preserved():
    definition = tool()
    definition["function"]["strict"] = True
    definition["function"]["parameters"]["$defs"] = {"state": {"enum": ["a", "b"]}}
    parsed = OpenCodeChatRequest.from_dict(request(tools=[definition]))
    assert parsed.tools[0].strict is True
    assert parsed.tools[0].to_dict() == definition


def test_sampling_output_and_stream_usage_options_are_preserved():
    options = {"temperature": 0.7, "top_p": 0.8, "max_tokens": 512,
               "seed": 42, "stop": ["END"], "frequency_penalty": 0.1,
               "presence_penalty": 0.2, "parallel_tool_calls": False,
               "stream_options": {"include_usage": True}}
    parsed = OpenCodeChatRequest.from_dict(request(stream=True, **options))
    assert parsed.options == options
    assert parsed.stream is True
    assert all(parsed.to_dict()[key] == value for key, value in options.items())


def test_opencode_store_false_is_supported_without_authorizing_transcript_storage():
    parsed = OpenCodeChatRequest.from_dict(request(store=False))
    assert parsed.options["store"] is False
    assert parsed.to_dict()["store"] is False
    for value in (True, 0, "false", None):
        with pytest.raises(ProtocolError, match="store:false"):
            OpenCodeChatRequest.from_dict(request(store=value))


@pytest.mark.parametrize("messages, match", [
    ([{"role": "tool", "tool_call_id": "orphan", "content": "output"}], "matching"),
    (exchange()[:-1], "corresponding"),
    (exchange()[:-1] + [{"role": "user", "content": "Continue."}], "before"),
    (exchange() + [exchange()[-1]], "matching"),
    (exchange()[:-1] + [{**exchange()[-1], "tool_call_id": "wrong"}], "matching"),
    (exchange()[:-1] + [{**exchange()[-1], "name": "write_file"}], "name differs"),
    (exchange() + exchange()[2:], "unique"),
])
def test_invalid_tool_pairing_is_refused(messages, match):
    with pytest.raises(ProtocolError, match=match):
        OpenCodeChatRequest.from_dict(request(messages))


@pytest.mark.parametrize("value, match", [
    (request(messages=[]), "nonempty"),
    (request(model=""), "model"),
    (request(stream="true"), "stream"),
    (request(tools=False), "array"),
    (request(tools=[tool(), tool()]), "unique"),
    (request(tool_choice="required"), "at least"),
    (request(tools=[tool()], tool_choice={"type": "function", "function": {"name": "unknown"}}), "declared"),
    (request(response_format={"type": "json_object"}), "response_format"),
    (request(n=2), "n"),
    (request(logprobs=True), "logprobs"),
    (request(temperature=float("nan")), "finite"),
    (request(max_tokens=True), "integer"),
    (request(max_tokens=0), "positive"),
    (request(max_tokens=1, max_completion_tokens=1), "choose"),
    (request(parallel_tool_calls="yes"), "boolean"),
    (request(stream_options={"include_usage": "yes"}), "boolean"),
    (request(stream_options={"other": True}), "other"),
])
def test_unrepresentable_or_invalid_request_fields_are_refused(value, match):
    with pytest.raises(ProtocolError, match=match):
        OpenCodeChatRequest.from_dict(value)


@pytest.mark.parametrize("message, match", [
    ({"role": "developer", "content": "rule"}, "role"),
    ({"role": "user", "content": None}, "content"),
    ({"role": "user", "content": "x", "tool_call_id": "call_1"}, "only on tool"),
    ({"role": "user", "content": "x", "tool_calls": [call()]}, "only on assistant"),
    ({"role": "assistant", "content": "x", "tool_calls": False}, "array"),
    ({"role": "tool", "content": "output"}, "tool_call_id"),
    ({"role": "assistant", "content": "x", "function_call": {"name": "old"}}, "function_call"),
])
def test_invalid_message_shapes_are_refused(message, match):
    with pytest.raises(ProtocolError, match=match):
        OpenCodeMessage.from_dict(message)


@pytest.mark.parametrize("arguments", ["not json", "[]", "null", '{"x":NaN}', '{"x":1e500}', {}])
def test_call_arguments_are_validated_without_coercing_them(arguments):
    messages = exchange()
    messages[2]["tool_calls"][0]["function"]["arguments"] = arguments
    with pytest.raises(ProtocolError, match="arguments"):
        OpenCodeChatRequest.from_dict(request(messages))


def test_unknown_empty_fields_can_be_omitted_but_unknown_instructions_cannot():
    parsed = OpenCodeChatRequest.from_dict(request(metadata=None, reasoning_effort=""))
    assert "metadata" not in parsed.to_dict()
    with pytest.raises(ProtocolError, match="reasoning_effort"):
        OpenCodeChatRequest.from_dict(request(reasoning_effort="high"))
