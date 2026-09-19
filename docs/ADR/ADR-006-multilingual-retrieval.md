# ADR-006 — Retrieval is multilingual by design

**Status:** Accepted · Phase 0

## Context

**VERIFIED SOURCE FACT.** The audited hybrid search in `unified-llm-local` @ `21a36b0`
(`chunker_v4.py` L460–532) implements real RRF over pgvector/HNSW and a tsvector lexical
arm — but the lexical arm is hard-coded to English:

```sql
content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
kw := websearch_to_tsquery('english', query_text);
```

PostgreSQL's English configuration does not stem or tokenize Arabic usefully. Arabic
lexical retrieval would not error — it would quietly return worse results, leaving the
hybrid search running on its vector arm alone while appearing to work.

The product treats Arabic and English as first-class. Inheriting this would embed a silent
defect in the retrieval foundation.

## Decision

Language is a first-class retrieval parameter, not a configuration default.

The RRF structure and the pgvector/HNSW schema are adapted. The English-only text-search
configuration is not. Per-language configuration is selected at index and query time, with
a language-neutral fallback where no good configuration exists.

Arabic cases are required in every retrieval test suite.

## Consequences

Indexing carries a language dimension, and documents need reliable language detection —
new work with its own failure modes. Mixed Arabic/English queries are a required test case.

Until Arabic lexical retrieval is implemented and tested, Arabic retrieval quality is
**unproven** and must be reported as such rather than assumed from English results.
