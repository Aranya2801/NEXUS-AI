# PROJECT_STATUS

## Completed (tested)
- Phase 1 foundation: config (prod secret guard), JSON logging, exceptions, bcrypt+JWT security,
  full SQLAlchemy schema (orgs, users, collections+ACL, documents, versions, chunks with page/section
  provenance, ingestion jobs, entities/mentions/relations, conversations, memory, audit log).
  `pytest`: 4 passing.
- Phase 2 (part 1, document processing): `document_processing/` — parsers for PDF/DOCX/PPTX/XLSX/TXT/MD/HTML/CSV with page+section
  provenance, PDF running header/footer removal, optional OCR hook (needs pytesseract+pdf2image, untested),
  normalisation, language detection (stop-word/script heuristic), bottom-k MinHash near-duplicate sketch,
  section-bound parent/child chunker with overlap and optional lexical-semantic boundaries;
  `ingestion/validation.py` (size, allow-list, magic bytes, zip-bomb guard, filename sanitising).
  Total `pytest`: 16 passing (python -m pytest in backend/).
- Pinned versions verified against PyPI at build time: langgraph 1.2.22, langchain-core 1.8.1,
  FastAPI 0.163.1, SQLAlchemy 2.1.9, pydantic 2.14.3 (see backend/requirements.txt).
- LangGraph APIs checked locally: StateGraph, Send, interrupt, Command, RetryPolicy, SqliteSaver.
- Phase 2 (part 2, COMPLETE): auth (register-org/login/admin-invited users, role checks re-read from DB each request,
  login rate limit, audit log), collection ACLs (hidden resources → 404), local storage backend, ingestion service
  (validation, exact dedupe, versioning with atomic current-version swap, near-duplicate flagging), in-process job
  runner (retries with backoff, dead-letter, crash recovery), FastAPI app factory, upload/status/source-viewer/delete
  endpoints, security headers + request IDs. Verified with a real uvicorn + curl run. `pytest`: 26 passing.
- GitHub readiness: README (honest status), MIT LICENSE, CI workflow (not yet run on GitHub), SECURITY.md, ADR-001..003,
  .gitignore, git repo initialised on `main` and files staged — NOT committed (no git identity configured).
- Phase 3 (COMPLETE): real embedding providers (OpenAI-compatible: tested via mock transport; sentence-transformers: untested),
  `retrieval/` (scope builder enforcing tenant/ACL/version/metadata/as-of inside the query, dense pgvector+numpy, sparse
  PostgreSQL FTS + in-memory BM25, RRF, hydration with parent context), `POST /search` with per-stage timings, query-hash audit.
  PostgreSQL + pgvector path integration-tested for real via `pgserver` (found and fixed 2 asyncpg/pgvector bugs that SQLite hid).
  Chunks record `embed_model`; dense search compares only same-model vectors. `pytest`: 46 passing, also in a clean venv.
- Docs: docs/architecture/rag-pipeline.md.
- Phase 4 (COMPLETE): `app/llm/` (interface, offline MockLLM, OpenAI-compatible + Anthropic adapters tested via mock transports,
  tolerant JSON parsing, usage metering), `app/reranking/` (lexical-v1 tested; cross-encoder adapter UNTESTED),
  `retrieval/transform.py` (rewrite, decompose, multi-query, HyDE; validated, always falls back to the original query),
  `retrieval/compression.py` (INDEX-ONLY extractive compression: the LLM can only choose source sentence indices, so
  injected text cannot reach evidence; lexical fallback), `retrieval/confidence.py`, `retrieval/adaptive.py` (rule-based
  classifier + escalation ladders + FixedPolicy), `retrieval/pipeline.py` (executes plans; `run_adaptive` bounded by
  `max_retrieval_loops`), `POST /search/advanced` (trace, plan, confidence, attempts, LLM usage).
  Adversarial tests: hostile/garbled LLM output, dead LLM, injection payload in passages, loop cap, tenant isolation for
  every plan. Also verified on real PostgreSQL+pgvector. `pytest`: 78 passing.
