# ADR-012: Release engineering, cloud infrastructure and disaster recovery

**Status:** accepted (Phase 18)

## Decisions
1. **Migrations are expand-only in `upgrade()`, enforced by a lint in CI.** Rolling deploys and rollbacks both mix versions and schemas; additive changes make the workload rollback a plain `rollout undo`. Contract steps need a written approval marker and a later release. We do not run `alembic downgrade` in production.
2. **One version source** (`backend/app/version.py`) with a script that fails CI on drift, instead of hand-edited tags across five files.
3. **Tag-triggered releases build once, sign by digest, deploy by version.** Deploys verify the cosign signature against this repository's workflow identity before touching the cluster, so a pushed-but-unreleased image cannot be deployed.
4. **No stored cloud credentials.** GitHub OIDC with trust pinned to repository *and* environment; the deploy role can only describe the cluster and edits the `nexus` namespace via an EKS access entry.
5. **Workflow policy is code** (`validate_workflows.py`): least-privilege tokens, timeouts, no `pull_request_target`, no untrusted expressions in `run:`, environments on anything touching a cloud account. Third-party actions are tag-pinned with a warning because commit SHAs could not be verified at build time.
6. **Terraform as a single reviewed root module**, validated three ways (repository policy checks, checkov, CI `terraform validate`), with every scanner skip justified inline. Raw resources rather than community modules to avoid unverified version pins.
7. **Backups are only real if restored**: a script performs a full restore drill (counts, revision, pgvector search, indexes) and detects corruption. RPO/RTO are published as targets with a game-day procedure to replace them with measurements.
8. **Managed secrets, no passwords in code or state**: RDS-managed master credential; Redis AUTH token in Secrets Manager.

## Consequences / honest limits
Workflows, Terraform and manifests have not been executed against real GitHub/AWS/Kubernetes; the first runs will surface defects. The expand-only rule costs an extra release for destructive schema changes. Single-architecture images, a CIDR-restricted public API endpoint, and missing S3 access logging are accepted gaps (see docs).
