from __future__ import annotations

import http.client
import json
import threading

import pytest

import personal_ai_core.ui.documents as documents_module
from personal_ai_core.ui.documents import PdfExtractorUnavailable
from personal_ai_core.ui.server import UiApplication, build_server


class Client:
    def __init__(self, server, app, data_dir):
        self.server, self.app, self.data_dir = server, app, data_dir
        self.host, self.port = server.server_address
        self.token = server.csrf_token
        self.origin = f"http://127.0.0.1:{self.port}"

    def request(self, method, path, body=None, *, host=None, origin=True,
                csrf=True, content_type=None):
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin is True:
            headers["Origin"] = self.origin
        elif origin:
            headers["Origin"] = origin
        if csrf is True:
            headers["X-CSRF-Token"] = self.token
        elif csrf:
            headers["X-CSRF-Token"] = csrf
        if content_type:
            headers["Content-Type"] = content_type
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def upload(self, filename, content, **kwargs):
        boundary = "----pac-boundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", "/api/documents", body,
                            content_type=f"multipart/form-data; boundary={boundary}", **kwargs)


def body_of(raw):
    return json.loads(raw)


@pytest.fixture
def ui(tmp_path):
    app = UiApplication(tmp_path)
    server = build_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Client(server, app, tmp_path)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


PROTECTED = [
    ("POST", "/api/documents"),
    ("POST", "/api/search"),
    ("DELETE", "/api/documents/abc"),
    ("POST", "/api/documents/abc/retry"),
]
LEAK_MARKERS = ("Traceback", 'File "', ".py", "RuntimeError", "OSError")


def test_server_binds_to_loopback_only(ui):
    assert ui.server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["evil.example", "localhost:{port}", "127.0.0.1:1", "127.0.0.1"])
def test_unexpected_host_is_rejected_for_reads_and_writes(ui, host):
    host = host.format(port=ui.port)
    status, _, _ = ui.request("GET", "/api/health", host=host)
    assert status in (400, 403)
    for method, path in PROTECTED:
        status, _, _ = ui.request(method, path, b"{}", host=host)
        assert status == 403, (method, path, host)


@pytest.mark.parametrize("method,path", PROTECTED)
@pytest.mark.parametrize("variant", ["missing_origin", "foreign_origin", "missing_csrf", "wrong_csrf"])
def test_every_state_changing_route_needs_origin_and_csrf(ui, method, path, variant):
    kwargs = {
        "missing_origin": {"origin": False},
        "foreign_origin": {"origin": "http://evil.example"},
        "missing_csrf": {"csrf": False},
        "wrong_csrf": {"csrf": "not-the-token"},
    }[variant]
    status, _, raw = ui.request(method, path, b"{}", **kwargs)
    assert status == 403
    assert body_of(raw) == {"error": "request_not_allowed"}


def test_rejected_delete_does_not_delete(ui):
    status, _, raw = ui.upload("notes.md", b"budget")
    assert status == 201
    doc_id = body_of(raw)["document"]["id"]
    status, _, _ = ui.request("DELETE", f"/api/documents/{doc_id}", origin=False)
    assert status == 403
    _, _, raw = ui.request("GET", "/api/documents")
    assert [d["id"] for d in body_of(raw)["documents"]] == [doc_id]


def test_no_permissive_cors(ui):
    for method, path in [("GET", "/api/health"), ("OPTIONS", "/api/documents")]:
        _, headers, _ = ui.request(method, path, origin="http://evil.example")
        assert not {k.lower() for k in headers} & {
            "access-control-allow-origin", "access-control-allow-credentials"}