- Research: docs/research/adaptive-retrieval-protocol.md (pre-registered hypotheses/methodology; NO results yet).
- Phase 5 (COMPLETE): `app/knowledge_graph/` — rule-based extractor (offline default), validated LLM extractor (entities must occur in
  text, relations must quote a verbatim sentence with both endpoints, closed predicate vocabulary, caps), entity resolution
  (canonicalisation, alias table, acronym unification, conservative fuzzy merge, per-org), `GraphIndexer` (extract-first/no-write,
  persist in the ingestion transaction, rebuild-per-version, orphan GC, reindex endpoint, delete cleanup), `GraphRetriever`
  (seed matching, bounded BFS, scope-enforced traversal, evidence-chunk ranking), pipeline integration via `plan.graph`
  (graph ranking fused by RRF + bounded rerank bonus), endpoints `/graph/entities`, `/graph/entities/{id}`, `/graph/query`,
  `/graph/stats`, `/graph/reindex/{document_id}`. Tests incl. multi-hop retrieval of a document with no query words, acronym/entity
  resolution across documents, update-on-reupload, delete cleanup, tenant/ACL traversal isolation, hostile LLM extractor,
  extractor-failure recovery; also on real PostgreSQL. Test run found+fixed a real bug (delete left graph rows behind).
  `pytest`: 91 passing. Docs: docs/architecture/knowledge-graph.md.
- Phase 6 (COMPLETE): `app/agents/` (orchestrator, research, retrieval + knowledge-graph branches, analytical, report, verification,
  quality control, human review), `app/workflows/research_graph.py` (explicit StateGraph: conditional edges, `Send` parallel
  retrieval, RetryPolicy on network errors, bounded re-retrieval loop, `interrupt()` human review, AsyncSqliteSaver
  checkpoints), `app/workflows/runner.py` (run/stream/resume/inspect, conversations + messages persistence, checkpoint deletion),
  `app/security/injection.py`, grounded-answer + verification prompts with offline MockLLM handlers. Endpoints: `/chat`
  (JSON or SSE), `/agents/runs[/{id}[/resume]]`, `/conversations[/{id}[/messages]]`. Tested: grounded+cited answers, dynamic agent
  selection, parallel branches, loop caps 0/1/2, transient-retry and exhausted-retry failure, degradation on non-transient
  failure, fabricated/uncited claims rejected, LLM judge downgrade-only, weak-support qualification, prompt-injection (incl.
  hostile model), review approve/edit/reject + authorisation + resume after process restart, SSE ordering (only verified
  tokens), tenant/ACL/conversation isolation, owner revalidation, checkpoint erasure; also on real PostgreSQL.
  `pytest`: 106 passing. Docs: docs/architecture/langgraph-workflow.md; diagram generated by infrastructure/scripts/export_workflow_diagram.py.
- Phase 7 (COMPLETE): `app/memory/` (long-term preferences, session working set, research contexts with pinned docs + page-cited findings,
  document-interaction history; TTL + purge loop, per-(org,user) privacy incl. admins, limits, access re-validated on read, user deletion
  incl. conversations + checkpoints), research-context scoping of chat (`SearchFilters.document_ids`), preferences block in the answer
  prompt, `/memory/*` API; `app/reporting/` (async ReportService with progress + restart recovery, ReportBuilder: discovery, per-section
  verified claims, verbatim results table / contradiction candidates / stated gaps, final numeric guard, citation registry with pages,
  access-time redaction, exporters MD/DOCX/PDF via reportlab 5.1.0), `/reports` API. Property test: all quotes verbatim, pages match, all
  numbers sourced. Redaction verified in JSON + all 3 exports. Sample corpus + sample report generated by
  `infrastructure/scripts/generate_sample_report.py` (synthetic data, mock LLM, labelled). Also found+fixed: Request not imported in
  routes_kb (silent 422), relevance floor based on lexical rerank score was fragile (replaced by topic-term coverage). `pytest`: 126 passing
  (also from a clean venv). Docs: docs/architecture/memory-and-reports.md.

- Phase 8 (COMPLETE): `frontend/` React 19 + TypeScript (Vite 8): typed API client with zod validation of every response, fetch-based SSE parser, auth context
  (JWT in sessionStorage — XSS-readable trade-off documented in ADR-002/threat model), pages for login, dashboard, documents + source viewer, research chat
  (agent timeline, evidence panel, verification badges, human-review banner), knowledge explorer, runs + inspector, citation explorer, reports + export,
  evaluation, settings/memory, admin. `tsc --noEmit` clean, 56 Vitest tests, production build OK. Contract test parses the real evaluation output.
