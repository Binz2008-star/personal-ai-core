"""Localhost-only OpenAI chat-completions adapter for OpenCode.

OpenCode is the tool executor. The HTTP adapter only validates a request,
hands its structured transcript to the provider service, and returns the
completion. Requests and tool contents are never written to HTTP logs.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

from .protocol import OpenCodeChatRequest, ProtocolError
from .service import IntegrationError
from .store import SUPPORTED_REQUEST_KINDS, IntegrationStoreError

if TYPE_CHECKING:
    from .service import OpenCodeProviderService

MAX_REQUEST_BYTES = 8 * 1024 * 1024
_CONNECTION_TIMEOUT = 30
_STREAM_FRAGMENT_SIZE = 256


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"message": message, "type": "invalid_request_error",
                      "param": None, "code": code}}


def _sse_events(body: Mapping[str, Any], *, include_usage: bool) -> tuple[bytes, ...]:
    """Represent one completed generation as ordinary OpenAI SSE deltas.

    Serializing every event before headers lets validation/provider failures
    retain normal JSON error responses. This is protocol compatibility, not
    token streaming from the Boss model.
    """
    base = {"id": body["id"], "object": "chat.completion.chunk",
            "created": body["created"], "model": body["model"]}
    choice = body["choices"][0]
    message = choice["message"]
    finish_reason = choice["finish_reason"]
    if finish_reason not in {"tool_calls", "stop", "length"}:
        raise ValueError("Unsupported completion finish reason")
    chunks: list[dict[str, Any]] = []

    def append(delta: dict[str, Any], finish: str | None = None) -> None:
        chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if include_usage:
            chunk["usage"] = None
        chunks.append(chunk)

    append({"role": "assistant", "content": ""})
    content = message.get("content")
    if content:
        for position in range(0, len(content), _STREAM_FRAGMENT_SIZE):
            append({"content": content[position:position + _STREAM_FRAGMENT_SIZE]})
    for index, call in enumerate(message.get("tool_calls", [])):
        function = call["function"]
        append({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                               "function": {"name": function["name"], "arguments": ""}}]})
        arguments = function["arguments"]
        for position in range(0, len(arguments), _STREAM_FRAGMENT_SIZE):
            append({"tool_calls": [{"index": index, "function": {
                "arguments": arguments[position:position + _STREAM_FRAGMENT_SIZE],
            }}]})
    append({}, finish_reason)
    if include_usage:
        chunks.append({**base, "choices": [], "usage": body.get("usage")})
    return tuple(
        b"data: " + json.dumps(chunk, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n\n"
        for chunk in chunks
    ) + (b"data: [DONE]\n\n",)


def create_server(service: OpenCodeProviderService, *, host: str = "127.0.0.1",
                  port: int = 8765, log: Callable[[str], None] | None = None) -> HTTPServer:
    """Create a single-threaded server; even explicit hosts must be loopback."""
    if host != "127.0.0.1":
        raise ValueError("PAC OpenCode provider must bind to 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be a whole number between 0 and 65535")

    class Handler(BaseHTTPRequestHandler):
        server_version = "PAC-OpenCode/1"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(_CONNECTION_TIMEOUT)

        def log_message(self, format: str, *args: object) -> None:
            # BaseHTTPRequestHandler includes the raw request target here,
            # which may contain keys in a query string. Only _send logs.
            return

        def _log_status(self, status: int) -> None:
            if log is not None:
                route = self.path.split("?", 1)[0]
                if route not in {"/v1/models", "/v1/chat/completions"}:
                    route = "/unknown"
                log(f"{route} {status}")

        def _send(self, status: int, body: dict[str, object],
                  *, session_id: str | None = None) -> None:
            serialized = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(serialized)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if session_id is not None:
                self.send_header("X-PAC-Session-ID", session_id)
            self.end_headers()
            self.wfile.write(serialized)
            self.close_connection = True
            self._log_status(status)

        def _send_sse(self, body: dict[str, Any], *, session_id: str,
                      include_usage: bool) -> None:
            events = _sse_events(body, include_usage=include_usage)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-PAC-Session-ID", session_id)
            self.end_headers()
            self.close_connection = True
            try:
                for event in events:
                    self.wfile.write(event)
                    self.wfile.flush()
            except OSError:
                # The model call already completed once. A disconnected client
                # must not trigger another generation or a second response.
                return
            self._log_status(200)

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/models":
                self._send(404, _error("not_found", "Unknown endpoint"))
                return
            self._send(200, {
                "object": "list",
                "data": [{"id": "pac-local", "object": "model", "created": 0,
                          "owned_by": "personal-ai-core", "name": "PAC (local)",
                          "context_window": service.context_window,
                          "output_limit": service.output_limit}],
            })

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/chat/completions":
                self._discard_small_body()
                self._send(404, _error("not_found", "Unknown endpoint"))
                return
            if self.headers.get_content_type() != "application/json":
                self._discard_small_body()
                self._send(415, _error("unsupported_media_type", "Use application/json"))
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
                self._send(411, _error("content_length_required", "One Content-Length header is required"))
                return
            try:
                length = int(lengths[0])
            except ValueError:
                self._send(400, _error("invalid_content_length", "Invalid Content-Length"))
                return
            if length < 0:
                self._send(400, _error("invalid_content_length", "Invalid Content-Length"))
                return
            if length > MAX_REQUEST_BYTES:
                self._send(413, _error("request_too_large", "Request exceeds the 8 MiB limit"))
                return
            try:
                payload = self.rfile.read(length)
                if len(payload) != length:
                    self._send(400, _error("incomplete_request", "Incomplete request body"))
                    return
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._send(400, _error("invalid_json", "Request must contain valid UTF-8 JSON"))
                return
            except (OSError, TimeoutError):
                self._send(408, _error("request_timeout", "Request body was not received in time"))
                return
            try:
                request = OpenCodeChatRequest.from_dict(decoded)
                conversation_id = self.headers.get("X-OpenCode-Session-ID")
                if conversation_id is None:
                    conversation_id = self.headers.get("X-PAC-Session-ID")
                if conversation_id is not None:
                    conversation_id = conversation_id.strip()
                    if (not conversation_id or len(conversation_id) > 256
                            or any(ord(character) < 32 for character in conversation_id)):
                        self._send(400, _error("invalid_session_id", "Invalid session ID"))
                        return
                request_kind = self.headers.get("X-PAC-Request-Kind", "primary").strip()
                if request_kind not in SUPPORTED_REQUEST_KINDS:
                    self._send(400, _error("invalid_request_kind", "Unsupported PAC request kind"))
                    return
                audit_selection = self.headers.get("X-PAC-Tool-Selection")
                keywords: dict[str, Any] = {"conversation_id": conversation_id,
                                            "request_kind": request_kind}
                if audit_selection is not None:
                    if len(audit_selection) > 16_384:
                        self._send(400, _error("invalid_selection_metadata", "Tool-selection metadata is too large"))
                        return
                    try:
                        selection = json.loads(audit_selection)
                    except (ValueError, RecursionError):
                        self._send(400, _error("invalid_selection_metadata", "Tool-selection metadata must be a JSON object"))
                        return
                    if not isinstance(selection, dict):
                        self._send(400, _error("invalid_selection_metadata", "Tool-selection metadata must be a JSON object"))
                        return
                    keywords["selection_diagnostics"] = selection
                completion = service.complete(request, **keywords)
                if request.stream:
                    stream_options = request.options.get("stream_options", {})
                    self._send_sse(completion.body, session_id=completion.session_id,
                                   include_usage=bool(stream_options.get("include_usage", False)))
                else:
                    self._send(200, completion.body, session_id=completion.session_id)
            except ProtocolError as error:
                self._send(400, _error("invalid_request", str(error)))
            except IntegrationError as error:
                self._send(error.status, _error(error.code, error.message))
            except IntegrationStoreError:
                self._send(400, _error("invalid_session", "PAC could not validate the integration session"))
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:  # noqa: BLE001 - the HTTP boundary must hide unexpected exception contents.
                self._send(500, _error("internal_error", "PAC provider could not complete the request"))

        def _discard_small_body(self) -> None:
            # Closing a socket with an unread ordinary request body can reset
            # the connection on Windows before the client sees its HTTP error.
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
                return
            try:
                length = int(lengths[0])
                if 0 < length <= MAX_REQUEST_BYTES:
                    self.rfile.read(length)
            except (ValueError, OSError):
                return

    return HTTPServer((host, port), Handler)


def serve(service: OpenCodeProviderService, *, port: int = 8765,
          log: Callable[[str], None] | None = None) -> None:
    with create_server(service, port=port, log=log) as server:
        server.serve_forever()
