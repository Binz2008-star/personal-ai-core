"""Small localhost-only HTTP API for the static local UI.

The server deliberately uses only the Python standard library. It exposes
application services, never Ollama or SQLite internals, and serves no uploaded
file bytes back to the browser.
"""
from __future__ import annotations

import json
import secrets
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.knowledge import Document, RetrievalQuery
from ..knowledge import (
    FixedSizeChunker,
    HashingEmbeddingProvider,
    InMemoryChunkCatalog,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
    IngestionService,
    HybridRetriever,
)
from .documents import (
    MAX_FILE_BYTES,
    DocumentError,
    DocumentStore,
    PdfExtractorUnavailable,
    QuotaExceeded,
)

MAX_REQUEST_BYTES = MAX_FILE_BYTES + 512 * 1024
STATIC_FILES = {"/", "/index.html", "/styles.css", "/app.js"}


class UiApplication:
    """Composition root for the UI's document and knowledge services."""

    def __init__(self, data_dir: Path | None = None) -> None:
        root = data_dir or Path.home() / ".personal-ai-core" / "ui-documents"
        self.documents = DocumentStore(root)
        embedder = HashingEmbeddingProvider()
        vector = InMemoryVectorIndex(model_id=embedder.model_id, dimensions=embedder.dimensions)
        lexical = InMemoryLexicalIndex()
        catalog = InMemoryChunkCatalog()
        self.ingestion = IngestionService(
            chunker=FixedSizeChunker(), embedder=embedder, vector_index=vector,
            lexical_index=lexical, catalog=catalog,
        )
        self.retriever = HybridRetriever(
            embedder=embedder, vector_index=vector, lexical_index=lexical, catalog=catalog,
        )
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        for record in self.documents.list():
            try:
                self._ingest(record.id)
            except PdfExtractorUnavailable:
                self.documents.mark_failed(record.id, reason="pdf_unavailable")
            except Exception:
                # Indexes are derived state. Never destroy durable user bytes
                # because reconstruction failed.
                self.documents.mark_failed(record.id, reason="index_error")

    def _ingest(self, document_id: str):
        record = self.documents.get(document_id)
        if record is None:
            raise DocumentError("Document not found.")
        text = self.documents.text(document_id)
        doc = Document(
            id=record.id,
            source_uri=f"ui://documents/{record.id}",
            title=record.display_name,
            metadata={"display_name": record.display_name},
        )
        report = self.ingestion.ingest(doc, text)
        return self.documents.mark_indexed(document_id, report.chunk_count)

    def upload(self, display_name: str, content: bytes):
        record = self.documents.save(display_name, content)
        try:
            return self._ingest(record.id)
        except PdfExtractorUnavailable:
            return self.documents.mark_failed(record.id, reason="pdf_unavailable")
        except Exception:
            return self.documents.mark_failed(record.id, reason="index_error")

    def retry(self, document_id: str):
        if self.documents.get(document_id) is None:
            raise DocumentError("Document not found.")
        try:
            return self._ingest(document_id)
        except PdfExtractorUnavailable:
            return self.documents.mark_failed(document_id, reason="pdf_unavailable")
        except Exception:
            return self.documents.mark_failed(document_id, reason="index_error")

    def retry(self, document_id: str):
        record = self.documents.get(document_id)
        if record is None:
            raise DocumentError("Document not found.")
        try:
            return self._ingest(document_id)
        except PdfExtractorUnavailable:
            return self.documents.mark_failed(document_id, failure_reason="pdf_unavailable")
        except Exception:
            return self.documents.mark_failed(document_id, failure_reason="index_error")

    def search(self, text: str, limit: int = 5) -> list[dict[str, object]]:
        results = self.retriever.retrieve(RetrievalQuery(text=text, limit=limit))
        return [
            {
                "document_id": result.provenance.document_id,
                "display_name": result.chunk.metadata.get("display_name")
                or self.documents.get(result.chunk.document_id).display_name
                if self.documents.get(result.chunk.document_id)
                else "document",
                "chunk_id": result.chunk.id,
                "start": result.chunk.start,
                "end": result.chunk.end,
                "text": result.chunk.text,
                "methods": [method.value for method in result.provenance.methods],
                "retrieval_status": "grounded",
            }
            for result in results
        ]


