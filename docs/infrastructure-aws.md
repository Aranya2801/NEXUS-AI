# AWS infrastructure (Terraform)

`deploy/terraform` defines one environment per state key (`nexus/<environment>.tfstate`), parameterised by `environments/<env>.tfvars`.

| File | Contents |
|---|---|
| `network.tf` | VPC, 2–3 AZs, public/private subnets, one NAT per AZ, locked-down default SG, VPC flow logs (365 d, KMS) |
| `eks.tf` | EKS (API auth mode, secrets envelope-encrypted, all control-plane logs, IMDSv2-only nodes with hop limit 1), managed node group, OIDC provider, IRSA role for the app limited to its bucket + key |
| `data.tf` | RDS PostgreSQL 16 (encrypted, `rds.force_ssl`, RDS-managed master secret, PITR, deletion protection, final snapshot, Performance Insights, Multi-AZ by variable), ElastiCache Redis (TLS, AUTH, at-rest KMS, Multi-AZ by variable) |
| `storage.tf` | Documents bucket (versioning, SSE-KMS, public access blocked, TLS-only policy, noncurrent expiry); optional DR bucket + cross-region replication |
| `backup.tf` | AWS Backup vault/plan (daily 35 d, weekly 1 y), optional copy to the DR region |
| `github_oidc.tf` | GitHub OIDC provider; deploy role trusted for **exactly** `repo:<owner/name>:environment:<env>`, EKS access entry scoped to namespace `nexus` |
| `alarms.tf` | SNS topic; DB CPU, DB free storage, failed backup jobs |
| `kms.tf` | One rotating CMK per environment (+ DR key) |

## First-time setup
1. Create the state bucket (versioned, encrypted, private) and lock table out of band; put their names in the GitHub Environment variables.
2. Edit `environments/*.tfvars` (`github_repository`, `eks_public_access_cidrs` — placeholders are marked `CHANGE-ME`; validation rejects `0.0.0.0/0`).
3. `terraform init -backend-config=...` then `plan`/`apply` (locally or via `terraform.yml`). The Terraform role used by CI (`AWS_TERRAFORM_ROLE_ARN`) is **not** created here (chicken-and-egg): bootstrap it once by hand with the permissions you are comfortable delegating.
4. Install in-cluster add-ons Terraform does not manage: AWS Load Balancer Controller (ingress), metrics-server (HPA), optionally cluster-autoscaler/Karpenter and KEDA. Annotate the `nexus` ServiceAccount with the `app_role_arn` output and create `nexus-secrets` from Secrets Manager (External Secrets or by hand).
5. Images live on GHCR; private GHCR needs an image-pull secret in the namespace (not defined here).

## Validation performed — and not performed
- **Independent scanner:** `checkov` 3.x over `deploy/terraform`: 193 checks passed, 0 failed, 8 skipped. Every skip is an inline `#checkov:skip=ID:reason` with a written justification (the validator rejects unjustified skips). Re-run: `checkov -d deploy/terraform --framework terraform`.
- **Repository policy checks** (`validate_terraform.py`, mutation-tested): encryption, public exposure, backups, IAM scoping, OIDC trust shape, DR gating, variable hygiene, tfvars guards for production, alignment style.
- **Never run:** `terraform fmt/validate/plan/apply` (the binary was unavailable in the build sandbox; CI runs fmt/validate). Provider-schema errors, attribute typos the parser cannot see, quota and region/AZ capacity issues, and IAM propagation problems are therefore **unverified**. Expect a first `plan` to need small fixes.
- No cost estimate was made. NAT gateways per AZ, Multi-AZ RDS, and cross-region copies are the main recurring costs; use the staging tfvars (single-AZ DB, 2 AZs, no DR) for experiments.

## Known gaps
- S3 server-access logging / CloudTrail data events are not configured (skip documented in `storage.tf`).
- The EKS API endpoint is public but CIDR-restricted so hosted runners can deploy; use VPC-internal runners and disable it if policy requires.
- No WAF in front of the ingress, no GuardDuty/Security Hub/Config (account-level services; enable them at organisation level).
- Redis AUTH rotation is a manual two-step (see disaster-recovery.md F).
- No Terraform module split or multi-account layout; this is a single root module intended as a starting point.
