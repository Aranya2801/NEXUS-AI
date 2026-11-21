# ADR-001: One SQLAlchemy model set, PostgreSQL+pgvector in prod, SQLite in tests

**Status:** accepted

**Decision.** Models use portable types. `EmbeddingType` maps to `vector(dim)` on PostgreSQL and a JSON
array elsewhere. Tests and offline demos run on SQLite with a numpy cosine fallback; production uses
pgvector and PostgreSQL full-text search.

**Trade-off.** SQLite cannot exercise pgvector/ANN or `tsvector` paths, so those code paths need
integration tests against a real PostgreSQL container (tracked in PROJECT_STATUS.md). The benefit is
that every unit and workflow test runs in seconds with no external services.

**Security consequence.** `org_id`, `collection_id` and `is_current` are denormalised onto `chunks`
so access filters are applied inside the retrieval query itself.