def test_page_carries_security_headers(ui):
    _, headers, _ = ui.request("GET", "/")
    lowered = {k.lower(): v for k, v in headers.items()}
    assert "frame-ancestors 'none'" in lowered["content-security-policy"]
    assert lowered["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["../x.md", "a/b.md", "..\\x.md", "a\\b.txt", ".."])
def test_path_like_filenames_are_rejected_not_sanitised(ui, name):
    status, _, raw = ui.upload(name, b"content")
    assert status == 400
    assert body_of(raw)["error"] == "document_invalid"
    assert ui.app.documents.list() == ()
    assert not [p for p in ui.data_dir.iterdir() if p.suffix in {".md", ".txt", ".pdf"}]


def test_oversized_content_length_is_rejected_before_the_body_is_read(ui):
    conn = http.client.HTTPConnection(ui.host, ui.port, timeout=5)
    conn.putrequest("POST", "/api/documents", skip_host=True)
    for key, value in {
        "Host": f"127.0.0.1:{ui.port}", "Origin": ui.origin, "X-CSRF-Token": ui.token,
        "Content-Type": "multipart/form-data; boundary=x",
        "Content-Length": str(500 * 1024 * 1024),
    }.items():
        conn.putheader(key, value)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_malformed_and_non_multipart_uploads_are_safe_errors(ui):
    status, _, raw = ui.request("POST", "/api/documents", b"garbage",
                                content_type="multipart/form-data; boundary=zzz")
    assert status == 400
    status, _, raw2 = ui.request("POST", "/api/documents", b"{}", content_type="application/json")
    assert status == 400 and body_of(raw2)["error"] == "multipart_required"


def test_error_responses_never_leak_paths_or_stack_traces(ui):
    responses = [
        ui.upload("../x.md", b"x"),
        ui.upload("notes.exe", b"x"),
        ui.upload("bad.txt", b"\xff\xfe\x00bad"),
        ui.upload("fake.pdf", b"not a pdf"),
        ui.request("DELETE", "/api/documents/..%2F..%2Fetc%2Fpasswd"),
        ui.request("POST", "/api/search", b"not json", content_type="application/json"),
        ui.request("GET", "/api/nope"),
        ui.request("POST", "/api/documents", b"garbage",
                   content_type="multipart/form-data; boundary=zzz"),
    ]
    for _, _, raw in responses:
        text = raw.decode("utf-8", "replace")
        assert str(ui.data_dir) not in text, text
        assert not any(marker in text for marker in LEAK_MARKERS), text


def _race(ui, payloads):
    barrier = threading.Barrier(len(payloads))
    statuses = []

    def go(name, content):
        barrier.wait()
        statuses.append(ui.upload(name, content)[0])

    threads = [threading.Thread(target=go, args=p) for p in payloads]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return sorted(statuses)


def test_concurrent_http_uploads_cannot_both_cross_the_count_quota(ui):
    ui.app.documents.max_count = 1
    assert _race(ui, [("a.txt", b"one"), ("b.txt", b"two")]) == [201, 507]


def test_concurrent_http_uploads_cannot_both_cross_the_byte_quota(ui):
    ui.app.documents.max_total_bytes = 10
    assert _race(ui, [("a.txt", b"12345678"), ("b.txt", b"abcdefgh")]) == [201, 507]


def test_upload_whose_indexing_fails_is_kept_as_failed(ui, monkeypatch):
    def boom(_id):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(ui.app, "_ingest", boom)
    status, _, raw = ui.upload("notes.md", b"budget")
    assert status == 201
    doc = body_of(raw)["document"]
    assert doc["ingestion_status"] == "failed" and doc["failure_reason"] == "index_error"
    assert "secret internals" not in raw.decode()
    assert len(ui.app.documents.list()) == 1
    assert any(ui.data_dir.glob("*.md"))


def test_failed_document_can_be_retried(ui, monkeypatch):
    real = ui.app._ingest
    calls = {"n": 0}

    def flaky(document_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(document_id)
    monkeypatch.setattr(ui.app, "_ingest", flaky)
    doc_id = body_of(ui.upload("notes.md", b"budget")[2])["document"]["id"]
    status, _, raw = ui.request("POST", f"/api/documents/{doc_id}/retry", b"")
    assert status == 200
    assert body_of(raw)["document"]["ingestion_status"] == "indexed"


def test_startup_rebuild_failure_never_deletes_documents(tmp_path, monkeypatch):
    UiApplication(tmp_path).upload("notes.md", b"budget")

    def boom(self, _id):
        raise RuntimeError("cannot index")
    monkeypatch.setattr(UiApplication, "_ingest", boom)
    app = UiApplication(tmp_path)
    (doc,) = app.documents.list()
    assert doc.ingestion_status == "failed" and doc.path.exists()


def test_restart_without_pdf_support_keeps_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(documents_module, "_extract_pdf", lambda _p: "hello pdf")
    UiApplication(tmp_path).upload("report.pdf", b"%PDF-1.4 stand-in")

    def unavailable(_p):
        raise PdfExtractorUnavailable("PDF support is not installed.")
    monkeypatch.setattr(documents_module, "_extract_pdf", unavailable)
    app = UiApplication(tmp_path)
    (doc,) = app.documents.list()
    assert doc.ingestion_status == "failed" and doc.path.exists()
    assert doc.failure_reason == "pdf_unavailable"


def _pdf(pages):
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {n} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        objs.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET" if text else ""
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return out


@pytest.fixture
def pypdf_available():
    pytest.importorskip("pypdf")


def test_valid_text_pdf_is_extracted_and_indexed(ui, pypdf_available):
    status, _, raw = ui.upload("report.pdf", _pdf(["Budget approved"]))
    doc = body_of(raw)["document"]
    assert status == 201 and doc["ingestion_status"] == "indexed" and doc["text_chars"] > 0


def test_malformed_pdf_is_a_safe_error(ui, pypdf_available):
    status, _, raw = ui.upload("broken.pdf", b"%PDF-1.4\n" + b"garbage" * 20)
    assert status == 400 and body_of(raw)["error"] == "document_invalid"
    assert ui.app.documents.list() == ()


def test_pdf_over_the_page_limit_is_rejected(ui, pypdf_available, monkeypatch):
    monkeypatch.setattr(documents_module, "MAX_PDF_PAGES", 2)
    status, _, raw = ui.upload("long.pdf", _pdf(["a", "b", "c"]))
    assert status == 400 and body_of(raw)["error"] == "document_invalid"


def test_pdf_without_extractable_text_is_reported_not_hidden(ui, pypdf_available):
    status, _, raw = ui.upload("scan.pdf", _pdf([""]))
    doc = body_of(raw)["document"]
    assert status == 201 and doc["text_chars"] == 0
    assert doc["notice"] == "no_extractable_text"
