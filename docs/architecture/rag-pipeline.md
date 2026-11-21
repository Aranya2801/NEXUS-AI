# Retrieval pipeline (Phase 3)

```mermaid
flowchart TD
  Q[Query + Principal + filters] --> S[build_scope<br/>org · accessible collections · version visibility · metadata]
  S --> D[Dense<br/>pgvector &lt;=&gt; HNSW / numpy cosine]
  S --> L[Sparse<br/>PostgreSQL FTS ts_rank_cd / in-memory BM25]
  D --> F[Reciprocal Rank Fusion<br/>Σ w / (k + rank)]
  L --> F
  F --> H[Hydrate top-k<br/>title · page · section · version · parent context]
```

## Guarantees (each covered by tests on SQLite **and** PostgreSQL)

1. **Authorisation is in the ranking query.** `build_scope` produces the WHERE clause used by *every* retriever.
   Requested `collection_ids` are *intersected* with the user's accessible set; a hidden or foreign collection
   yields zero results, never an error that reveals it exists, and never widened access.
2. **Version visibility.** `current` (default) = only the live version. `all_versions` = every version that was
   ever live (a failed/never-swapped upload is excluded). `as_of=T` = the version live at T.
3. **Embedding-model safety.** Each chunk records `embed_model`; dense search only compares vectors produced by the
   same model as the query, so switching providers cannot silently return garbage similarities.
4. **Auditability.** Searches are logged with a hash of the query, not its text.

## Implementation notes and honest limits

* **SQLite path** loads the scoped corpus and scores it in memory (exact cosine; BM25 with non-negative IDF).
  It is for tests/demos, O(corpus) per query.
* **PostgreSQL path** uses `chunks.embedding <=> :q` (cosine) with an HNSW index and a GIN `to_tsvector('simple', text)`
  index. Sparse queries are OR-ed tokens ranked by `ts_rank_cd`, which is not identical to BM25 (no length
  normalisation parameters); results between the two backends can therefore differ in order.
* Filtered HNSW search can lose recall; `hnsw.ef_search` is raised to 200 per query. The integration tests run on tiny
  corpora and do **not** prove the planner uses the HNSW index or measure recall at scale; that belongs to the Phase 9
  benchmarks.
* The default `mock` embedder is lexical (feature hashing). "Dense" results with it are not semantic. Use the
  OpenAI-compatible provider (tested against a mock transport only) or sentence-transformers (untested) for real runs.
* The BM25/RRF weights are defaults, not tuned; tuning is part of the adaptive-retrieval experiment (Phase 4/9).
