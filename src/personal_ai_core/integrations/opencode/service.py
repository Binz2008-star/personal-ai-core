"""PAC's reasoning boundary for an OpenCode-owned native tool loop.

This service never executes a tool and never reconstructs caller history from
PAC's ordinary conversation store. The supplied structured transcript is the
authority; PAC adds its identity and measures everything before generation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import time
from typing import Any, Callable, Mapping, Protocol, Sequence, cast
import uuid

from ...core.contracts import IdentityComposer, ModelProvider, ModelSpecLike, SecretRedactor, ToolCallingProvider
from ...core.domain import ModelResponse, ToolDeclaration
from ...core.errors import ProviderError
from ...core.redaction import RedactionError
from .budget import OpenCodeContextBudget
from .protocol import OpenCodeChatRequest, OpenCodeMessage
from .store import SUPPORTED_REQUEST_KINDS


class IntegrationError(Exception):
    """A safe public error: no provider exception text or caller content."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StructuredModelProvider(Protocol):
    """The existing provider's additional structured-message input capability."""

    @property
    def name(self) -> str: ...

    def generate(self, *, model: str, messages: Sequence[Mapping[str, Any]],
                 options: Mapping[str, Any] | None = None) -> ModelResponse: ...

    def generate_with_tools(self, *, model: str, messages: Sequence[Mapping[str, Any]],
                            tools: Sequence[ToolDeclaration],
                            options: Mapping[str, Any] | None = None) -> ModelResponse: ...


