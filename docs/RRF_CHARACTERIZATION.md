# RRF Characterization — legacy hybrid retrieval

**Source (pinned, read-only, never modified):**
`unified-llm-local @ 21a36b0765d89390eed63da094977e4bb8e5b4c2`
· `chunker_v4.py` L460–532 — `CREATE FUNCTION hybrid_search(...)`
· `brain_agent_v4.py` L222–286 — the only call site

This records what the legacy implementation **does**. It is not a design, not an
endorsement, and not the Core contract.

```text
legacy implementation → characterization → observed behaviour
                      → Core contract → in-memory implementation
```

The Phase 0 audit classified this RRF `implemented, unproven` with **zero** dedicated
tests. `tests/characterization/test_legacy_rrf.py` is the first test coverage it has had.

`tests/characterization/rrf_transcription.py` is a faithful Python transcription of the
SQL, used so the behaviour can be pinned without importing the legacy package or requiring
Postgres. It is **not** a runtime dependency of the Core and must not be promoted into
`personal_ai_core`.

---

## Observed behaviour

### Constants

| Observed | Value |
|---|---|
| `rrf_k` | **60** — function default, and passed explicitly by the call site |
| `candidate_k` | **`GREATEST(match_count * 8, 50)`** — ×8 with a floor of 50 |
| `match_count` at the call site | `top_k * 3` when any filter is set, else `top_k`; `top_k` defaults to 8 |

### Candidate generation

**Vector arm (`vec_cand`)** — `ORDER BY embedding <=> query_embedding`, `ROW_NUMBER()`
1-based ascending distance, `LIMIT candidate_k`. **No `WHERE` clause and no similarity
threshold**: every chunk in the corpus is eligible, however distant.
`vec_sim = 1 - cosine_distance`.

**Lexical arm (`kw_cand`)** — `WHERE content_tsv @@ kw`, so it **is** filtered;
`ORDER BY ts_rank_cd(...) DESC`, `ROW_NUMBER()` 1-based, `LIMIT candidate_k`. Returns
**rank only** — no lexical score reaches the output.

**Query preparation** — `websearch_to_tsquery('english', query_text)`, falling back to
`plainto_tsquery('english', query_text)` when the first yields NULL or an empty tsquery.

### Scoring

```sql
COALESCE(1.0 / (rrf_k + v.vec_rank), 0.0) + COALESCE(1.0 / (rrf_k + k.kw_rank), 0.0)
```

Absence from a list contributes **exactly 0.0** — not a penalty, not `1/(k+∞)`.

With `k=60` the curve is deliberately flat: rank 1 versus rank 2 differ by
`1/61 − 1/62 ≈ 0.00026`. Appearing in **both** lists therefore beats ranking first in one.

### Merge and output

`FULL OUTER JOIN vec_cand ON kw_cand USING id` — the union of both id sets, merged on id,
so a chunk found by both paths yields **one** row carrying both contributions.

`vec_sim = COALESCE(v.vec_sim, recomputed via JOIN chunks_v4)` — a lexical-only hit outside
the vector pool still reports a similarity, recomputed through the join.

Final: `ORDER BY cb.rrf_score DESC LIMIT match_count`.

### Edge cases

| Case | Observed |
|---|---|
| No lexical matches | degrades to vector-only; every vector hit still returned |
| Empty corpus | empty result (the vector arm is unfiltered, so nothing else empties it) |
| Duplicate across arms | merged to one row, contributions summed |
| Output size | `match_count`, not `candidate_k` |

---

## Ambiguities — recorded, not resolved

### A1 · Tie-breaking is undefined

`ORDER BY cb.rrf_score DESC` carries **no tie-breaker**, and both `ROW_NUMBER()` windows
order by a single expression with no tie-breaker either. Two chunks with an identical score
have an unspecified relative order, which may vary between runs, plans or parallel workers.

The characterization asserts only that ties are *reachable*. It asserts no order, because
the source defines none. The Core contract already specifies deterministic tie-breaking
(`FusedCandidate.sort_key`) — that is a **Core decision**, not observed behaviour.

### A2 · The lexical arm is hard-coded to English

`websearch_to_tsquery('english', …)`, `plainto_tsquery('english', …)`, and the stored
column is `to_tsvector('english', content)`. Arabic is neither stemmed nor usefully
tokenised by the English configuration, so for an Arabic query the lexical arm contributes
little or nothing — **silently**, because the vector arm still returns results (ADR-006).

### A3 · `ts_rank_cd` is not reproduced

The transcription accepts `ts_rank` as an input rather than recomputing it. Reproducing
PostgreSQL's English text search in Python would be inventing behaviour, not characterizing
it. Any adaptation that must match legacy lexical ranking exactly has to be verified
against Postgres directly.

---

## Defect found during characterization

**VERIFIED.** Two of the three filter parameters on `search_brain()` always return an empty
list.

`hybrid_search()` declares `RETURNS TABLE (id, project_id, file_path, chunk_name, content,
similarity, rank)` — seven columns. It does **not** return `language` or `chunk_type`.

The call site then filters in Python:

```python
if language:   results = [r for r in results if r.get("language")   == language]
if chunk_type: results = [r for r in results if r.get("chunk_type") == chunk_type]
```

`r.get("language")` is always `None`, so `None == "ar"` is `False` for every row and the
result is `[]`. The same holds for `chunk_type`. Only `project_id` works, because that
column *is* returned.

Confirmed against the live schema (Neon `misty-sun-80388989`, read-only): `chunks_v4`
**does** have `language text NULL` and `chunk_type text NOT NULL DEFAULT 'generic'`. The
columns exist; the function simply never selects them.

Compounding it: setting either filter switches `match_count` to `top_k * 3`, so the query
fetches three times as much work before discarding all of it.

**Not fixed here.** `unified-llm-local` is a pinned, read-only source. This is recorded so
that (a) the Core contract returns filterable fields deliberately, and (b) nobody adapts
this path assuming the filters work.

---

## Consequences for the Core contract

The Core contract at `b5347e7` already diverges from observed behaviour in three places.
Each divergence is deliberate:

| Observed legacy | Core contract | Why |
|---|---|---|
| tie order undefined | `FusedCandidate.sort_key` — score desc, then `chunk_id` asc | an unstable ranking is untestable |
| lexical arm English-only, implicit | `language` explicit on `RetrievalQuery` and `LexicalIndex.search` | forces a decision instead of a silent default |
| filter fields absent from output | provenance is a typed contract | a result must carry what it is filtered and cited by |

No Core contract was changed in this step.

## Not done in this step

No in-memory implementation · no RRF implementation in the Core · no pgvector, HNSW or
Neon adapter · no context assembly · no modification to `unified-llm-local`, Neon, or any
Core contract.
