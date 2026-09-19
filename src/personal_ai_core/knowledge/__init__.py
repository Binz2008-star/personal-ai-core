"""Knowledge layer: chunking, embeddings, indexing, retrieval, ingestion.

In-memory implementations behind `core.contracts`. Nothing here is a miniature
pgvector: these satisfy the Core contract, and a persistent vector adapter will
satisfy the same contract later without either owning it.

Everything is exact, deterministic and dependency-free. That is a deliberate
trade -- O(n) scans instead of an approximate index -- and it buys two things
worth more at this stage than speed: a reference implementation an approximate
index can be checked against, and a test suite that needs no service, no model
download and no network.
"""
from .catalog import InMemoryChunkCatalog
from .chunking import FixedSizeChunker
from .embedding import HashingEmbeddingProvider
from .fusion import RRF_K, ReciprocalRankFusion
from .ingestion import IngestionReport, IngestionService
from .language import language_matches
from .lexical_index import InMemoryLexicalIndex
from .retrieval import DEFAULT_CANDIDATE_DEPTH, HybridRetriever
from .vector_index import InMemoryVectorIndex

__all__ = [
    "DEFAULT_CANDIDATE_DEPTH",
    "FixedSizeChunker",
    "HashingEmbeddingProvider",
    "HybridRetriever",
    "InMemoryChunkCatalog",
    "InMemoryLexicalIndex",
    "InMemoryVectorIndex",
    "IngestionReport",
    "IngestionService",
    "RRF_K",
    "ReciprocalRankFusion",
    "language_matches",
]