class SessionMetadataStore(Protocol):
    def touch(self, external_id: str | None, *, model: str, profile_fingerprint: str,
              request_kind: str = "primary", metadata: Mapping[str, Any] | None = None) -> str: ...

    def record(self, session_id: str, metadata: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    body: dict[str, Any]
    session_id: str
    budget: dict[str, Any]


class OpenCodeProviderService:
    """Identity + full native-transcript budgeting + the registered Boss model."""

    def __init__(self, *, provider: StructuredModelProvider, model: ModelSpecLike,
                 identity: IdentityComposer, budget: OpenCodeContextBudget,
                 redactor: SecretRedactor, sampling: Mapping[str, Any],
                 store: SessionMetadataStore | None = None, output_limit: int = 1024,
                 confirm_context: Callable[[], None] | None = None,
                 audit: Callable[[Mapping[str, Any]], None] | None = None,
                 audit_required: bool = False,
                 audit_warning: Callable[[str], None] | None = None) -> None:
        if not isinstance(cast(object, provider), ModelProvider):
            raise ValueError("a ModelProvider is required")
        if audit_required and audit is None:
            raise ValueError("audit_required needs an audit callback")
        self._provider, self._model = provider, model
        self._identity, self._budget, self._redactor = identity, budget, redactor
        self._sampling, self._store = dict(sampling), store
        self._confirm_context = confirm_context
        self._audit, self._audit_required, self._audit_warning = audit, audit_required, audit_warning
        self.context_window, self.output_limit = model.context_window, output_limit
        self._identity_text = identity.compose(session_id="opencode-identity").content
        self.profile_fingerprint = hashlib.sha256(self._identity_text.encode()).hexdigest()

    def complete(self, request: OpenCodeChatRequest, *, conversation_id: str | None = None,
                 request_kind: str = "primary",
                 selection_diagnostics: Mapping[str, Any] | None = None) -> ProviderCompletion:
        """Audit admission before generation and completion afterward, without content."""
        from .audit import describe_request

        correlation_id = uuid.uuid4().hex
        state: dict[str, Any] = {"audit_attempted": False, "admitted": False,
                                 "completion_attempted": False, "provider_response": False}
        profile = selection_diagnostics.get("profile") if selection_diagnostics else None
        policy = ("opencode-native-http-" + profile + "-v1" if isinstance(profile, str) and profile in
                  {"repo", "github", "context7", "memory"} else "opencode-native-pass-through-v1")
        envelope = {"request_correlation_id": correlation_id, "selection_policy_version": policy,
                    "request_kind": request_kind if request_kind in SUPPORTED_REQUEST_KINDS else "primary"}

        def warn_or_refuse() -> None:
            message = "PAC integration audit could not be written; no request content was logged."
            try:
                if self._audit_warning is not None:
                    self._audit_warning(message)
                else:
                    import sys
                    print(message, file=sys.stderr)
            except Exception:
                pass
            if self._audit_required:
                raise IntegrationError("audit_unavailable", "The required integration audit could not be written.", 503) from None

        def emit(cost: Any, outcome: str) -> None:
            state["audit_attempted"] = True
            if self._audit is not None:
                try:
                    try:
                        record = describe_request(request, cost,
                            selection_diagnostics=selection_diagnostics, redactor=self._redactor)
                    except (ValueError, TypeError):
                        record = describe_request(request, cost, redactor=self._redactor)
                        record["warnings"].append("invalid_upstream_selection_manifest")
                    record.update(**envelope, event_type="request_admission", outcome=outcome)
                    self._audit(json.loads(json.dumps(record, allow_nan=False)))
                except Exception:
                    warn_or_refuse()
            state["admitted"] = outcome == "admitted"

        def completion(outcome: str, result: ProviderCompletion | None = None) -> None:
            if state["completion_attempted"]:
                return
            state["completion_attempted"] = True
            if self._audit is None:
                return
            record: dict[str, Any] = {**envelope, "event_type": "request_completion", "outcome": outcome}
            if result is not None:
                choice = result.body["choices"][0]
                record.update(tool_call_count=len(choice["message"].get("tool_calls", [])),
                              finish_reason=choice["finish_reason"])
                for key in ("prompt_tokens", "completion_tokens"):
                    value = result.body.get("usage", {}).get(key)
                    if type(value) is int and 0 <= value <= 2**63 - 1:
                        record[key] = value
            try:
                self._audit(json.loads(json.dumps(record, allow_nan=False)))
            except Exception:
                warn_or_refuse()

        state["emit"] = emit
        outcome = "refused_protocol"
        try:
            result = self._complete(request, conversation_id=conversation_id,
                                    request_kind=request_kind, audit_state=state)
            choice = result.body["choices"][0]
            completion("generated_tool_calls" if choice["message"].get("tool_calls") else "generated_text", result)
            return result
        except IntegrationError as error:
            if state["admitted"]:
                completion("provider_error" if error.code == "provider_failure" else "protocol_error")
            elif error.code == "tools_not_supported" or error.code == "required_tool_unavailable":
                outcome = "refused_required_tool_unavailable"
            raise
        except Exception:
            if state["admitted"]:
                completion("protocol_error" if state["provider_response"] else "provider_error")
            raise
        finally:
            if not state["audit_attempted"]:
                # Early validation refusals still get numeric estimates.
                cost = state.get("cost") or self._budget.measure(model=self._model,
                    messages=request.messages, tools=request.tools)
                emit(cost, outcome)

    def _complete(self, request: OpenCodeChatRequest, *, conversation_id: str | None,
                  request_kind: str, audit_state: dict[str, Any]) -> ProviderCompletion:
        if request.model != "pac-local":
            raise IntegrationError("model_not_found", "Choose the pac-local model.", 404)
        if isinstance(request.tool_choice, Mapping) and request.tool_choice["function"]["name"] not in {tool.name for tool in request.tools}:
            raise IntegrationError("required_tool_unavailable", "The required named tool needs its complete schema selected for this request.")
        options = self._options(request)
        messages, redactions = self._redact_transcript(request.messages)
        try:
            for tool in request.tools:
                _, count = self._redact_json(tool.to_dict())
                if count:
                    raise IntegrationError("secret_tool_schema", "A tool schema contains secret-shaped data; no tool definitions were changed or sent.")
        except RedactionError as error:
            raise IntegrationError("secret_tool_schema", "A tool schema could not be safely checked; nothing was sent.") from error
        choice_instruction = self._choice_instruction(request.tool_choice)
        measured = ([OpenCodeMessage(role="system", content=choice_instruction)]
                    if choice_instruction else []) + list(messages)
        cost = self._budget.measure(model=self._model, messages=measured, tools=request.tools)
        audit_state["cost"] = cost
        allocation = cost.allocation
        if allocation.overcommitted:
            audit_state["emit"](cost, "refused_context_budget")
            if self._store:
                refused_id = self._store.touch(conversation_id, model=self._model.name,
                    profile_fingerprint=self.profile_fingerprint, request_kind=request_kind)
                self._store.record(refused_id, {
                    **self._budget_metadata(cost.to_dict()), "status": "context_overflow",
                    "tool_results": sum(m.role == "tool" for m in messages),
                    "redactions": redactions,
                })
            raise IntegrationError(
                "context_length_exceeded",
                f"PAC needs about {allocation.spoken_for} tokens for identity, messages, "
                f"tool schemas and reply reserve; the configured window is {self.context_window}. "
                "Use an OpenCode tool selection that fits this window, or compact the session. "
                "No messages or tools were truncated; the model was not called.",
            )
        if request.tools and not isinstance(cast(object, self._provider), ToolCallingProvider):
            raise IntegrationError("tools_not_supported", "The configured PAC provider cannot call tools.")
        for tool in request.tools:
            if tool.strict:
                raise IntegrationError("unsupported_tool_constraint", "strict:true tool schemas are not supported by this provider.")
        session_id = (self._store.touch(conversation_id, model=self._model.name,
                                       profile_fingerprint=self.profile_fingerprint,
                                       request_kind=request_kind)
                      if self._store else str(uuid.uuid4()))
        wire = [{"role": "system", "content": self._identity_text},
                *self._ollama_messages(measured)]
        options["num_ctx"] = self.context_window
        metadata = self._budget_metadata(cost.to_dict())
        metadata.update(tool_results=sum(m.role == "tool" for m in messages), redactions=redactions)
        audit_state["emit"](cost, "admitted")
        try:
            response = (
                self._provider.generate_with_tools(model=self._model.name, messages=wire,
                    tools=[tool.declaration() for tool in request.tools], options=options)
                if request.tools else
                self._provider.generate(model=self._model.name, messages=wire, options=options)
            )
        except ProviderError as error:
            if self._store:
                self._store.record(session_id, {**metadata, "status": "provider_failure"})
            classification = error.kind + (f", HTTP {error.status}" if error.status is not None else "")
            raise IntegrationError("provider_failure", f"The configured PAC model could not complete the request ({classification}).", 502) from error
        audit_state["provider_response"] = True
        if self._confirm_context is not None:
            self._confirm_context()
        native_message = response.raw.get("message")
        if not request.tools and isinstance(native_message, Mapping) and native_message.get("tool_calls"):
            raise IntegrationError("undeclared_tool_call", "The model returned a tool call without declared tools; the response was withheld.", 502)
        calls = self._response_calls(response, request)
        try:
            shown = self._redactor.redact(response.text)
        except RedactionError as error:
            raise IntegrationError("redaction_failed", "The reply could not be checked for secrets and was withheld.", 502) from error
        message: dict[str, Any] = {"role": "assistant", "content": shown.text or (None if calls else "")}
        if calls:
            message["tool_calls"] = calls
        finish = "tool_calls" if calls else ("length" if response.finish_reason == "length" else "stop")
        body: dict[str, Any] = {
            "id": "chatcmpl-pac-" + uuid.uuid4().hex,
            "object": "chat.completion", "created": int(time.time()), "model": "pac-local",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        if response.prompt_tokens is not None and response.completion_tokens is not None:
            body["usage"] = {"prompt_tokens": response.prompt_tokens,
                             "completion_tokens": response.completion_tokens,
                             "total_tokens": response.prompt_tokens + response.completion_tokens}
            metadata.update(prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens)
        metadata.update(status="completed", tool_calls=len(calls), redactions=redactions + shown.total)
        if self._store:
            self._store.record(session_id, metadata)
        return ProviderCompletion(body=body, session_id=session_id, budget=cost.to_dict())

    @staticmethod
    def _budget_metadata(cost: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in cost.items()
                if key in {"context_window", "identity_tokens", "transcript_tokens", "tool_schema_tokens",
                           "generation_reserve", "overhead", "guard_reserve", "spoken_for"}}

    def _options(self, request: OpenCodeChatRequest) -> dict[str, Any]:
        options = dict(self._sampling)
        supplied = request.options
        token_limits = [supplied[key] for key in ("max_tokens", "max_completion_tokens") if key in supplied]
        if len(token_limits) == 2 and token_limits[0] != token_limits[1]:
            raise IntegrationError("conflicting_output_limits", "max_tokens and max_completion_tokens disagree.")
        limit = token_limits[0] if token_limits else self.output_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.output_limit:
            raise IntegrationError("invalid_output_limit", f"PAC's output limit is {self.output_limit} tokens; request a value from 1 to {self.output_limit}.")
        options["num_predict"] = limit
        for key, bounds in {"temperature": (0, 2), "top_p": (0, 1),
                            "frequency_penalty": (-2, 2), "presence_penalty": (-2, 2)}.items():
            if key in supplied:
                value = supplied[key]
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
                    raise IntegrationError("invalid_sampling_option", f"{key} is outside the supported range.")
                options[key] = value
        if "seed" in supplied:
            if isinstance(supplied["seed"], bool) or not isinstance(supplied["seed"], int):
                raise IntegrationError("invalid_seed", "seed must be an integer.")
            options["seed"] = supplied["seed"]
        if "stop" in supplied:
            value = supplied["stop"]
            stops = [value] if isinstance(value, str) else value
            if not isinstance(stops, list) or not all(isinstance(stop, str) for stop in stops):
                raise IntegrationError("invalid_stop", "stop must be text or a list of text strings.")
            options["stop"] = stops
        return options

    def _redact_transcript(self, messages: Sequence[OpenCodeMessage]) -> tuple[tuple[OpenCodeMessage, ...], int]:
        checked = []
        total = 0
        for message in messages:
            try:
                content, count = self._redact_content(message.content)
                total += count
                calls = []
                for call in message.tool_calls:
                    arguments, count = self._redact_json(json.loads(call.arguments))
                    total += count
                    calls.append(replace(call, arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))))
            except RedactionError as error:
                raise IntegrationError("redaction_failed", "A message could not be checked for secrets; nothing was sent to the model.") from error
            checked.append(replace(message, content=content, tool_calls=tuple(calls)))
        return tuple(checked), total

    def _redact_content(self, content: str | None) -> tuple[str | None, int]:
        if content is None:
            return None, 0
        # JSON tool results may escape a key's letters, defeating raw-text
        # matching. Redact decoded leaf strings too; leave clean output exact.
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            checked, count = self._redact_json(decoded)
            if count:
                return json.dumps(checked, ensure_ascii=False, separators=(",", ":")), count
        shown = self._redactor.redact(content)
        return shown.text, shown.total

    def _redact_json(self, value: Any) -> tuple[Any, int]:
        if isinstance(value, str):
            shown = self._redactor.redact(value)
            return shown.text, shown.total
        if isinstance(value, list):
            items = [self._redact_json(item) for item in value]
            return [item for item, _ in items], sum(count for _, count in items)
        if isinstance(value, dict):
            for key in value:
                if self._redactor.redact(key).total:
                    # A replacement key could collide with another field or
                    # change a tool's argument structure. Fail closed instead.
                    raise RedactionError(RedactionError.INVALID_INPUT)
            items = {key: self._redact_json(item) for key, item in value.items()}
            return {key: item for key, (item, _) in items.items()}, sum(count for _, count in items.values())
        return value, 0

    @staticmethod
    def _choice_instruction(choice: Any) -> str:
        if choice == "none":
            return "For this generation, do not call a tool. Answer in assistant text only."
        if choice == "required":
            return "For this generation, call at least one of the declared tools. Do not substitute an assistant answer for a required tool call."
        if isinstance(choice, Mapping):
            return "For this generation, call the declared function " + json.dumps(choice["function"]["name"]) + ". Do not call a different function or substitute an assistant answer."
        return ""

    @staticmethod
    def _ollama_messages(messages: Sequence[OpenCodeMessage]) -> list[Mapping[str, Any]]:
        names: dict[str, str] = {}
        result = []
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content or ""}
            if message.tool_calls:
                item["tool_calls"] = []
                for call in message.tool_calls:
                    names[call.id] = call.name
                    item["tool_calls"].append({"id": call.id, "type": "function",
                        "function": {"name": call.name, "arguments": json.loads(call.arguments)}})
            if message.role == "tool":
                call_id = message.tool_call_id
                if call_id is None or call_id not in names:
                    raise IntegrationError("invalid_tool_history", "A tool result has no matching assistant call.")
                item["tool_call_id"] = call_id
                item["tool_name"] = names[call_id]
            result.append(item)
        return result

    def _response_calls(self, response: ModelResponse, request: OpenCodeChatRequest) -> list[dict[str, Any]]:
        declared = {tool.name for tool in request.tools}
        calls = []
        for call in response.tool_calls:
            if not isinstance(call.name, str) or call.name not in declared or not isinstance(call.arguments, dict):
                raise IntegrationError("invalid_tool_call", "The model returned an invalid or undeclared tool call; nothing was executed.", 502)
            try:
                _, count = self._redact_json(call.arguments)
                if count:
                    # Changing call arguments can change what a tool does.
                    # Withhold this call instead of executing altered inputs.
                    raise IntegrationError("secret_tool_arguments", "The model returned secret-shaped tool arguments; the call was withheld.", 502)
                arguments = json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError, RedactionError) as error:
                raise IntegrationError("invalid_tool_arguments", "The model returned invalid tool arguments; nothing was executed.", 502) from error
            calls.append({"id": "call_pac_" + uuid.uuid4().hex, "type": "function",
                          "function": {"name": call.name, "arguments": arguments}})
        choice = request.tool_choice
        if choice == "none" and calls:
            raise IntegrationError("tool_choice_violation", "The model called a tool when tool_choice was none.", 502)
        if (choice == "required" or isinstance(choice, Mapping)) and not calls:
            raise IntegrationError("tool_choice_violation", "The model did not return the required tool call.", 502)
        if isinstance(choice, Mapping) and any(call["function"]["name"] != choice["function"]["name"] for call in calls):
            raise IntegrationError("tool_choice_violation", "The model returned a different tool than requested.", 502)
        if request.options.get("parallel_tool_calls") is False and len(calls) > 1:
            raise IntegrationError("parallel_tool_calls_violation", "The model returned multiple calls when parallel calls were disabled.", 502)
        return calls
