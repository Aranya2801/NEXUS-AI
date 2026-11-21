# Deployment

## Docker Compose (reference stack)
```bash
cp .env.example .env            # set NEXUS_JWT_SECRET (>= 32 random chars), POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD
docker compose up --build -d    # UI on http://localhost:8080
docker compose logs -f api worker
```
Services: `postgres` (pgvector/pgvector:pg16), `redis`, `minio` (documents), `api` (runs `alembic upgrade head` on start; prod mode: no `/docs`), `worker` ×2
(`python -m app.worker`), `frontend` (nginx: static UI + `/api` reverse proxy with SSE buffering off).
Start-up order is enforced by health checks: databases → api (migrates) → worker/frontend.

> **Verification status:** the compose file passes `docker compose config`, and the *same* stack (real PostgreSQL+pgvector, Redis, S3, Alembic, API in `prod`
> mode, two external workers, Postgres checkpointer) was exercised end to end outside Docker. The images were **not built** in the authoring environment (no Docker
> daemon); the CI `docker` job builds them. Expect to fix small image-level issues on first run.

First user: `POST /auth/register` creates an organisation and its admin (or use the UI's login page).

## Using real models
Set `NEXUS_LLM_PROVIDER` (`openai` | `anthropic`), `NEXUS_LLM_MODEL`, `NEXUS_LLM_API_KEY`, `NEXUS_EMBEDDING_PROVIDER` (`openai` | `sentence_transformers`),
`NEXUS_EMBEDDING_MODEL`, `NEXUS_EMBEDDING_DIM` (must equal the model's output dimension and the migrated schema; the default schema is 384).
`sentence_transformers` needs extra packages not in the image. After changing models, **re-run the evaluation and recalibrate the abstention gate**
(`docs/evaluation.md`); results measured with mock components do not transfer.

## Without Docker
`make install && make test && make run` (SQLite + local files, mock models). For PostgreSQL: `NEXUS_DATABASE_URL=postgresql+asyncpg://…`, then `make migrate`.
Separate ingestion: `NEXUS_INGESTION_MODE=external` on the API and `python -m app.worker` elsewhere.

## Operations
* Health: `/health/live`, `/health/ready`. Metrics: `GET /admin/metrics` (admin bearer token; see `infrastructure/monitoring/prometheus.yml`).
* Tracing: `pip install -r backend/requirements-otel.txt`, `NEXUS_OTEL_ENABLED=true`, `NEXUS_OTEL_ENDPOINT=…`.
* Backups: Postgres volume and the MinIO bucket together (documents and their chunks must stay consistent).
* Scaling: add API replicas (Redis limiter + Postgres checkpoints make this safe) and workers (`deploy.replicas`).
