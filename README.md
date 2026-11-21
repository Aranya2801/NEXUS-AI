# NEXUS AI

**Enterprise multi-agent knowledge intelligence and research platform** — a research-grade, security-first
RAG system built with FastAPI, LangGraph, PostgreSQL/pgvector and React + TypeScript.

> **Status: all 18 build phases are implemented and tested** (491 backend tests incl. real PostgreSQL/pgvector, Alembic, S3 and Redis;
> 56 frontend tests). Two things to keep in mind: (1) every measurement so far was made with **offline mock components**
> (hashing embedder, extractive mock LLM) on a **synthetic** benchmark, so results validate plumbing and relative strategy behaviour — not real-world answer
> quality; (2) the Docker images have **not been built** by the authors yet (no Docker daemon was available); the same stack was verified end to end outside Docker.
> See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for details and [`docs/research/results.md`](docs/research/results.md) for the generated results.

## What works today

| Area | Implemented & tested |
|---|---|
| Auth | Org registration, JWT login, roles (admin/editor/viewer), admin-invited users, login rate limit, audit log |
| Multi-tenancy | Org isolation; restricted collections with per-user ACL; hidden resources return 404 (no existence leak) |
| Ingestion | PDF, DOCX, PPTX, XLSX, TXT, MD, HTML, CSV; page/section provenance; PDF header/footer removal; exact + near-duplicate detection; versioning with atomic swap; async jobs with retries, dead-lettering and crash recovery |
| Multimodal | Image uploads (PNG/JPEG/WebP/GIF/TIFF/BMP; SVG refused), real tesseract OCR, embedded images from PDF/DOCX/PPTX, PDF tables, vision captions (mock, OpenAI-compatible, Anthropic adapters), figure/table chunk kinds + `kinds` search filter, image-grounded search (`/search/multimodal`), tenant-isolated figure serving; see [docs/multimodal.md](docs/multimodal.md) |
| Connectors | Google Drive, SharePoint (delta), Slack, Teams, read-only SQL, REST; incremental sync, deletion propagation, strict ACL gating, encrypted write-only credentials, SSRF guard; tested against fake sources (no real tenant) — [docs/connectors.md](docs/connectors.md) |
| Autonomous agents | Tool registry (search, graph, read, calculator, sandboxed python), validated LLM/rule planners with dynamic re-planning, parallel DAG execution, hard budgets, verified answers, step traces; code execution off by default — [docs/agentic.md](docs/agentic.md) |
| LLMOps | Model routing per task with fallback chains and circuit breakers, per-org daily budgets (degrade, then HTTP 429), tenant-keyed exact-match cache, versioned prompt overrides with rollback, per-call metering (hashes/counts only), admin usage API; tested with mock models only — [docs/llmops.md](docs/llmops.md) |
| AI security & governance | Per-org policy: PII/secret detection (Luhn/mod-97-checked) with redact/flag/reject at ingestion, query block/redact, injection-query audit, redaction of outbound LLM prompts and answers; HMAC-signed, verifiable, exportable audit log; mock-free unit + API tests, regex-based recall only — [docs/security/governance.md](docs/security/governance.md) |
| Distributed operation | Kubernetes manifests (Kustomize; hardened pods, HPAs, PDBs, NetworkPolicies, migration Job) validated **statically only**; Redis leases for cross-replica single-flight, event bus (fan-out + at-least-once with dead-letter) for cache coherence, queue-depth gauge, worker heartbeat — [docs/deployment-kubernetes.md](docs/deployment-kubernetes.md) |
| Continuous learning | Consented human feedback with reasons, org-specific retrieval regression evaluation, prompt A/B experiments with confidence intervals and an `insufficient data` guard (humans conclude), automated quality-regression monitoring — proxies, no auto-promotion; [docs/learning.md](docs/learning.md) |
| Release & cloud engineering | Tag-driven CI/CD (signed images, SBOM, scan), environment-gated deploys with automatic workload rollback, expand-only migration lint, Terraform for AWS (EKS, RDS+pgvector, Redis, S3, KMS, Backup, OIDC), backup/restore drill tested on real PostgreSQL; [docs/releases.md](docs/releases.md), [docs/disaster-recovery.md](docs/disaster-recovery.md), [docs/infrastructure-aws.md](docs/infrastructure-aws.md). **Workflows, Terraform and manifests were validated statically only, never run on GitHub/AWS/Kubernetes** |
| Chunking | Section-bound parent/child chunks with overlap, char offsets, page ranges |
| Upload safety | Size cap, extension allow-list, magic-byte check, zip-bomb guard, filename sanitising |
| Retrieval | Dense (pgvector/HNSW), sparse (PostgreSQL FTS / BM25) and hybrid search with Reciprocal Rank Fusion; scope (tenant, ACL, version, metadata, as-of date) enforced inside the query; per-stage latency timings |
| Embeddings | Offline mock (feature hashing), OpenAI-compatible endpoint (tested with a mock transport), sentence-transformers (untested) |
| Advanced RAG | LLM interface (offline mock, OpenAI-compatible, Anthropic adapters), lexical reranker (cross-encoder adapter untested), query rewrite / decomposition / multi-query / HyDE, index-only evidence compression, retrieval-confidence score, rule-based query classifier with an escalation ladder (bounded by `max_retrieval_loops`) |
| Knowledge graph | Rule-based and validated-LLM entity/relation extraction, persistent entity resolution (aliases, acronyms, conservative fuzzy merge), graph rebuilt per document version, multi-hop graph retrieval fused into ranking, scope-enforced traversal, browse/query/reindex endpoints |
| Multi-agent workflow | LangGraph StateGraph with 8 specialist agents: parallel retrieval/graph branches, bounded re-retrieval loop, claim-level verification and citation validation, injection-safe grounded answers, quality control, human-in-the-loop `interrupt` with SQLite checkpoints (resumable across restarts), SSE streaming, persisted conversations and inspectable runs |
| Memory | Long-term preferences, session working set, research contexts (pinned documents + page-cited findings that scope chat), document history; TTL expiry + purge job, per-user privacy (even from admins), access re-validated on read, user-controlled deletion incl. conversations and checkpoints |
| Reports | Async, page-cited research reports (methods, datasets, results table, limitations, contradiction candidates, stated gaps); every number/quote verified against sources; redacted if access is revoked; export to Markdown, DOCX, PDF |
| Frontend | React 19 + TypeScript (Vite): login, dashboard, document manager + source viewer, evidence-first research chat (SSE streaming, agent timeline, citations with page + quote, verification badges, human-review banner), knowledge-graph explorer, run inspector, citation explorer, reports (+export), evaluation, memory settings, admin. JWT in `sessionStorage`; every API response validated with zod |
| Evaluation | Retrieval (P@k, R@k, MRR, nDCG), engineering (latency, LLM calls) and answer metrics; 122-question synthetic benchmark with programmatically verified gold; systems S1–S6; paired bootstrap, Wilcoxon + Holm, AUROC; dev/test split; abstention-gate calibration; generated report |
| Observability | Prometheus metrics (`/admin/metrics`), optional OpenTelemetry tracing and LangSmith, per-run agent traces |
| Operations | Docker Compose (Postgres+pgvector, Redis, MinIO, API, 2 workers, nginx UI), Alembic migrations (drift-checked in tests), Postgres LangGraph checkpointer, Redis-shared rate limiter, S3/MinIO storage, atomic job claiming, standalone workers |
| API | Auth, collections, upload, documents, chunk "source viewer", job status, `/search`, `/search/advanced`, `/graph/*`, `/chat`, `/agents/runs`, `/conversations`, `/memory/*`, `/reports` (+export), health |

