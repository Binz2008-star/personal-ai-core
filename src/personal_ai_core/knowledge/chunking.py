"""Deterministic text chunking.

Implements `core.contracts.Chunker`.

Generic prose chunking, deliberately not the legacy AST/tree-sitter chunker.
That one is code-oriented and was classified `REWRITE` for exactly this reason:
the Core's knowledge layer must handle documents, not only source files. Code
chunking may return later as a separate strategy behind the same contract.

Determinism is a contract requirement, not a nicety: the same content and
settings must always produce the same chunks with the same offsets, or every
citation becomes unverifiable.
"""
from __future__ import annotations

import re

from ..core.knowledge import Chunk, Document, DocumentVersion

# Paragraph boundary: a blank line, however it is spelled.
_PARAGRAPH = re.compile(r"\n\s*\n")


class FixedSizeChunker:
    """Splits on paragraph boundaries, packing up to `max_chars` per chunk.

    Character-based rather than token-based on purpose: offsets must index the
    source text exactly so `(version_id, start, end)` can be re-read. A
    token-based split would need the tokenizer present to resolve a citation.

    Overlap is supported because a fact split across a boundary is otherwise
    unretrievable, and the legacy chunker used overlap for the same reason.
    """

    def __init__(self, *, max_chars: int = 1000, overlap_chars: int = 100) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        if overlap_chars < 0:
            raise ValueError("overlap_chars must be non-negative")
        if overlap_chars >= max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        self._max = max_chars
        self._overlap = overlap_chars

    def chunk(
        self, *, document: Document, version: DocumentVersion, content: str
    ) -> list[Chunk]:
        spans = self._spans(content)
        return [
            Chunk(
                document_id=document.id,
                version_id=version.id,
                text=content[start:end],
                ordinal=ordinal,
                start=start,
                end=end,
                language=document.declared_language,
            )
            for ordinal, (start, end) in enumerate(spans)
        ]

    def _spans(self, content: str) -> list[tuple[int, int]]:
        if not content.strip():
            return []

        # Paragraph boundaries first: splitting mid-sentence loses meaning that
        # no amount of overlap recovers.
        boundaries = [0]
        for match in _PARAGRAPH.finditer(content):
            boundaries.append(match.end())
        boundaries.append(len(content))

        paragraphs = [
            (boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)
        ]

        spans: list[tuple[int, int]] = []
        cur_start: int | None = None
        cur_end = 0

        for p_start, p_end in paragraphs:
            if cur_start is None:
                cur_start, cur_end = p_start, p_end
                continue
            if p_end - cur_start <= self._max:
                cur_end = p_end
            else:
                spans.append((cur_start, cur_end))
                cur_start, cur_end = p_start, p_end

        if cur_start is not None:
            spans.append((cur_start, cur_end))

        # A single paragraph longer than max_chars still has to be split.
        split: list[tuple[int, int]] = []
        for start, end in spans:
            if end - start <= self._max:
                split.append((start, end))
                continue
            step = self._max - self._overlap
            pos = start
            while pos < end:
                split.append((pos, min(pos + self._max, end)))
                pos += step
        return [(s, e) for s, e in split if content[s:e].strip()]
