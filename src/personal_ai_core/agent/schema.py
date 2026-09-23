"""Argument validation against a tool's declared `input_schema`.

A deliberately small subset of JSON Schema -- `type: object`, `properties`
with `type` of string / integer / boolean, `required`, and
`additionalProperties: false` -- because that is all the tools declare, and a
validator that accepts keywords it does not check reports schemas as enforced
when they are not. An unsupported keyword is an error in the SCHEMA, raised
when the tool is registered, not ignored when it runs.

Arguments come from a planner, which may be a model: untrusted input. They are
checked here before the policy's decision is acted on and before any tool sees
them.
"""
from __future__ import annotations

from typing import Any, Mapping

_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
}
_SUPPORTED_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_SUPPORTED_PROPERTY_KEYS = frozenset({"type", "description"})


class SchemaError(ValueError):
    """A tool declared a schema this validator cannot enforce."""


def check_schema(schema: Mapping[str, Any]) -> None:
    unsupported = set(schema) - _SUPPORTED_OBJECT_KEYS
    if unsupported:
        raise SchemaError(f"unsupported schema keywords: {sorted(unsupported)}")
    if schema.get("type") != "object":
        raise SchemaError("a tool's input schema must be an object")
    for name, prop in schema.get("properties", {}).items():
        extra = set(prop) - _SUPPORTED_PROPERTY_KEYS
        if extra:
            raise SchemaError(f"unsupported keywords for {name!r}: {sorted(extra)}")
        if prop.get("type") not in _TYPES:
            raise SchemaError(f"unsupported type for {name!r}: {prop.get('type')!r}")
    for name in schema.get("required", []):
        if name not in schema.get("properties", {}):
            raise SchemaError(f"required argument {name!r} is not a declared property")


def validate_arguments(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Every way `arguments` violates `schema`. Empty means valid."""
    problems: list[str] = []
    properties: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments:
            problems.append(f"missing required argument: {name}")
    for name, value in arguments.items():
        prop = properties.get(name)
        if prop is None:
            if schema.get("additionalProperties", True) is False:
                problems.append(f"unexpected argument: {name}")
            continue
        expected = _TYPES[prop["type"]]
        # bool is an int subclass; an integer argument must not accept True.
        if not isinstance(value, expected) or (
            prop["type"] == "integer" and isinstance(value, bool)
        ):
            problems.append(f"{name} must be {prop['type']}, got {type(value).__name__}")
    return problems