## Quick start

**Docker Compose (full stack):**
```bash
git clone https://github.com/Aranya2801/NEXUS-AI.git nexus-ai && cd nexus-ai
cp .env.example .env           # set NEXUS_JWT_SECRET and the passwords
docker compose up --build -d   # UI on http://localhost:8080 — see docs/deployment.md
```

**Local development (SQLite, no external services):**
```bash
make install                   # venv + backend deps + npm ci
make test test-frontend        # 491 backend + 60 frontend tests
make run                       # API on http://127.0.0.1:8000  (docs at /docs)
make frontend                  # UI on http://localhost:5173 (proxies /api to the API)
```

Try it (SQLite + local storage, no external services, offline mock embeddings):

```bash
B=http://127.0.0.1:8000
curl -X POST $B/auth/register -H 'content-type: application/json' \
  -d '{"organization_name":"Demo","email":"me@example.com","password":"a-long-demo-password"}'
TOKEN=$(curl -s -X POST $B/auth/login -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-demo-password"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
CID=$(curl -s -X POST $B/collections -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"papers"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -X POST $B/collections/$CID/documents -H "Authorization: Bearer $TOKEN" -F file=@paper.pdf
curl $B/documents -H "Authorization: Bearer $TOKEN"
curl -X POST $B/search -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query":"transformer attention limitations","mode":"hybrid","top_k":5,"include_parent":true}'
```

