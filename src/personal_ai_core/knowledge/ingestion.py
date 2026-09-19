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

    `unchanged` reports that the content hash matched and nothing was done.
    Without it a no-op is indistinguishable from a re-ingestion that happened
    to produce the same chunk count, and "did this change anything" is the
    question a caller most needs answered.
    """

    document_id: str
    version: DocumentVersion
    chunk_count: int
    replaced_previous: bool
    unchanged: bool = False


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
        self._versions: dict[str, DocumentVersion] = {}
        self._chunk_counts: dict[str, int] = {}

    def ingest(self, document: Document, content: str) -> IngestionReport:
        """Ingest `content` as the current version of `document`.

        Idempotent by content hash. Re-ingesting text identical to the stored
        version does nothing at all and returns the existing version.

        That short-circuit is the whole reason `content_hash` exists rather
        than being a field nobody reads. Without it, re-running an unchanged
        source mints a new version id and a new id for every chunk, so every
        citation already issued against the previous version stops resolving --
        silently, and while still looking correct.
        """
        digest = content_hash(content)
        previous = self._versions.get(document.id)

        if previous is not None and previous.content_hash == digest:
            # A deliberate pure no-op: no re-chunk, no re-embed, no index
            # mutation, and no catalog write. Anything touched here would move
            # `index_version`, which provenance records, for a version that did
            # not change.
            return IngestionReport(
                document_id=document.id,
                version=previous,
                chunk_count=self._chunk_counts.get(document.id, 0),
                replaced_previous=False,
                unchanged=True,
            )

        revision = 1 if previous is None else previous.revision + 1

        version = DocumentVersion(
            document_id=document.id,
            content_hash=digest,
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

        self._versions[document.id] = version
        self._chunk_counts[document.id] = len(chunks)
        return IngestionReport(
            document_id=document.id,
            version=version,
            chunk_count=len(chunks),
            replaced_previous=previous is not None,
        )