- Phase 9 (COMPLETE): `app/evaluation/` — metrics (P/R/MRR/nDCG, paired bootstrap, Wilcoxon+Holm, AUROC; unit-tested against hand-computed values), deterministic
  synthetic benchmark (24 docs, 122 questions in 6 classes, gold verified by `verify()`, dev/test split), harness for S1–S6 + end-to-end answers, abstention-gate
  calibration on dev (new setting `abstain_confidence_threshold`; chosen 0.4669, held on test with n=9 unanswerable — wide uncertainty), generated
  `docs/research/results.md`, `GET /evaluation/results|calibration`, Prometheus `/admin/metrics`, optional OpenTelemetry + LangSmith, `/admin/observability`.
  Real findings recorded honestly (see docs/evaluation.md): simple S3 beat S4/S5 overall; decomposition hurts comparison; classifier misses exploratory phrasings;
  mock LLM cannot compose multi-hop answers; H5 label degenerate (post-hoc analysis labelled).
- Phase 10 (COMPLETE): Dockerfiles (backend, frontend+nginx), `docker-compose.yml` (pgvector/Postgres, Redis, MinIO, API with migrations, 2 workers, UI), Alembic
  (initial migration generated on real PG, `alembic check` + downgrade tested), `AsyncPostgresSaver` checkpoints, Redis-shared rate limiter (fail-soft), `S3Storage`,
  atomic job claim + `ingestion_mode=external` + `python -m app.worker`, CI (backend, frontend, docker build), Makefile, threat model, deployment guide, API reference,
  ADR-004/005. `pytest`: 165 passing (0 skipped). A Docker-less end-to-end smoke test of the identical stack (prod mode, real PG/Redis/S3, 2 worker processes)
  passed; the container images themselves were NOT built here.

- Phase 11 (COMPLETE, extension phases 11-18 requested after the original 10): multimodal ingestion and retrieval — see docs/multimodal.md. `pytest`: 183 passing;
  frontend 58 passing. OCR is real tesseract (tested on generated images and an image-only PDF); vision captioning is tested with the mock model and mock HTTP transports
  only (no real VLM was called). Alembic 0002 adds `chunks.kind`.

- Phase 12 (COMPLETE): connector framework + Google Drive, SharePoint, Slack, Teams, SQL, REST (docs/connectors.md, ADR-006); Alembic 0003; admin UI page.
  `pytest`: 212 passing; frontend 59. Tested against FAKE source servers (and real PostgreSQL for SQL); NO real Google/Microsoft/Slack tenant was contacted.

