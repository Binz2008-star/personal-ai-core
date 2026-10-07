"""Application-owned document storage and text extraction for the local UI."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

MAX_FILE_BYTES: Final = 10 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 500 * 1024 * 1024
MAX_FILE_COUNT: Final = 500
MAX_TEXT_CHARS: Final = 2_000_000
MAX_PDF_PAGES: Final = 200
STALE_UPLOAD_HOURS: Final = 1
ALLOWED_SUFFIXES: Final = frozenset({".md", ".txt", ".pdf"})
_SAFE_DISPLAY = re.compile(r"[^\w .()\-\u0600-\u06ff]", re.UNICODE)


class DocumentError(ValueError):
    """A safe, user-facing document error."""


class PdfExtractorUnavailable(DocumentError):
    """The optional PDF dependency is not installed."""


class QuotaExceeded(DocumentError):
    """Storage quota exceeded."""


class ManifestCorrupt(DocumentError):
    """The durable document manifest could not be trusted."""


@dataclass(frozen=True, slots=True)
class StoredDocument:
    id: str
    display_name: str
    suffix: str
    size_bytes: int
    text_chars: int
    uploaded_at: datetime
    indexed_at: datetime | None
    ingestion_status: Literal["pending", "indexed", "failed"]
    chunk_count: int | None
    failure_reason: str | None
    notice: str | None
    path: Path

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "type": self.suffix.removeprefix("."),
            "size_bytes": self.size_bytes,
            "text_chars": self.text_chars,
            "uploaded_at": self.uploaded_at.isoformat(),
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "ingestion_status": self.ingestion_status,
            "chunk_count": self.chunk_count,
            "failure_reason": self.failure_reason,
            "notice": self.notice,
        }

    def to_manifest_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "suffix": self.suffix,
            "size_bytes": self.size_bytes,
            "text_chars": self.text_chars,
            "uploaded_at": self.uploaded_at.isoformat(),
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "ingestion_status": self.ingestion_status,
            "chunk_count": self.chunk_count,
            "failure_reason": self.failure_reason,
            "notice": self.notice,
        }

    @classmethod
    def from_manifest_dict(cls, data: dict[str, object], base_path: Path) -> "StoredDocument":
        required = {"id", "display_name", "suffix", "size_bytes", "text_chars", "uploaded_at", "ingestion_status"}
        if not required.issubset(data):
            raise DocumentError("The document manifest is invalid.")
        suffix = str(data["suffix"])
        if suffix not in ALLOWED_SUFFIXES:
            raise DocumentError("The document manifest contains an unsupported type.")
        document_id = str(data["id"])
        if not re.fullmatch(r"[0-9a-f]{32}", document_id):
            raise DocumentError("The document manifest contains an invalid identifier.")
        return cls(
            id=document_id,
            display_name=_clean_display_name(str(data["display_name"])),
            suffix=suffix,
            size_bytes=int(data["size_bytes"]),
            text_chars=int(data["text_chars"]),
            uploaded_at=datetime.fromisoformat(str(data["uploaded_at"])),
            indexed_at=(datetime.fromisoformat(str(data["indexed_at"])) if data.get("indexed_at") else None),
            ingestion_status="pending",
            chunk_count=None,
            failure_reason=None,
            notice=(str(data["notice"]) if data.get("notice") else None),
            path=base_path / f"{document_id}{suffix}",
        )


def _clean_display_name(name: str) -> str:
    cleaned = _SAFE_DISPLAY.sub("_", name.strip()).strip(" .")
    return (cleaned or "document")[:160]


def safe_display_name(name: str) -> str:
    """Validate a browser-provided label and return a display-only filename."""
    stripped = name.strip()
    if not stripped or "/" in stripped or "\\" in stripped:
        raise DocumentError("The file name must not contain a path.")
    if stripped in {".", ".."} or any(part in {".", ".."} for part in stripped.split("/")):
        raise DocumentError("The file name must not contain dot segments.")
    return _clean_display_name(stripped)


def extract_text(path: Path, suffix: str) -> str:
    if suffix in {".md", ".txt"}:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentError("The file is not valid UTF-8 text.") from exc
    elif suffix == ".pdf":
        text = _extract_pdf(path)
    else:
        raise DocumentError("This file type is not supported.")
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError("The extracted document text is too large.")
    return text


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PdfExtractorUnavailable(
            "PDF support is not installed. Install personal-ai-core[pdf]."
        ) from exc
    try:
        reader = PdfReader(str(path), strict=False)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentError("The PDF has too many pages for local ingestion.")
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("The PDF could not be read.") from exc
    return text


class DocumentStore:
    """Durable metadata plus server-named files under one app-owned directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_count: int = MAX_FILE_COUNT,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_count = max_count
        self.manifest_path = self.root / "manifest.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._documents: dict[str, StoredDocument] = {}
        self._load_manifest()
        self.cleanup_stale_uploads()
        self._quarantine_orphaned_files()

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            documents = data.get("documents", [])
            if not isinstance(documents, list):
                raise DocumentError("The document manifest is invalid.")
            for raw in documents:
                if not isinstance(raw, dict):
                    raise DocumentError("The document manifest is invalid.")
                doc = StoredDocument.from_manifest_dict(raw, self.root)
                if doc.path.exists() and doc.path.is_file() and not doc.path.is_symlink():
                    self._documents[doc.id] = doc
        except Exception as exc:
            backup = self.manifest_path.with_name(f"manifest.corrupt-{int(time.time())}.json")
            try:
                os.replace(self.manifest_path, backup)
            except OSError:
                pass
            raise ManifestCorrupt(
                "The document list could not be read safely; a backup was kept."
            ) from exc

    def _save_manifest(self) -> None:
        data = {"version": 1, "documents": [doc.to_manifest_dict() for doc in self._documents.values()]}
        temp = self.root / f".manifest-{secrets.token_hex(8)}.tmp"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.manifest_path)
        finally:
            temp.unlink(missing_ok=True)

    def cleanup_stale_uploads(self) -> None:
        threshold = time.time() - (STALE_UPLOAD_HOURS * 3600)
        for path in self.root.glob(".uploading-*"):
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < threshold:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _quarantine_orphaned_files(self) -> None:
        """Move unlisted regular files aside; never delete user data during recovery."""
        known = {record.path.name for record in self._documents.values()}
        quarantine = self.root / "orphans"
        for path in self.root.iterdir():
            if path.name == self.manifest_path.name or path.name.startswith("manifest.corrupt-"):
                continue
            if path.name.startswith(".uploading-") or path.name.startswith(".manifest-"):
                continue
            if path.is_file() and not path.is_symlink() and path.name not in known:
                quarantine.mkdir(exist_ok=True)
                target = quarantine / f"{int(time.time())}-{path.name}"
                path.replace(target)

    def _check_quota(self, incoming_bytes: int) -> None:
        if len(self._documents) >= self.max_count:
            raise QuotaExceeded(f"Maximum number of documents ({self.max_count}) reached.")
        total = sum(doc.size_bytes for doc in self._documents.values())
        if total + incoming_bytes > self.max_total_bytes:
            total_mb = self.max_total_bytes // (1024 * 1024)
            raise QuotaExceeded(f"Total storage quota ({total_mb} MB) would be exceeded.")

    def save(self, display_name: str, content: bytes) -> StoredDocument:
        with self._lock:
            if len(content) > self.max_file_bytes:
                raise DocumentError("The file is larger than the upload limit.")
            self._check_quota(len(content))
            label = safe_display_name(display_name)
            suffix = Path(label).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise DocumentError("Only .md, .txt and .pdf files are supported.")
            if suffix == ".pdf" and not content.startswith(b"%PDF-"):
                raise DocumentError("The uploaded file is not a valid PDF container.")
            if suffix in {".md", ".txt"}:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DocumentError("The file is not valid UTF-8 text.") from exc

            document_id = secrets.token_hex(16)
            final_path = self.root / f"{document_id}{suffix}"
            temp_path = self.root / f".uploading-{document_id}{suffix}"
            temp_path.write_bytes(content)
            record: StoredDocument | None = None
            try:
                text = extract_text(temp_path, suffix)
                temp_path.rename(final_path)
                now = datetime.now(timezone.utc)
                record = StoredDocument(
                    id=document_id, display_name=label, suffix=suffix,
                    size_bytes=len(content), text_chars=len(text),
                    uploaded_at=now, indexed_at=None,
                    ingestion_status="pending", chunk_count=None,
                    failure_reason=None,
                    notice=("no_extractable_text" if not text.strip() else None),
                    path=final_path,
                )
                self._documents[document_id] = record
                self._save_manifest()
                return record
            except Exception:
                self._documents.pop(document_id, None)
                temp_path.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
                raise

    def get(self, document_id: str) -> StoredDocument | None:
        with self._lock:
            return self._documents.get(document_id)

    def list(self) -> tuple[StoredDocument, ...]:
        with self._lock:
            return tuple(self._documents.values())

    def text(self, document_id: str) -> str:
        with self._lock:
            record = self._documents.get(document_id)
            if record is None or not record.path.exists() or record.path.is_symlink():
                raise DocumentError("Document not found.")
            return extract_text(record.path, record.suffix)

    def mark_indexed(self, document_id: str, chunk_count: int) -> StoredDocument:
        with self._lock:
            record = self._documents.get(document_id)
            if record is None:
                raise DocumentError("Document not found.")
            updated = StoredDocument(
                id=record.id, display_name=record.display_name, suffix=record.suffix,
                size_bytes=record.size_bytes, text_chars=record.text_chars,
                uploaded_at=record.uploaded_at, indexed_at=datetime.now(timezone.utc),
                ingestion_status="indexed", chunk_count=chunk_count,
                failure_reason=None, notice=record.notice, path=record.path,
            )
            self._documents[document_id] = updated
            self._save_manifest()
            return updated

    def mark_failed(self, document_id: str, *, reason: str = "index_error") -> StoredDocument:
        """Keep durable user bytes while marking derived indexing as unavailable."""
        with self._lock:
            record = self._documents.get(document_id)
            if record is None:
                raise DocumentError("Document not found.")
            updated = StoredDocument(
                id=record.id, display_name=record.display_name, suffix=record.suffix,
                size_bytes=record.size_bytes, text_chars=record.text_chars,
                uploaded_at=record.uploaded_at, indexed_at=None,
                ingestion_status="failed", chunk_count=None,
                failure_reason=reason, notice=record.notice, path=record.path,
            )
            self._documents[document_id] = updated
            self._save_manifest()
            return updated
