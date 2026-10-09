"""Metadata-only request descriptions; execution and selection remain OpenCode's.

No request text, schema description/default, argument, result, or profile is
returned. Code Mode catalogs are observed only in their generated system
section. Hidden inline listings are not reported as permission denials.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...core.contracts import SecretRedactor
from ...core.redaction import RedactionError
from .budget import OpenCodeBudget
from .protocol import OpenCodeChatRequest

# The authored native names in OpenCode 2.0.22's core tool plugins. `execute`
# belongs to Code Mode and is deliberately separate from this baseline.
BUILTIN_BASELINE = frozenset({
    "read", "write", "edit", "patch", "glob", "grep", "shell", "skill",
    "subagent", "question", "webfetch", "websearch",
})
_GROUPS = {
    "read": "filesystem_read", "glob": "filesystem_read", "grep": "filesystem_read",
    "write": "filesystem_write", "edit": "filesystem_write", "patch": "filesystem_write",
    "shell": "shell", "webfetch": "web", "websearch": "web", "execute": "codemode",
    "skill": "skills", "subagent": "delegation", "question": "interaction",
}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_SUSPECT_NAME = re.compile(
    r"(?:sk-|gh[pousr]_|github_pat_|AKIA|ASIA|xox[baprs]-|eyJ[A-Za-z0-9_-]{12})",
    re.IGNORECASE,
)
_CODEMODE_START = (
    "# Code Mode\n\nUse the `execute` tool to call the tools listed below. "
    "They cannot be called directly"
)
_NO_CODEMODE = (
    "No Code Mode tools are currently available. Later Code Mode catalog updates may add or remove tools. "
    "Do not call `execute` unless there is at least one available Code Mode tool."
)
_NAMESPACE_LINE = re.compile(
    r"^- ([^\s]+) \((\d{1,12}) tools?(?:, (?:(\d{1,12}) shown|(none shown)))?\)(?: //.*)?$"
)
_SEGMENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_OMISSION_REASONS = frozenset({
    "permission_policy", "agent_preset", "context_budget", "upstream_selection",
    "explicit_denial", "not_selected", "configured_policy",
})
_SOURCE_BOUNDARIES = (
    "\n\n<mcp_instructions>",
    "\n\nSkills provide specialized instructions and workflows for specific tasks.",
    "\n\nProject references provide additional directories that can be accessed when relevant.",
)


def _name(value: str, warnings: set[str], redactor: SecretRedactor | None) -> str:
    safe = bool(_IDENTIFIER.fullmatch(value)) and not _SUSPECT_NAME.search(value)
    if safe and redactor is not None:
        try:
            checked = redactor.redact(value)
            safe = checked.total == 0 and checked.text == value
        except RedactionError:
            safe = False
            warnings.add("name_check_failed")
    if safe:
        return value
    warnings.add("suspect_or_invalid_name_withheld")
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _path(line: str) -> tuple[str, tuple[str, ...]] | None:
    """Read only the generated callable expression, never its signature/data."""
    if not line.startswith("  - tools"):
        return None
    text = line[len("  - "):]
    offset = len("tools")
    segments: list[str] = []
    while offset < len(text):
        if text[offset] == ".":
            match = _SEGMENT.match(text, offset + 1)
            if match is None:
                return None
            segments.append(match.group())
            offset = match.end()
        elif text[offset:offset + 2] == '["':
            try:
                value, consumed = json.JSONDecoder().raw_decode(text[offset + 1:])
            except ValueError:
                return None
            offset += consumed + 1
            if not isinstance(value, str) or text[offset:offset + 1] != "]":
                return None
            segments.append(value)
            offset += 1
        else:
            break
    if not segments or text[offset:offset + 1] != "(":
        return None
    return text[:offset], tuple(segments)


def _safe_path(expression: str, segments: tuple[str, ...], warnings: set[str],
               redactor: SecretRedactor | None) -> str:
    if all(_name(segment, warnings, redactor) == segment for segment in segments):
        return expression
    return "sha256:" + hashlib.sha256(expression.encode("utf-8", errors="surrogatepass")).hexdigest()


def _finish_group(budget: OpenCodeBudget, group: dict[str, Any] | None, lines: list[str]) -> None:
    if group is not None:
        group["instruction_tokens"] = budget.estimate_text("\n".join(lines))


def _catalogs(request: OpenCodeChatRequest, budget: OpenCodeBudget,
              warnings: set[str], redactor: SecretRedactor | None) -> dict[str, Any]:
    catalogs: list[dict[str, Any]] = []
    for message_index, message in enumerate(request.messages):
        if message.role != "system" or not message.content:
            continue
        content = message.content
        if _NO_CODEMODE in content:
            catalogs.append({
                "message_index": message_index, "instruction_tokens": budget.estimate_text(_NO_CODEMODE),
                "catalog_complete": True, "namespaces": [], "inventory_count": 0, "inline_count": 0,
                "lazy_not_inline_count": 0,
            })
        cursor = 0
        while (start := content.find(_CODEMODE_START, cursor)) >= 0:
            next_heading = re.search(r"\n# ", content[start + len(_CODEMODE_START):])
            end = (start + len(_CODEMODE_START) + next_heading.start()
                   if next_heading is not None else len(content))
            source_boundary: int | None = None
            for boundary in _SOURCE_BOUNDARIES:
                position = content.find(boundary, start + len(_CODEMODE_START))
                if position >= 0:
                    end = min(end, position)
                    source_boundary = position if source_boundary is None else min(source_boundary, position)
            section = content[start:end].rstrip()
            cursor = end
            marker = "\n## Available tools\n"
            marker_at = section.find(marker)
            if marker_at < 0:
                continue
            preamble = section[:marker_at + len(marker)]
            catalog_lines = section[marker_at + len(marker):].splitlines()
            namespaces: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            group_lines: list[str] = []

            for line in catalog_lines:
                header = _NAMESPACE_LINE.fullmatch(line)
                if header is not None:
                    _finish_group(budget, current, group_lines)
                    count = int(header.group(2))
                    shown = int(header.group(3)) if header.group(3) else (0 if header.group(4) else count)
                    if shown > count:
                        current = None
                        group_lines = []
                        warnings.add("invalid_catalog_counts")
                        continue
                    current = {
                        "name": _name(header.group(1), warnings, redactor), "inventory_count": count,
                        "inline_count": shown, "lazy_not_inline_count": count - shown,
                        "inline_paths": [], "unparsed_inline_count": 0,
                    }
                    namespaces.append(current)
                    group_lines = [line]
                elif current is not None:
                    group_lines.append(line)
                    if line.startswith("  - "):
                        parsed = _path(line)
                        if parsed is None:
                            current["unparsed_inline_count"] += 1
                        else:
                            expression, segments = parsed
                            current["inline_paths"].append(_safe_path(expression, segments, warnings, redactor))
            _finish_group(budget, current, group_lines)
            catalogs.append({
                "message_index": message_index,
                "instruction_tokens": budget.estimate_text(section),
                "preamble_tokens": budget.estimate_text(preamble),
                "catalog_complete": "The catalog is complete. Do not guess tool names." in preamble,
                "namespaces": namespaces,
                "inventory_count": sum(group["inventory_count"] for group in namespaces),
                "inline_count": sum(group["inline_count"] for group in namespaces),
                "lazy_not_inline_count": sum(group["lazy_not_inline_count"] for group in namespaces),
            })
            if source_boundary == end:
                break
    return {
        "present": bool(catalogs), "section_count": len(catalogs), "sections": catalogs,
        "instruction_tokens": sum(catalog["instruction_tokens"] for catalog in catalogs),
        "inventory_semantics": "observed_inline_snapshots_not_permission_decisions",
        "group_estimates_are_additive": False,
    }


def _names(value: Any) -> list[str]:
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or not all(isinstance(name, str) for name in value)):
        raise ValueError("selection diagnostics names must be an array of strings")
    result = list(value)
    if len(set(result)) != len(result):
        raise ValueError("selection diagnostics names must be unique")
    return result


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError("selection diagnostic token counts must be nonnegative integers")
    return value


def describe_request(request: OpenCodeChatRequest, budget: OpenCodeBudget, *,
                     selection_diagnostics: Mapping[str, Any] | None = None,
                     redactor: SecretRedactor | None = None) -> dict[str, Any]:
    """Describe the received set, or an explicit trusted upstream selection.

    The caller owns correlation/policy/outcome envelopes and JSONL persistence.
    No missing MCP or native inventory is invented from a narrowed request.
    """
    warnings: set[str] = set()
    received = [tool.name for tool in request.tools]
    incoming, selected = received, received
    incoming_tokens = selected_tokens = budget.tool_schema_tokens
    omissions: list[dict[str, str]] = []
    visibility = "received_set_only_upstream_inventory_unknown"
    if selection_diagnostics is not None:
        incoming = _names(selection_diagnostics.get("incoming_names"))
        selected = _names(selection_diagnostics.get("selected_names"))
        if selected != received or not set(selected) <= set(incoming):
            raise ValueError("selection diagnostics do not match the received tools")
        incoming_tokens = _count(selection_diagnostics.get("incoming_schema_tokens"))
        selected_tokens = _count(selection_diagnostics.get("selected_schema_tokens"))
        if selected_tokens != budget.tool_schema_tokens or incoming_tokens < selected_tokens:
            raise ValueError("selection diagnostic token counts do not match the measured budget")
        raw_omissions = selection_diagnostics.get("omitted")
        if not isinstance(raw_omissions, list):
            raise ValueError("selection diagnostics omissions must be an array")
        for item in raw_omissions:
            if (not isinstance(item, Mapping) or not isinstance(item.get("name"), str)
                    or item.get("reason") not in _OMISSION_REASONS):
                raise ValueError("selection omission needs a name and a supported reason")
            omissions.append({"name": item["name"], "reason": item["reason"]})
        if (len({item["name"] for item in omissions}) != len(omissions)
                or {item["name"] for item in omissions} != set(incoming) - set(selected)):
            raise ValueError("selection diagnostics must account for every omitted tool exactly once")
        visibility = "trusted_upstream_selection_manifest"
    grouped: dict[str, list[str]] = {}
    for name in received:
        group = _GROUPS.get(name, "mcp_native" if name.startswith("mcp_") else "other_native")
        grouped.setdefault(group, []).append(_name(name, warnings, redactor))
    message_diagnostics = budget.describe_messages(request.messages)
    result = {
        "description_version": "opencode-request-metadata-v1",
        "selection_owner": "opencode", "selection_visibility": visibility,
        "incoming_native_tools": [_name(name, warnings, redactor) for name in incoming],
        "selected_native_tools": [_name(name, warnings, redactor) for name in selected],
        "omitted_native_tools": [{"name": _name(item["name"], warnings, redactor),
                                  "reason": item["reason"]} for item in omissions],
        "native_groups_received": grouped,
        "incoming_tool_schema_tokens": incoming_tokens,
        "selected_tool_schema_tokens": selected_tokens,
        "saved_tool_schema_tokens": incoming_tokens - selected_tokens,
        "builtin_baseline_not_received": sorted(BUILTIN_BASELINE - set(received)),
        "unreceived_builtin_semantics": "absence_from_request_not_a_permission_denial",
        "mcp_omitted_inventory": "unknown_without_upstream_manifest",
        "incoming_messages": message_diagnostics,
        "integration_instruction_increment_tokens": (
            budget.transcript_tokens - message_diagnostics["canonical_transcript_tokens"]
        ),
        "transcript_delta_scope": "integration_instructions_and_redaction",
        "codemode": _catalogs(request, budget, warnings, redactor),
        "authoritative_budget": budget.to_dict(),
    }
    result["warnings"] = sorted(warnings)
    return result


class JsonlAuditWriter:
    """Append one metadata record per call; caller owns callback error policy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def __call__(self, record: Mapping[str, Any]) -> None:
        # Serialize first: invalid/NaN data cannot create or partly append a
        # record. JSON escapes CR/LF inside fields, preserving one record per
        # physical line. Escape the Unicode line separators as well.
        _validate_record(record)
        serialized = json.dumps(dict(record), ensure_ascii=False, allow_nan=False,
                                separators=(",", ":"))
        serialized = serialized.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as destination:
                destination.write(serialized + "\n")