- Phase 13 (COMPLETE): `app/agentic` (tools, planner, agent loop, sandbox), `/agentic/*`, Alembic 0004, UI page, docs/agentic.md, ADR-007. `pytest`: 261 passing; frontend 60.
- Phase 14 (COMPLETE): `app/llmops` (router, fallback + circuit breakers, per-org budgets, exact-match cache, prompt versions, metering), `/admin/llm/*`, Alembic 0005, docs/llmops.md, ADR-008. `pytest`: 277 passing. Tested with fake/mock models + real Redis only; no real provider called; no frontend view yet.
- Phase 15 (COMPLETE): `app/security/{pii,policy,auditsig}`, per-org policy enforced at ingestion/chat/LLM egress/answers, signed + exportable audit, `/admin/security/*`, Alembic 0006, shared `llmops/stack.py` (fixes: standalone worker now gets routing/metering/policy), docs/security/governance.md, ADR-009, threat-model rows 16-17. `pytest`: 317 passing. Regex-based PII only; no frontend view yet.
- Phase 16 (COMPLETE, statically validated only): `deploy/k8s` (Kustomize: api/worker/frontend, HPAs, PDBs, NetworkPolicies, migrate Job, optional KEDA), `infrastructure/scripts/validate_k8s.py` + 28 mutation tests, `app/distributed/{lease,bus}` (Redis leases, fan-out + at-least-once event bus with DLQ), cache invalidation across replicas, queue-depth gauge, worker heartbeat; fixed multi-replica bugs in connector single-flight / startup reaper. docs/deployment-kubernetes.md, ADR-010. `pytest`: see README. NEVER applied to a cluster; images not built; thresholds unmeasured.
- Phase 17 (COMPLETE): consented feedback with reasons, org retrieval-regression evaluation (cases, runs, paired compare), prompt A/B experiments (hash assignment pinned per run, Wilson CIs, Holm-adjusted tests, human conclusion), automated quality monitor (window tests, audit alerts, gauge), Alembic 0007, docs/learning.md, ADR-011. `pytest`: 374 passing. No UI; no alert channel; thresholds uncalibrated.
- Phase 18 (COMPLETE, statically validated; nothing run on GitHub/AWS): `infrastructure/scripts/backup.py` (pg_dump + manifest, verify, restore drill with count/revision/pgvector/index checks, tested on real PG incl. corruption detection), `release.py` (single version source `app/version.py`, drift check, single alembic head, expand-only migration lint, OpenAPI freshness, bump, notes), `/health/version`, workflows (ci, release, deploy, terraform) + `validate_workflows.py`, `deploy/terraform` (AWS) + `validate_terraform.py`, checkov 3.4.6: 193 passed/0 failed/8 justified skips, docs/releases.md, disaster-recovery.md, infrastructure-aws.md, operations.md, ADR-012. `pytest`: 491 passing. RPO/RTO are targets, not measurements; third-party actions are tag-pinned (warned); terraform fmt/validate/plan never run.
  LLM planning tested with a scripted planner + mock only; the code sandbox is layered but NOT a hard security boundary (default off).

## Outstanding / recommended next steps
- Build and run the Docker stack; fix any image-level issues; let the GitHub CI run once. Then run `release.yml` on a pre-release tag, `terraform plan` against a sandbox AWS account, and apply `deploy/k8s` to a test cluster: none of these have been run. Pin third-party actions to commit SHAs. Run the DR game day (docs/disaster-recovery.md) and replace the RPO/RTO targets with measurements.
- Re-run the evaluation with a REAL embedding model and LLM (`harness.run(settings=...)`), recalibrate the abstention gate, and only then draw conclusions about
  the adaptive policy. Consider a human-annotated benchmark on real documents and a second annotator.
- Calibrate the remaining heuristic gates (evidence sufficiency 0.35, completeness, review thresholds, report relevance floor, contradiction/gap cues).
- Improve the exploratory-query classifier without tuning on the test split; consider per-tenant quotas, MFA/SSO, TLS termination, dependency scanning (image signing and Trivy scanning are defined in release.yml but unrun).

## Known issues / caveats
- ALL BUT ONE HEURISTIC GATE ARE UNCALIBRATED (see README). Confidence is backend-sensitive (SQLite BM25 vs PostgreSQL FTS).
- Mock-component results validate plumbing only; never report them as quality results. The benchmark is synthetic and not human-annotated.
- Reports are not versioned; in-flight reports fail on restart; no charts; PDF is simple; non-Latin text needs a TTF font (DejaVu auto-detected).
- Verification is lexical + optional LLM judge. Graph extraction recall unmeasured. PDF tables come from pdfplumber ruled-table detection (borderless tables are missed; text is also indexed in flowing form, so it is duplicated). OCR quality on real scans/handwriting unmeasured. Real VLM captions untested. Scanned-PDF OCR hook, sentence-transformers embedder and
  cross-encoder reranker are untested. PG tests use tiny corpora (no recall/latency at scale).
- Memory values are plain JSON in the DB (do not store secrets). The vector dimension is a schema constant (ADR-004).
- Operational gotchas found while building: do not use `pkill -f` with a pattern in the same shell command; never replay recorded shell history that contains its own replay
  command; `pgserver` is installed as `pixeltable-pgserver` (module `pixeltable_pgserver`) on current Pythons.

## Publishing to GitHub
`git init` was done and files are staged but NOT committed (no git identity). Run: `git config user.name "…" && git config user.email "…" && git commit -m "Initial commit"`,
replace `<your-repo-url>` in README, push, and watch the first CI run.
