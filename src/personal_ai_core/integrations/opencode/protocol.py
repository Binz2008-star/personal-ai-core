"""Validated native chat messages without changing PAC's existing domain schema.

OpenCode owns the tool executor. These values preserve assistant calls and
tool results as native messages; a result is never disguised as user text.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...core.domain import ToolDeclaration


class ProtocolError(ValueError):
    """A request cannot be represented by the supported chat protocol."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ProtocolError(f"{label} must be an object with string keys")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{label} must be a nonempty string")
    return value


def _supported(data: Mapping[str, Any], keys: set[str], label: str) -> None:
    for key, value in data.items():
        if key not in keys and value not in (None, "", [], {}):
            raise ProtocolError(f"unsupported {label} field: {key}")


def _json(value: Any, label: str) -> None:
    """Reject values JSON would quietly coerce or encode outside its standard."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json(item, label)
        return
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value):
        for item in value.values():
            _json(item, label)
        return
    raise ProtocolError(f"{label} must contain only valid JSON values")


@dataclass(frozen=True, slots=True)
class OpenCodeToolCall:
    id: str
    name: str
    arguments: str

    @classmethod
    def from_dict(cls, value: Any) -> OpenCodeToolCall:
        data = _object(value, "tool call")
        _supported(data, {"id", "type", "function"}, "tool call")
        if data.get("type") != "function":
            raise ProtocolError("tool call type must be function")
        function = _object(data.get("function"), "tool call function")
        _supported(function, {"name", "arguments"}, "tool call function")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ProtocolError("tool call arguments must be a JSON object string")
        try:
            parsed = json.loads(arguments, parse_constant=lambda value: _bad_constant(value))
        except (ValueError, TypeError) as exc:
            raise ProtocolError("tool call arguments must be a JSON object string") from exc
        if not isinstance(parsed, dict):
            raise ProtocolError("tool call arguments must be a JSON object string")
        _json(parsed, "tool call arguments")
        return cls(
            id=_string(data.get("id"), "tool call id"),
            name=_string(function.get("name"), "tool call name"),
            arguments=arguments,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


def _bad_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _content(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ProtocolError("message content must be text or text parts")
    parts: list[str] = []
    for item in value:
        part = _object(item, "message content part")
        if part.get("type") != "text" or not isinstance(part.get("text"), str):
            raise ProtocolError("only text message content parts are supported")
        _supported(part, {"type", "text"}, "message content part")
        parts.append(part["text"])
    return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class OpenCodeMessage:
    role: str
    content: str | None = None
    tool_calls: tuple[OpenCodeToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> OpenCodeMessage:
        data = _object(value, "message")
        _supported(data, {"role", "content", "tool_calls", "tool_call_id", "name"}, "message")
        role = data.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise ProtocolError("message role must be system, user, assistant or tool")
        content = _content(data.get("content"))
        raw_calls = data.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise ProtocolError("message tool_calls must be an array")
        calls = tuple(OpenCodeToolCall.from_dict(item) for item in raw_calls)
        name = data.get("name")
        if name is not None:
            name = _string(name, "message name")
        call_id = data.get("tool_call_id")
        if role == "tool":
            call_id = _string(call_id, "tool result tool_call_id")
            if content is None:
                raise ProtocolError("tool result content must be text")
        elif call_id is not None:
            raise ProtocolError("tool_call_id is supported only on tool messages")
        if calls and role != "assistant":
            raise ProtocolError("tool_calls are supported only on assistant messages")
        if content is None and not (role == "assistant" and calls):
            raise ProtocolError("message content is required unless an assistant calls tools")
        return cls(role=role, content=content, tool_calls=calls, tool_call_id=call_id, name=name)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        return data


@dataclass(frozen=True, slots=True)
class OpenCodeTool:
    name: str
    description: str
    parameters: Mapping[str, Any]
    strict: bool | None = None

    @classmethod
    def from_dict(cls, value: Any) -> OpenCodeTool:
        data = _object(value, "tool")
        _supported(data, {"type", "function"}, "tool")
        if data.get("type") != "function":
            raise ProtocolError("tool type must be function")
        function = _object(data.get("function"), "tool function")
        _supported(function, {"name", "description", "parameters", "strict"}, "tool function")
        description = function.get("description", "")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise ProtocolError("tool description must be text")
        parameters = _object(function.get("parameters", {}), "tool parameters")
        _json(parameters, "tool parameters")
        strict = function.get("strict")
        if strict is not None and not isinstance(strict, bool):
            raise ProtocolError("tool strict must be a boolean")
        return cls(
            name=_string(function.get("name"), "tool name"),
            description=description,
            parameters=dict(parameters),
            strict=strict,
        )

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(name=self.name, description=self.description, parameters=self.parameters)

    def to_dict(self) -> dict[str, Any]:
        function: dict[str, Any] = {
            "name": self.name, "description": self.description, "parameters": dict(self.parameters)
        }
        if self.strict is not None:
            function["strict"] = self.strict
        return {"type": "function", "function": function}


def _paired(messages: tuple[OpenCodeMessage, ...]) -> None:
    seen: set[str] = set()
    pending: dict[str, OpenCodeToolCall] = {}
    for message in messages:
        if message.role == "tool":
            call = pending.pop(message.tool_call_id or "", None)
            if call is None:
                raise ProtocolError("tool result has no matching unresolved assistant tool call")
            if message.name is not None and message.name != call.name:
                raise ProtocolError("tool result name differs from its assistant tool call")
            continue
        if pending:
            raise ProtocolError("every assistant tool call needs a result before the next message")
        for call in message.tool_calls:
            if call.id in seen:
                raise ProtocolError("assistant tool call ids must be unique")
            seen.add(call.id)
            pending[call.id] = call
    if pending:
        raise ProtocolError("every assistant tool call needs a corresponding tool result")


_OPTIONS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop",
    "frequency_penalty", "presence_penalty", "parallel_tool_calls", "stream_options", "store",
}


def _options(data: Mapping[str, Any]) -> dict[str, Any]:
    if "store" in data and data["store"] is not False:
        raise ProtocolError("only store:false is supported; completion transcripts are not stored")
    options = {key: data[key] for key in _OPTIONS if key in data and data[key] is not None}
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
        if key in options:
            value = options[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ProtocolError(f"{key} must be a finite number")
    for key in ("max_tokens", "max_completion_tokens", "seed"):
        if key in options and (isinstance(options[key], bool) or not isinstance(options[key], int)):
            raise ProtocolError(f"{key} must be an integer")
    for key in ("max_tokens", "max_completion_tokens"):
        if key in options and options[key] <= 0:
            raise ProtocolError(f"{key} must be positive")
    if "max_tokens" in options and "max_completion_tokens" in options:
        raise ProtocolError("choose max_tokens or max_completion_tokens, not both")
    if "parallel_tool_calls" in options and not isinstance(options["parallel_tool_calls"], bool):
        raise ProtocolError("parallel_tool_calls must be a boolean")
    if "stop" in options:
        stop = options["stop"]
        if not (isinstance(stop, str) or (isinstance(stop, list) and all(isinstance(s, str) for s in stop))):
            raise ProtocolError("stop must be text or an array of text")
    if "stream_options" in options:
        stream_options = _object(options["stream_options"], "stream_options")
        _supported(stream_options, {"include_usage"}, "stream_options")
        if "include_usage" in stream_options and not isinstance(stream_options["include_usage"], bool):
            raise ProtocolError("stream_options.include_usage must be a boolean")
        options["stream_options"] = dict(stream_options)
    return options


def _choice(value: Any, tools: tuple[OpenCodeTool, ...]) -> str | Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in ("auto", "none", "required"):
            raise ProtocolError("tool_choice must be auto, none, required or a named function")
        if value == "required" and not tools:
            raise ProtocolError("tool_choice required needs at least one tool")
        return value
    choice = _object(value, "tool_choice")
    _supported(choice, {"type", "function"}, "tool_choice")
    function = _object(choice.get("function"), "tool_choice function")
    _supported(function, {"name"}, "tool_choice function")
    name = _string(function.get("name"), "tool_choice function name")
    if choice.get("type") != "function" or name not in {tool.name for tool in tools}:
        raise ProtocolError("tool_choice must name a declared function")
    return {"type": "function", "function": {"name": name}}


@dataclass(frozen=True, slots=True)
class OpenCodeChatRequest:
    model: str
    messages: tuple[OpenCodeMessage, ...]
    tools: tuple[OpenCodeTool, ...] = ()
    tool_choice: str | Mapping[str, Any] | None = None
    stream: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> OpenCodeChatRequest:
        data = _object(value, "request")
        _supported(data, {"model", "messages", "tools", "tool_choice", "stream", *_OPTIONS}, "request")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ProtocolError("messages must be a nonempty array")
        messages = tuple(OpenCodeMessage.from_dict(message) for message in raw_messages)
        _paired(messages)
        raw_tools = data.get("tools")
        if raw_tools is None:
            raw_tools = []
        if not isinstance(raw_tools, list):
            raise ProtocolError("tools must be an array")
        tools = tuple(OpenCodeTool.from_dict(tool) for tool in raw_tools)
        if len({tool.name for tool in tools}) != len(tools):
            raise ProtocolError("tool names must be unique")
        stream = data.get("stream", False)
        if not isinstance(stream, bool):
            raise ProtocolError("stream must be a boolean")
        return cls(
            model=_string(data.get("model"), "model"), messages=messages, tools=tools,
            tool_choice=_choice(data.get("tool_choice"), tools), stream=stream,
            options=_options(data),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "model": self.model,
            "messages": [message.to_dict() for message in self.messages],
            "stream": self.stream,
            **dict(self.options),
        }
        if self.tools:
            data["tools"] = [tool.to_dict() for tool in self.tools]
        if self.tool_choice is not None:
            data["tool_choice"] = self.tool_choice
        return data
