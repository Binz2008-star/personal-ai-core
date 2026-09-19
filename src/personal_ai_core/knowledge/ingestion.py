"""Ingestion: document content in, indexed and catalogued chunks out.

Composed from contracts, not from implementations. It is the one place that
knows the order of operations -- version, chunk, embed, index, catalogue -- so
that order exists once rather than in every caller.

Re-ingesting a document **replaces** it. Every chunk of the previous version is
removed from both indexes and the catalog before the new ones are added.
Additive re-ingestion is the alternative and it is worse: stale chunks from a
deleted paragraph keep being retrieved and cited, and the citation resolves to
text that is no longer there.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..core.contracts import Chunker, EmbeddingProvider, LexicalIndex, VectorIndex
from ..core.knowledge import Document, DocumentVersion
from .catalog import InMemoryChunkCatalog


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """What one ingestion actually did.

    Returned rather than logged. A caller that cannot see how many chunks were
    produced cannot tell an empty document from a broken chunker.
    """

    document_id: str
    version: DocumentVersion
    chunk_count: int
    replaced_previous: bool


def content_hash(content: str) -> str:
    """Identity of the text itself, which makes re-ingestion detectable."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class IngestionService:
    def __init__(
        self,
        *,
        chunker: Chunker,
        embedder: EmbeddingProvider,
        vector_index: VectorIndex,
        lexical_index: LexicalIndex,
        catalog: InMemoryChunkCatalog,
    ) -> None:
        self._chunker = chunker
        self._embedder = embedder
        self._vector_index = vector_index
        self._lexical_index = lexical_index
        self._catalog = catalog
        self._revisions: dict[str, int] = {}

    def ingest(self, document: Document, content: str) -> IngestionReport:
        previous = self._revisions.get(document.id)
        revision = 1 if previous is None else previous + 1

        version = DocumentVersion(
            document_id=document.id,
            content_hash=content_hash(content),
            revision=revision,
        )

        if previous is not None:
            self._vector_index.remove_document(document.id)
            self._lexical_index.remove_document(document.id)
            self._catalog.remove_document(document.id)

        chunks = self._chunker.chunk(
            document=document, version=version, content=content
        )

        self._catalog.register(document)
        if chunks:
            embeddings = self._embedder.embed([chunk.text for chunk in chunks])
            self._vector_index.add(chunks, embeddings)
            self._lexical_index.add(chunks)
            self._catalog.add(chunks)

        self._revisions[document.id] = revision
        return IngestionReport(
            document_id=document.id,
            version=version,
            chunk_count=len(chunks),
            replaced_previous=previous is not None,
        )
