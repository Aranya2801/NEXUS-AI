# Operations runbook (index)

| Situation | Where |
|---|---|
| Ship a release / roll back | `docs/releases.md` (rollback = redeploy previous version; never `alembic downgrade` in production) |
| Database damaged, instance lost, region lost, S3 undelete, secret rotation | `docs/disaster-recovery.md` (runbooks A–F, game-day checklist) |
| Kubernetes layout, probes, autoscaling, NetworkPolicy | `docs/deployment-kubernetes.md` |
| Cloud resources | `docs/infrastructure-aws.md` |
| LLM budget exhausted / provider outage / bad prompt version | `docs/llmops.md` (budgets, breakers, prompt rollback to `builtin`) |
| PII/policy questions, audit export and verification | `docs/security/governance.md` |
| Quality regression alert, experiments | `docs/learning.md` |

## Quick checks
- Which version is live: `curl https://<host>/api/health/version`.
- Is it healthy: `/api/health/ready` (database reachable; worker count) and the Prometheus gauges `nexus_ingestion_queue_depth`, `nexus_quality_regressions_active`.
- Ingestion stuck: `nexus_ingestion_queue_depth{status="dead"}` > 0 means jobs exhausted retries; inspect `ingestion_jobs.error` (`GET /admin/jobs`), fix the cause and re-upload the document (there is no re-queue endpoint yet).
- Roll back a bad deploy by hand: `kubectl -n nexus rollout undo deploy/api deploy/worker deploy/frontend`.