## Configuration

All settings are environment variables prefixed `NEXUS_` (see `.env.example` and `backend/app/core/config.py`).
Deployment guide: [`docs/deployment.md`](docs/deployment.md). In `NEXUS_ENV=prod` the app refuses to start with a default or short `NEXUS_JWT_SECRET`.
Never commit `.env`.

## Research and evaluation

The adaptive-retrieval study is pre-registered in [`docs/research/adaptive-retrieval-protocol.md`](docs/research/adaptive-retrieval-protocol.md) (with a change log of every
deviation). It was executed once, on mock components and a synthetic benchmark; the generated report is [`docs/research/results.md`](docs/research/results.md)
(reproduce with `make eval`; method and honest takeaways in [`docs/evaluation.md`](docs/evaluation.md)). In that run the adaptive policy was non-inferior to the fixed strong
pipeline at ~40% of its LLM calls — **but a simple hybrid + rerank baseline scored best overall**, and the headline claims still need a real-model re-run.

## Design notes

* **Access control is part of retrieval**, not a post-filter: chunks carry `org_id`, `collection_id` and
  `is_current`, so queries are scoped before ranking.
* **Offline mock mode:** the default embedder is a deterministic feature-hashing model. It captures
  lexical overlap only; retrieval-quality experiments must use a real embedding model and say so.
* Memory and reports: [`docs/architecture/memory-and-reports.md`](docs/architecture/memory-and-reports.md) (sample output in [`docs/examples/`](docs/examples)). Workflow design: [`docs/architecture/langgraph-workflow.md`](docs/architecture/langgraph-workflow.md). Knowledge graph design: [`docs/architecture/knowledge-graph.md`](docs/architecture/knowledge-graph.md). Retrieval design and limits: [`docs/architecture/rag-pipeline.md`](docs/architecture/rag-pipeline.md). Architecture decisions: [`docs/adr/`](docs/adr). Security: [`docs/security/threat-model.md`](docs/security/threat-model.md). API: [`docs/api/`](docs/api/README.md). Architecture diagram: [`docs/architecture/architecture.md`](docs/architecture/architecture.md).

## Known limitations

* **Mock-only measurements** and a **synthetic, non-human-annotated** benchmark (24 documents); real embedding/LLM runs have not been done. Gates other than the
  abstention threshold (evidence sufficiency 0.35, completeness, review thresholds, report relevance floor, contradiction/gap cues) are heuristic and **uncalibrated**;
  confidence is backend-sensitive (SQLite BM25 vs PostgreSQL FTS), so recalibrate per deployment.
* Docker images unbuilt by the authors; CI has not run on GitHub yet; no TLS in the compose stack; no MFA/SSO; JWT in `sessionStorage` (see ADR-002); not audited.
* The rule-based query classifier misses most exploratory phrasings; graph extraction/resolution quality is unmeasured; PDF tables are plain text; OCR hook,
  sentence-transformers embedder and cross-encoder reranker are untested; verification is lexical + optional LLM judge.
* PostgreSQL retrieval is tested on tiny corpora only (no recall/latency-at-scale measurements). The vector dimension is a schema constant (ADR-004).
* Reports are unversioned and in-flight reports fail on restart; ingestion polling adds up to ~1 s latency in external mode.

## Tests

`make test test-frontend` — **374 backend tests** (parsing, chunking, validation, auth, tenant isolation, ACLs, versioning, retries, RRF/BM25, search scope/temporal,
adaptive retrieval, knowledge graph, 8-agent workflow incl. injection and human review, memory, reports, admin/feedback, evaluation metrics and harness, observability,
**real PostgreSQL + pgvector** integration with Alembic upgrade/drift-check/downgrade and the Postgres checkpointer, S3 via moto, Redis via a real `redis-server`,
atomic job claiming and external workers) and **60 frontend tests** (Vitest + Testing Library, typecheck, production build).

## License

MIT — see [LICENSE](LICENSE).