class UiHandler(BaseHTTPRequestHandler):
    server_version = "PersonalAICoreUI/0.1"

    @property
    def application(self) -> UiApplication:
        return self.server.application  # type: ignore[attr-defined]

    @property
    def csrf_token(self) -> str:
        return self.server.csrf_token  # type: ignore[attr-defined]

    @property
    def origin(self) -> str:
        host, port = self.server.server_address  # type: ignore[attr-defined]
        return f"http://{host}:{port}"

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_host"})
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._json(HTTPStatus.OK, self._health())
            return
        if path == "/api/documents":
            self._json(HTTPStatus.OK, {"documents": [d.public_dict() for d in self.application.documents.list()]})
            return
        if path in STATIC_FILES:
            self._static(path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_host() or not self._same_origin() or not self._csrf_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "request_not_allowed"})
            return
        path = urlsplit(self.path).path
        if path == "/api/documents":
            self._upload()
            return
        if path.startswith("/api/documents/") and path.endswith("/retry"):
            document_id = path[len("/api/documents/"):-len("/retry")].strip("/")
            self._retry(document_id)
            return
        if path == "/api/search":
            self._search()
            return
        if path.startswith("/api/documents/") and path.endswith("/retry"):
            document_id = unquote(path[len("/api/documents/"):-len("/retry")]).strip("/")
            self._retry(document_id)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._valid_host() or not self._same_origin() or not self._csrf_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "request_not_allowed"})
            return
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "document_deletion_deferred"})

    def _upload(self) -> None:
        length = self._content_length()
        if length is None or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "upload_too_large"})
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "multipart_required"})
            return
        body = self.rfile.read(length)
        try:
            filename, content = _multipart_file(content_type, body)
            record = self.application.upload(filename, content)
        except PdfExtractorUnavailable:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "pdf_support_unavailable"})
        except QuotaExceeded:
            self._json(HTTPStatus.INSUFFICIENT_STORAGE, {"error": "document_quota_exceeded"})
        except DocumentError as exc:
            safe_errors = {
                "The file is larger than the upload limit.",
                "Only .md, .txt and .pdf files are supported.",
                "The uploaded file is not a valid PDF container.",
                "The file is not valid UTF-8 text.",
                "The extracted document text is too large.",
                "The PDF could not be read.",
                "The document was saved but could not be indexed.",
            }
            message = str(exc) if str(exc) in safe_errors else "The document could not be processed."
            self._json(HTTPStatus.BAD_REQUEST, {"error": "document_invalid", "message": message})
            return
        self._json(HTTPStatus.CREATED, {"document": record.public_dict()})

    def _retry(self, document_id: str) -> None:
        try:
            record = self.application.retry(document_id)
        except DocumentError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "document_not_found"})
            return
        self._json(HTTPStatus.OK, {"document": record.public_dict()})

    def _retry(self, document_id: str) -> None:
        if not re_full_document_id(document_id):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            record = self.application.retry(document_id)
        except DocumentError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._json(HTTPStatus.OK, {"document": record.public_dict()})

    def _search(self) -> None:
        try:
            payload = self._json_body()
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise DocumentError("A search text is required.")
            results = self.application.search(text, min(int(payload.get("limit", 5)), 20))
        except (DocumentError, ValueError, TypeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search"})
            return
        self._json(HTTPStatus.OK, {"state": "grounded" if results else "no_match", "passages": results})

    def _health(self) -> dict[str, object]:
        root = self.application.documents.root
        probe = root / f".health-{secrets.token_hex(8)}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
            writable = True
        except OSError:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            writable = False
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]  # noqa: F401
            pdf_available = True
        except ImportError:
            pdf_available = False
        return {
            "status": "ready" if writable else "degraded",
            "storage_writable": writable,
            "pdf_available": pdf_available,
        }

    def _static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        root = Path(__file__).with_name("static").resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        content = target.read_bytes()
        if relative == "index.html":
            content = content.replace(b"__PAC_CSRF_TOKEN__", self.csrf_token.encode("ascii"))
        content_types = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types[relative])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _valid_host(self) -> bool:
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_address[1]}"  # type: ignore[attr-defined]

    def _same_origin(self) -> bool:
        return self.headers.get("Origin", "") == self.origin

    def _csrf_valid(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.csrf_token)

    def _content_length(self) -> int | None:
        try:
            value = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return value if value >= 0 else None

    def _json_body(self) -> dict[str, Any]:
        length = self._content_length()
        if length is None or length > 256 * 1024:
            raise ValueError("invalid body")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("object required")
        return data

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _multipart_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise DocumentError("A multipart upload is required.")
    for part in message.iter_parts():
        if part.get_content_disposition() == "form-data" and part.get_filename():
            payload = part.get_payload(decode=True)
            if payload is None:
                raise DocumentError("The uploaded file is empty or invalid.")
            return str(part.get_filename()), payload
    raise DocumentError("No file was included in the upload.")


def re_full_document_id(value: str) -> bool:
    return len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)


def build_server(application: UiApplication | None = None, *, port: int = 0) -> ThreadingHTTPServer:
    app = application or UiApplication()
    server = ThreadingHTTPServer(("127.0.0.1", port), UiHandler)
    server.application = app  # type: ignore[attr-defined]
    server.csrf_token = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    return server


def serve(*, data_dir: Path | None = None, port: int = 0) -> None:
    server = build_server(UiApplication(data_dir), port=port)
    print(f"Personal AI Core UI: http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