def _keys(value: Any, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("audit record contains missing or unsupported metadata fields")
    return value


def _literal(value: Any, choices: set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValueError("audit record contains unsupported metadata text")


def _safe_identifier(value: Any) -> None:
    if not isinstance(value, str) or not (
        re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        or (_IDENTIFIER.fullmatch(value) and not _SUSPECT_NAME.search(value))
    ):
        raise ValueError("audit record contains an unsafe name")


def _name_list(value: Any) -> None:
    if not isinstance(value, list):
        raise TypeError("audit names must be an array")
    for name in value:
        _safe_identifier(name)


def _validate_record(record: Mapping[str, Any]) -> None:
    """Reject caller text at every nested seam before opening the file."""
    if not isinstance(record, Mapping):
        raise TypeError("audit record must be metadata")
    event_type = record.get("event_type")
    if event_type == "request_completion":
        _validate_completion(record)
        return
    if event_type != "request_admission":
        raise ValueError("audit record has an unsupported event type")
    data = _keys(record, {
        "description_version", "selection_owner", "selection_visibility", "incoming_native_tools",
        "selected_native_tools", "omitted_native_tools", "native_groups_received",
        "incoming_tool_schema_tokens", "selected_tool_schema_tokens", "saved_tool_schema_tokens",
        "builtin_baseline_not_received", "unreceived_builtin_semantics", "mcp_omitted_inventory",
        "incoming_messages", "integration_instruction_increment_tokens", "transcript_delta_scope",
        "codemode", "authoritative_budget", "warnings", "request_correlation_id",
        "selection_policy_version", "request_kind", "outcome", "event_type",
    })
    _literal(data["description_version"], {"opencode-request-metadata-v1"})
    _literal(data["selection_owner"], {"opencode"})
    _literal(data["selection_visibility"], {
        "received_set_only_upstream_inventory_unknown", "trusted_upstream_selection_manifest",
    })
    _literal(data["unreceived_builtin_semantics"], {"absence_from_request_not_a_permission_denial"})
    _literal(data["mcp_omitted_inventory"], {"unknown_without_upstream_manifest"})
    _literal(data["transcript_delta_scope"], {"integration_instructions_and_redaction"})
    _validate_envelope(data)
    _literal(data["outcome"], {
        "admitted", "refused_context_budget", "refused_required_tool_unavailable", "refused_protocol",
    })
    for key in ("incoming_native_tools", "selected_native_tools", "builtin_baseline_not_received"):
        _name_list(data[key])
    for key in ("incoming_tool_schema_tokens", "selected_tool_schema_tokens", "saved_tool_schema_tokens"):
        _count(data[key])
    delta = data["integration_instruction_increment_tokens"]
    if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > 2**63 - 1:
        raise ValueError("audit transcript delta must be a finite integer")
    if not isinstance(data["warnings"], list):
        raise TypeError("audit warnings must be an array")
    for warning in data["warnings"]:
        _literal(warning, {"name_check_failed", "suspect_or_invalid_name_withheld", "invalid_catalog_counts",
                           "invalid_upstream_selection_manifest"})
    if not isinstance(data["omitted_native_tools"], list):
        raise TypeError("audit omitted tools must be an array")
    for omitted in data["omitted_native_tools"]:
        item = _keys(omitted, {"name", "reason"})
        _safe_identifier(item["name"])
        _literal(item["reason"], set(_OMISSION_REASONS))
    groups = data["native_groups_received"]
    if not isinstance(groups, Mapping) or not set(groups) <= set(_GROUPS.values()) | {"mcp_native", "other_native"}:
        raise ValueError("audit native group names must be fixed metadata")
    for names in groups.values():
        _name_list(names)
    budget = _keys(data["authoritative_budget"], {
        "context_window", "identity_tokens", "transcript_tokens", "tool_schema_tokens", "generation_reserve",
        "overhead", "guard_reserve", "spoken_for", "remaining_tokens", "overcommitted",
    })
    for key, value in budget.items():
        if key == "overcommitted":
            if not isinstance(value, bool):
                raise ValueError("audit budget overflow must be a boolean")
        else:
            _count(value)
    messages = _keys(data["incoming_messages"], {
        "canonical_transcript_tokens", "system_canonical_tokens", "non_system_increment_tokens",
        "role_counts", "per_message", "standalone_estimates_are_additive",
    })
    for key in ("canonical_transcript_tokens", "system_canonical_tokens", "non_system_increment_tokens"):
        _count(messages[key])
    if messages["standalone_estimates_are_additive"] is not False:
        raise ValueError("audit message diagnostics must declare separate rounding")
    for value in _keys(messages["role_counts"], {"system", "user", "assistant", "tool"}).values():
        _count(value)
    if not isinstance(messages["per_message"], list):
        raise TypeError("audit message diagnostics must be an array")
    for value in messages["per_message"]:
        message = _keys(value, {"index", "role", "estimated_tokens"})
        _count(message["index"])
        _count(message["estimated_tokens"])
        _literal(message["role"], {"system", "user", "assistant", "tool", "unknown_role"})
    _validate_catalog(data["codemode"])


def _validate_envelope(data: Mapping[str, Any]) -> None:
    _literal(data["request_kind"], {"primary", "title", "summary", "compaction", "auxiliary", "generate"})
    if not isinstance(data["request_correlation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["request_correlation_id"]):
        raise ValueError("audit correlation ID must be an integration UUID")
    _safe_identifier(data["selection_policy_version"])


def _validate_completion(record: Mapping[str, Any]) -> None:
    base = {"event_type", "request_correlation_id", "selection_policy_version", "request_kind", "outcome"}
    optional = {"tool_call_count", "finish_reason", "prompt_tokens", "completion_tokens"}
    outcome = record.get("outcome")
    _literal(outcome, {"generated_text", "generated_tool_calls", "provider_error", "protocol_error"})
    allowed_optional = set(record) - base
    if (not allowed_optional <= optional
            or (outcome in {"provider_error", "protocol_error"} and allowed_optional)):
        raise ValueError("audit completion contains unsupported metadata fields")
    data = _keys(record, base | allowed_optional)
    _validate_envelope(data)
    if "finish_reason" in data:
        _literal(data["finish_reason"], {"stop", "length", "tool_calls"})
    for key in ("tool_call_count", "prompt_tokens", "completion_tokens"):
        if key in data:
            _count(data[key])
    if "tool_call_count" in data:
        if outcome == "generated_text" and data["tool_call_count"] != 0:
            raise ValueError("text completion cannot contain tool calls")
        if outcome == "generated_tool_calls" and data["tool_call_count"] < 1:
            raise ValueError("tool completion must contain at least one tool call")
    if "finish_reason" in data and (
        (outcome == "generated_tool_calls" and data["finish_reason"] != "tool_calls")
        or (outcome == "generated_text" and data["finish_reason"] == "tool_calls")
    ):
        raise ValueError("audit completion finish reason conflicts with its outcome")


def _validate_catalog(catalog_value: Any) -> None:
    mode = _keys(catalog_value, {"present", "section_count", "sections", "instruction_tokens",
                         "inventory_semantics", "group_estimates_are_additive"})
    if not isinstance(mode["present"], bool) or mode["group_estimates_are_additive"] is not False:
        raise ValueError("audit Code Mode flags must be booleans")
    _literal(mode["inventory_semantics"], {"observed_inline_snapshots_not_permission_decisions"})
    _count(mode["section_count"])
    _count(mode["instruction_tokens"])
    if not isinstance(mode["sections"], list):
        raise TypeError("audit Code Mode sections must be an array")
    for value in mode["sections"]:
        expected = {"message_index", "instruction_tokens", "catalog_complete", "namespaces",
                    "inventory_count", "inline_count", "lazy_not_inline_count"}
        if isinstance(value, Mapping) and "preamble_tokens" in value:
            expected.add("preamble_tokens")
        section = _keys(value, expected)
        if not isinstance(section["catalog_complete"], bool):
            raise TypeError("audit catalog completeness must be a boolean")
        for key in expected - {"catalog_complete", "namespaces"}:
            _count(section[key])
        if not isinstance(section["namespaces"], list):
            raise TypeError("audit namespaces must be an array")
        for value in section["namespaces"]:
            group = _keys(value, {"name", "inventory_count", "inline_count", "lazy_not_inline_count",
                                  "inline_paths", "unparsed_inline_count", "instruction_tokens"})
            _safe_identifier(group["name"])
            for key in ("inventory_count", "inline_count", "lazy_not_inline_count", "unparsed_inline_count", "instruction_tokens"):
                _count(group[key])
            if not isinstance(group["inline_paths"], list):
                raise TypeError("audit Code Mode paths must be an array")
            for path in group["inline_paths"]:
                if isinstance(path, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", path):
                    continue
                parsed = _path("  - " + path + "()") if isinstance(path, str) else None
                if parsed is None or parsed[0] != path:
                    raise ValueError("audit Code Mode path must be a callable identifier")
                for segment in parsed[1]:
                    _safe_identifier(segment)
