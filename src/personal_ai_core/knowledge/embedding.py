"""A deterministic, local, dependency-free embedding provider.

Implements `core.contracts.EmbeddingProvider`.

**Read this before using it for anything that matters.**

This is *not* a semantic embedding model. It is a signed feature-hashing
projection of character n-grams and word tokens. Two texts that say the same
thing in different words score near zero against each other. It captures
surface overlap, nothing more.

It exists for three honest reasons:

  1. The contract needs a real implementation to be proven implementable.
  2. Tests need an embedder that is deterministic across processes and
     machines, with no network and no model download.
  3. It makes the *shape* of the vector path executable now, so a real model
     later is a substitution behind the contract rather than a redesign.

A real embedding model plugs in behind the same `EmbeddingProvider` contract
and changes nothing above it. When one does, `model_id` changes with it -- and
because `Embedding` carries `model_id`, vectors from the two will not be
silently compared. That is the whole point of putting the model identity on
the value object.

Determinism note: Python's built-in `hash()` is salted per process, so a
feature hash built on it would produce different vectors on every run and
different vectors on two machines. `blake2b` is used instead.
"""
from __future__ import annotations

import hashlib
import math
from typing import Sequence

from ..core.errors import EmbeddingError
from ..core.knowledge import Embedding
from .text import character_ngrams, normalize, tokenize

DEFAULT_DIMENSIONS = 256
DEFAULT_NGRAM_SIZE = 3


class HashingEmbeddingProvider:
    """Signed feature hashing over character n-grams and word tokens.

    Features are hashed into `dimensions` buckets with a sign drawn from a
    separate slice of the same digest. The sign is what keeps collisions from
    accumulating in one direction: unrelated features that land in the same
    bucket cancel on average instead of adding.

    Vectors are L2-normalized, so the dot product in `InMemoryVectorIndex` is
    cosine similarity.
    """

    def __init__(
        self,
        *,
        dimensions: int = DEFAULT_DIMENSIONS,
        ngram_size: int = DEFAULT_NGRAM_SIZE,
        model_id: str | None = None,
    ) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if ngram_size < 1:
            raise ValueError("ngram_size must be positive")
        self._dimensions = dimensions
        self._ngram_size = ngram_size
        # The identity encodes every parameter that changes the output. Two
        # configurations that produce incomparable vectors must not share an id.
        self._model_id = model_id or f"hashing-ngram{ngram_size}-{dimensions}d-v1"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed each text, in order.

        Raises `EmbeddingError` for a text with no extractable features. A
        blank chunk has no embedding; producing a zero vector instead would
        make cosine similarity undefined and would rank it arbitrarily against
        everything. Failing here is louder and cheaper than debugging that.
        """
        out: list[Embedding] = []
        for position, text in enumerate(texts):
            vector = self._vector(text)
            if vector is None:
                raise EmbeddingError(
                    f"text at position {position} has no embeddable content"
                )
            out.append(Embedding(vector=vector, model_id=self._model_id))
        return out

    def _vector(self, text: str) -> tuple[float, ...] | None:
        if not normalize(text):
            return None

        accumulator = [0.0] * self._dimensions
        features = character_ngrams(text, self._ngram_size) + tokenize(text)
        if not features:
            return None

        for feature in features:
            index, sign = self._bucket(feature)
            accumulator[index] += sign

        magnitude = math.sqrt(sum(value * value for value in accumulator))
        if magnitude == 0.0:
            # Every feature cancelled. Vanishingly unlikely, but a zero vector
            # must never escape: it would be silently unrankable.
            return None
        return tuple(value / magnitude for value in accumulator)

    def _bucket(self, feature: str) -> tuple[int, int]:
        digest = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=8
        ).digest()
        index = int.from_bytes(digest[:4], "big") % self._dimensions
        sign = 1 if digest[4] & 1 else -1
        return index, sign
