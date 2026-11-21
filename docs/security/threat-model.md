# Threat model

Scope: the NEXUS AI API, workers, stores (PostgreSQL, object storage, Redis) and web UI as deployed by `docker-compose.yml`.
Method: STRIDE over trust boundaries, with each mitigation tied to code and, where it exists, a test. **Residual risk is stated, not hidden.**
This is a design-level review by the authors, not a penetration test or third-party audit.

## Assets
Tenant documents and derived data (chunks, embeddings, graph), conversations/memory/reports, credentials (passwords, JWTs, LLM/embedding API keys,
S3 keys), audit logs, availability of the service, integrity of answers (users act on them).

## Trust boundaries and actors
Browser ⇄ nginx/API (untrusted internet) · API ⇄ PostgreSQL/Redis/object store (trusted network) · API ⇄ external LLM/embedding providers (third party) ·
**uploaded documents and retrieved text are untrusted input** · tenants are mutually untrusted · org admins are trusted only within their organisation.

## Threats and mitigations

| # | Threat (STRIDE) | Mitigation (where) | Tests | Residual risk |
|---|---|---|---|---|
| 1 | **Spoofing** — credential stuffing / brute force | bcrypt (SHA-256 pre-hash, no 72-byte truncation), min length, login rate limit (Redis-shared across replicas, per-replica fallback) (`core/security.py`, `core/ratelimit.py`) | test_foundation, test_phase10 | No MFA/SSO, no account lockout or breached-password check. Rate limit is per source IP behind a proxy that must set `X-Forwarded-For` correctly |
| 2 | **Spoofing** — forged/stolen token | HS256 JWT, expiry, secret ≥ 32 chars enforced in prod, role and active flag **re-read from the DB on every request** (revocation is immediate for role/disable) | test_foundation, test_admin_feedback | JWT itself is not revocable before expiry (60 min) except by disabling the user; UI keeps the token in `sessionStorage` (readable by XSS; chosen over cookies to avoid CSRF — see ADR-002) |
| 3 | **Tampering/Info disclosure** — cross-tenant or ACL bypass in retrieval | Scope (org, collection ACL, current/as-of version) is part of the SQL query before ranking, for dense, sparse, graph and report paths; hidden resources → 404 (`retrieval/scope.py`, `auth/access.py`) | isolation tests across search, advanced plans, graph, chat, memory, reports, exports | Correct only while every new query goes through `build_scope`; no Postgres row-level security as defence in depth |
| 4 | **Tampering** — malicious upload (zip bomb, polyglot, path traversal, oversized) | Size cap, extension allow-list + magic-byte check, zip-bomb guard, sanitised filenames, storage-key traversal guard (local and S3), parsing in worker threads (`ingestion/validation.py`, `services/storage.py`) | test_document_processing, test_phase10 | No antivirus/malware scan; parser libraries (pypdf, python-docx, …) may have their own vulnerabilities — keep pinned versions updated; parsing is not sandboxed |
| 5 | **Tampering** — **prompt injection** via document text or hostile model output | Instruction patterns neutralised before prompting (`security/injection.py`); evidence compression is *index-only* (the LLM can only pick source sentence indices); answers are claim-level verified against retrieved text and uncited/unsupported claims are dropped; LLM judge may only *downgrade* | test_workflow (hostile documents and hostile model), test_llm_and_components | Heuristics are not a proof: a determined attacker may still steer *which* true passages are cited or exploit paraphrase-level manipulation; the model never executes tools, which limits blast radius |
| 6 | **Repudiation** — untraceable admin/data actions | Append-only audit log (login, upload, delete, search query *hashes*, memory, reports, role changes), request IDs on every response/log (`auth/service.audit`, `/admin/audit`) | test_admin_feedback | Audit rows are in the same database (an attacker with DB write can alter them); no external log shipping/WORM storage |
| 7 | **Information disclosure** — secrets leakage | Config only from env; `.env` ignored by git; no secrets in logs (query text is hashed); prod refuses default secret; compose requires `NEXUS_JWT_SECRET` | test_foundation | Compose defaults for Postgres/MinIO passwords are placeholders: **change them**. No secret manager integration; memory values are plain JSON (do not store secrets) |
| 8 | **Information disclosure** — data sent to third-party LLM/embedding APIs | Offline mock is the default; providers are opt-in per deployment; evidence is limited to the user's authorised passages | — | Using an external provider discloses retrieved text to it; legal/contractual review is the deployer's responsibility |
| 9 | **Information disclosure** — memory/privacy | Memory is per (org, user), invisible even to admins; TTL purge; user-controlled deletion including conversations and checkpoints; reports re-validated and redacted when access is revoked | test_memory, test_reports | Data is not encrypted at the application layer (rely on disk/DB encryption) |
| 10 | **Denial of service** — request flooding, huge uploads, expensive queries | Global rate limit, upload size cap (also nginx `client_max_body_size`), bounded loops (`max_retrieval_loops`), bounded graph traversal, caps on claims/passages/memory items, worker concurrency limits | test_foundation, test_pipeline, test_memory | No per-tenant quotas or cost budgets; an authenticated user can still run many expensive `/chat` calls up to the rate limit; no request timeout/circuit breaker around LLM calls beyond the provider client's |
| 11 | **Denial of service** — job poison / crash loop | Retries with exponential backoff, dead-lettering, stuck-job recovery, atomic claims so a job never runs twice (`ingestion/service.py`, `worker.py`) | test_api_ingestion, test_phase10 | A hung parser thread can occupy a worker slot until recovery |
| 12 | **Elevation of privilege** — role abuse, last-admin lockout | Roles checked server-side per request; admin endpoints scoped to own org; cannot demote/disable the last admin; reviewer identity set by the server in human review | test_admin_feedback, test_workflow | Single "admin" role is coarse |
| 13 | **Tampering** — web attacks on the UI | Strict CSP (`script-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, no `dangerouslySetInnerHTML`, answers rendered as text, zod-validated API responses, same-origin proxy (no CORS) | frontend tests | CSP allows inline *styles*; no automated dependency scanning is configured yet |
| 14 | **Integrity of answers** — hallucination/over-confidence | Claim-level verification, citations with page + quote, answer status, abstention gate (calibrated on dev, see `docs/research/results.md`), human-review interrupt | evaluation harness | Gates are calibrated only on a synthetic mock-component benchmark — **recalibrate per deployment**; verification is lexical + optional LLM judge |
| 15 | **Supply chain** — dependencies and images | All Python and npm dependencies pinned; Docker base images pinned by tag | CI | No hash-pinning, SBOM, signed images, or Dependabot config yet; `pgvector/pgvector:pg16`, `redis:7-alpine`, `nginx:1.27-alpine` are tag-pinned only |
| 16 | **Sensitive data exposure / leakage to model providers** | Per-org PII + secret policy: redact or reject at ingestion, redact queries, outbound prompts and answers; detected values never logged; scan dry-run endpoint | test_security | Pattern-based detectors (no names/addresses); coverage differs by endpoint — see docs/security/governance.md |
| 17 | **Repudiation / audit tampering** | HMAC-signed audit rows, verify and export endpoints, policy changes audited with diffs | test_security | Edits detected, deletions are not; secret holders can re-sign |

## Deployment hardening checklist (not automated)
1. Terminate TLS in front of nginx (the compose stack serves HTTP). 2. Change all default passwords; put secrets in a secret manager.
3. Restrict network exposure: only `frontend:8080` should be reachable; Postgres/Redis/MinIO stay internal.
4. Enable disk/DB encryption and backups (Postgres volume, MinIO bucket). 5. Ship logs and `/admin/metrics` to your monitoring.
6. Run `alembic upgrade head` from one service only (compose does). 7. Review the LLM provider's data-handling terms.
8. Rotate `NEXUS_JWT_SECRET` to invalidate all sessions.

## Not covered
Physical/host security, insider threats at the infrastructure level, side channels, compliance regimes (GDPR/HIPAA/SOC 2 — no claim is made),
and security of the LLM provider.
