# Releases and CI/CD

## Flow
1. Work lands on `main` through PRs. **CI** (`.github/workflows/ci.yml`) runs the backend suite (SQLite + real PostgreSQL/pgvector + Redis + moto S3), the frontend checks, and a `static-checks` job:
   Kubernetes validator, workflow validator, Terraform policy checks, and `release.py check`.
2. To release: write the notes under `## [Unreleased]` in `CHANGELOG.md`, run `python infrastructure/scripts/release.py bump X.Y.Z` (updates `backend/app/version.py`, `frontend/package.json`, every kustomize image tag and rolls the CHANGELOG), regenerate
   OpenAPI (`python infrastructure/scripts/export_openapi.py`), merge, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. **Release** (`release.yml`, tag-triggered) re-verifies (tag == code version, `release.py check`, full backend suite), builds `api` and `frontend` images, pushes them to GHCR with provenance and an SBOM, scans them with Trivy
   (fixable HIGH/CRITICAL fails the release), signs each image **by digest** with cosign keyless, and creates the GitHub Release from the CHANGELOG section.
4. **Deploy** (`deploy.yml`, manual) takes `environment` + `version`. It runs in a GitHub Environment (add required reviewers for production), authenticates to AWS by OIDC, verifies the cosign signature
   against this repository's release workflow identity, runs the migration Job, rolls out, smoke-tests `/api/health/version` + `/health/ready`, and on failure runs `kubectl rollout undo` for the three workloads.
5. **Terraform** (`terraform.yml`): PRs get `fmt`/`validate` + the policy checks; `plan`/`apply` are manual and environment-gated, and apply only ever applies a plan saved in the same run.

Required GitHub Environment variables (per environment): `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `AWS_TERRAFORM_ROLE_ARN`, `EKS_CLUSTER`, `REGISTRY` (e.g. `ghcr.io/<owner>/<repo>`), `PUBLIC_URL`, `TF_STATE_BUCKET`, `TF_STATE_LOCK_TABLE`.
No long-lived cloud credentials are stored in GitHub.

## Database migrations: expand, then contract
During a rollout the old and new application versions run against one schema, and a rollback runs the old version against the new schema. So `upgrade()` must be **additive**: new tables, nullable or defaulted columns, new indexes.
`release.py lint-migrations` (part of `check`, run in CI and on release) rejects in `upgrade()`: `drop_table/drop_column/drop_constraint`, renames, type changes, `NOT NULL` without a default, destructive SQL and non-literal `op.execute`.
Removing something is a *later* release ("contract") once no deployed version reads it, and the statement must carry `# release: destructive-approved` with a reason. `downgrade()` is not linted and is not used by the pipeline:
**rollback means redeploying the previous version, never running `alembic downgrade` on production data.** If a migration cannot be made additive, restore from backup is the rollback (see `docs/disaster-recovery.md`).

## Versioning
SemVer. `backend/app/version.py` is the single source; `release.py check` fails on drift in `frontend/package.json`, every kustomize overlay and the migrate Job, on a missing dated CHANGELOG section, on multiple Alembic heads, and on a stale `docs/api/openapi.json`.
`GET /health/version` returns the running version (used by the deploy smoke test).

## What has and has not been exercised
Exercised in this repository's test-suite: the release script (including bump and drift detection), the migration lint (mutation-tested; all shipped migrations pass), the workflow validator (mutation-tested), cross-checks that
workflows reference real deployment names/files/endpoints, and the Terraform policy checks. **Not exercised: the workflows have never run on GitHub, and nothing was applied to a cluster or an AWS account.**
Known limits you should treat as to-do items:
- Third-party actions (`aws-actions/configure-aws-credentials`, `hashicorp/setup-terraform`, `sigstore/cosign-installer`, `aquasecurity/trivy-action`) are pinned by **tag**, which is mutable. The validator warns on each; pin them to commit SHAs (Dependabot or `pinact`) before go-live.
  Tag existence could not be verified from the build sandbox.
- `release.yml` builds `frontend/Dockerfile.unprivileged`; the `api` image uses `backend/Dockerfile`. Images are single-architecture (the runner's).
- The migration Job and the rollout are separate steps: if the Job succeeds and the rollout then fails, the schema stays migrated (by design: expand-only).
