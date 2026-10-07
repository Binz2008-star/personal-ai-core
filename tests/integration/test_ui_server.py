from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from personal_ai_core.ui.server import UiApplication, build_server


def _multipart(filename: str, content: bytes) -> tuple[str, bytes]:
    boundary = "----pac-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def _start(tmp_path: Path):
    app = UiApplication(tmp_path)
    server = build_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_health_and_static_page_expose_readiness_without_file_paths(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/api/health", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["storage_writable"] is True

        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert "__PAC_CSRF_TOKEN__" not in page
        assert "csrf-token" in page
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_upload_requires_exact_origin_and_csrf_then_indexes_text(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        origin = f"http://127.0.0.1:{port}"
        token = server.csrf_token
        content_type, body = _multipart("notes.md", "budget: 18000".encode())
        connection = http.client.HTTPConnection(host, port)
        headers = {
            "Host": f"127.0.0.1:{port}",
            "Origin": origin,
            "X-CSRF-Token": token,
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        connection.request("POST", "/api/documents", body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 201
        assert payload["document"]["ingestion_status"] == "indexed"
        assert payload["document"]["chunk_count"] == 1

        content_type, body = _multipart("other.md", b"nope")
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        headers["Origin"] = "http://evil.example"
        connection.request("POST", "/api/documents", body=body, headers=headers)
        response = connection.getresponse()
        assert response.status == 403
    finally:
        server.shutdown()
        thread.join(timeout=2)
