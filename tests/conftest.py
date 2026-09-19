import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_ai_core.core.knowledge import Chunk, Document, DocumentVersion  # noqa: E402


@pytest.fixture
def make_chunk():
    """Build a Chunk without repeating six required fields in every test.

    Offsets default to something self-consistent rather than zero, so a test
    that accidentally depends on start == end fails here rather than later.
    """

    def _make(
        text: str,
        *,
        chunk_id: str | None = None,
        document_id: str = "doc-1",
        version_id: str = "ver-1",
        ordinal: int = 0,
        language: str = "und",
        start: int = 0,
    ) -> Chunk:
        kwargs = {
            "document_id": document_id,
            "version_id": version_id,
            "text": text,
            "ordinal": ordinal,
            "start": start,
            "end": start + len(text),
            "language": language,
        }
        if chunk_id is not None:
            kwargs["id"] = chunk_id
        return Chunk(**kwargs)

    return _make


@pytest.fixture
def document() -> Document:
    return Document(source_uri="file:///notes/example.md", title="Example")


@pytest.fixture
def version(document: Document) -> DocumentVersion:
    return DocumentVersion(document_id=document.id, content_hash="deadbeef")
