from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from personal_ai_core.ui.server import MAX_REQUEST_BYTES, UiApplication, build_server


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


def _protected_headers(server, *, content_type: str, length: int) -> dict[str, str]:
    host, port = server.server_address
    return {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "X-CSRF-Token": server.csrf_token,
        "Content-Type": content_type,
        "Content-Length": str(length),
    }


def test_server_binds_loopback_only(tmp_path) -> None:
    server = build_server(UiApplication(tmp_path))
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


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
        csp = response.getheader("Content-Security-Policy")
        assert csp is not None
        assert "frame-ancestors 'none'" in csp
        assert str(tmp_path) not in page
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_unexpected_host_is_rejected(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/api/health", headers={"Host": "localhost:9999"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert payload == {"error": "invalid_host"}
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_state_change_requires_origin_and_csrf(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        content_type, body = _multipart("notes.md", b"content")
        base = _protected_headers(server, content_type=content_type, length=len(body))

        cases = [
            {k: v for k, v in base.items() if k != "Origin"},
            {**base, "Origin": "http://evil.example"},
            {k: v for k, v in base.items() if k != "X-CSRF-Token"},
            {**base, "X-CSRF-Token": "wrong"},
        ]
        for headers in cases:
            connection = http.client.HTTPConnection(host, port)
            connection.request("POST", "/api/documents", body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.close()
        assert server.application.documents.list() == ()
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_upload_requires_exact_origin_and_csrf_then_indexes_text(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, _ = server.server_address
        content_type, body = _multipart("notes.md", "budget: 18000".encode())
        connection = http.client.HTTPConnection(host, server.server_address[1])
        headers = _protected_headers(server, content_type=content_type, length=len(body))
        connection.request("POST", "/api/documents", body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 201
        assert payload["document"]["ingestion_status"] == "indexed"
        assert payload["document"]["chunk_count"] == 1
        assert "path" not in payload["document"]
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_path_like_filename_is_rejected(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        content_type, body = _multipart("../notes.md", b"content")
        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "POST",
            "/api/documents",
            body=body,
            headers=_protected_headers(server, content_type=content_type, length=len(body)),
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert payload["error"] == "document_invalid"
        assert server.application.documents.list() == ()
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_oversized_content_length_is_rejected_before_body_read(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port)
        headers = _protected_headers(
            server,
            content_type="multipart/form-data; boundary=x",
            length=MAX_REQUEST_BYTES + 1,
        )
        connection.request("POST", "/api/documents", body=b"", headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 413
        assert payload == {"error": "upload_too_large"}
        assert server.application.documents.list() == ()
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_document_delete_is_deferred(tmp_path) -> None:
    server, thread = _start(tmp_path)
    try:
        host, port = server.server_address
        headers = _protected_headers(server, content_type="application/json", length=0)
        connection = http.client.HTTPConnection(host, port)
        connection.request("DELETE", "/api/documents/abc", headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 405
        assert payload == {"error": "document_deletion_deferred"}
    finally:
        server.shutdown()
        thread.join(timeout=2)
