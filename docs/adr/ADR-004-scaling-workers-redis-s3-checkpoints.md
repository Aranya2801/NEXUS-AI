# ADR-004: Horizontal scaling — external workers, Redis rate limiting, S3 storage, Postgres checkpoints

Status: accepted (supersedes the "future" notes in ADR-003). Date: 2026-11-21.

## Context
Phase 1–9 ran everything in one process: in-memory job queue and rate limiter, local-disk documents, SQLite LangGraph checkpoints. That cannot
run as several API replicas, and a paused human-review run would be invisible to a replica that did not create it.

## Decision
* **Jobs stay rows in `ingestion_jobs`; the queue is the table.** `process_job` claims a job with an atomic compare-and-set
  (`UPDATE … WHERE status='queued'`), so any number of processes can poll safely and a job never runs twice. `NEXUS_INGESTION_MODE=external`
  makes the API only enqueue; `python -m app.worker` polls, applies exponential backoff from `updated_at`, and recovers jobs stuck in `running`.
  Chosen over Arq/Celery because it adds no new infrastructure, keeps jobs transactional with document rows, and is already durable.
* **Redis** is used only for the shared fixed-window rate limiter (`INCR`+`EXPIRE`). If Redis is unreachable the limiter degrades to per-replica limits and
  logs it (fail-soft rather than fail-open or fail-closed). Redis holds no data that must survive.
* **Object storage:** `S3Storage` (boto3 in threads) for AWS S3/MinIO behind the same `Storage` protocol; the same key-traversal guard as local storage.
* **LangGraph checkpoints:** `AsyncPostgresSaver` on the same database when `NEXUS_CHECKPOINT_BACKEND=postgres` (tables created by `saver.setup()`;
  excluded from Alembic's remit), so a paused run survives restarts and can be resumed through any replica.
* **Schema:** Alembic, one initial migration generated against real PostgreSQL then hand-completed with the extension and the two Postgres-only indexes;
  `alembic check` runs in the test suite to prevent drift. In `prod`, the app no longer calls `create_all`.

## Consequences
* Polling latency (default 1 s) instead of push; adequate for ingestion, and `FOR UPDATE SKIP LOCKED` can replace the CAS if contention appears.
* Fixed-window counters allow bursts up to 2× the limit at window edges.
* The vector dimension is a schema constant (`EMBEDDING_DIM`, default 384): changing the embedding model's dimension needs a new migration and re-ingestion.
* Verified end to end *without Docker* (real PostgreSQL/pgvector, Redis, S3 via moto, two worker processes, API in prod mode); the container images themselves
  have **not** been built in the authoring environment (no Docker daemon) — CI's `docker` job is the first build.
