# ADR-003: Database-durable jobs with a pluggable in-process runner

**Decision.** Ingestion jobs are rows in `ingestion_jobs` (status, stage, attempts, error). `JobRunner`
only schedules them: bounded async workers, exponential backoff, and re-enqueue of queued/orphaned
jobs on start-up. Permanent errors (corrupt file, validation) go straight to `dead`; transient errors
retry up to `max_attempts`. A new version only replaces the old one in a single transaction on success.

**Trade-off.** In-process workers keep local setup to a single command but do not scale across replicas
and two replicas could pick up the same queued job. Production should swap in a Redis/Arq worker
(Phase 10) with row-level claiming (`SELECT ... FOR UPDATE SKIP LOCKED`) behind the same interface.
