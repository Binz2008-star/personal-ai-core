"""Chunk catalog.

Retrieval produces ranked chunk ids; provenance requires the chunk itself and
the document it came from. Something has to hold that mapping.

It is a separate object rather than a field on an index because both index
arms need it and neither owns it. Putting it inside one of them would make the
other depend on that one, which is how a hybrid retriever quietly becomes a
vector retriever with a lexical attachment.
"""
from __future__ import annotations

from typing import Iterator, Sequence

from ..core.knowledge import Chunk, Document


class InMemoryChunkCatalog:
    """Chunks by id, plus the source URI of the document each came from."""

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}
        self._source_uris: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._chunks)

    def __iter__(self) -> Iterator[Chunk]:
        return iter(self._chunks.values())

    def register(self, document: Document) -> None:
        self._source_uris[document.id] = document.source_uri

    def add(self, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.id] = chunk

    def get(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def source_uri(self, document_id: str) -> str | None:
        return self._source_uris.get(document_id)

    def remove_document(self, document_id: str) -> None:
        for chunk_id in [
            chunk_id
            for chunk_id, chunk in self._chunks.items()
            if chunk.document_id == document_id
        ]:
            del self._chunks[chunk_id]
        self._source_uris.pop(document_id, None)
