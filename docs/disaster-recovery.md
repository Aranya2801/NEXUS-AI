# Backup and disaster recovery

## Objectives (targets, not measurements)
| Scenario | Target RPO | Target RTO | Mechanism |
|---|---|---|---|
| Bad deploy | 0 | minutes | `kubectl rollout undo` (workloads); schema is expand-only |
| Operator/app error deletes or corrupts data | ≤ 5 min | 1–4 h | RDS point-in-time restore (14–35 day window) into a new instance |
| Document object deleted/overwritten | 0 | minutes | S3 versioning (noncurrent versions kept 90 days by default) |
| AZ failure | ~0 | 1–2 min | RDS Multi-AZ failover, ElastiCache Multi-AZ, nodes in 3 AZs |
| Region loss | ≤ 24 h (DB), minutes (objects) | 4–8 h | AWS Backup copy to the DR region + S3 cross-region replication, then rebuild with Terraform |
| Total database loss incl. instance deletion | ≤ 24 h | 1–4 h | AWS Backup recovery point (daily 35 d, weekly 1 y) |

These numbers follow from the configuration (PITR granularity, daily backup schedule, replication behaviour). **They are design targets. No failover or restore has been timed against a production-sized dataset.**
Run the game day below and replace them with what you measure.

## What protects what
- **PostgreSQL (all relational data, vectors, graph, audit log, checkpoints):** RDS automated backups + PITR; AWS Backup daily/weekly recovery points in a KMS-encrypted vault, copied cross-region when `enable_dr = true`; deletion protection and a final snapshot.
- **Document blobs (S3):** versioning, SSE-KMS, noncurrent-version retention, cross-region replication with delete markers *not* replicated (a delete in the primary does not erase the DR copy).
- **Redis:** holds no durable business data (rate-limit counters, leases, event streams). Losing it loses in-flight events and pending-ack bookkeeping; the Postgres-backed job table makes ingestion and connector syncs recoverable. Not backed up beyond ElastiCache snapshots.
- **Secrets:** the RDS master secret and the Redis AUTH token live in Secrets Manager (KMS-encrypted); the JWT signing key / connector encryption key are application secrets you must back up in your secret store. **Losing the connector key makes stored connector credentials unreadable; losing the JWT key invalidates sessions and breaks audit-signature verification for existing rows.**
- **Infrastructure:** Terraform in git; remote state in a versioned S3 bucket (create it out of band; versioning on, access restricted).

## Logical backup and restore drill (`infrastructure/scripts/backup.py`)
A portable `pg_dump` (custom format) plus a manifest (sha256, server version, Alembic revision, per-table row counts, optional content fingerprints). It complements, not replaces, PITR.
```
python infrastructure/scripts/backup.py backup --out /backups --dsn "$NEXUS_DATABASE_URL" [--deep] [--storage-dir ./storage]
python infrastructure/scripts/backup.py verify --manifest /backups/nexus-<ts>.manifest.json
python infrastructure/scripts/backup.py drill  --manifest /backups/nexus-<ts>.manifest.json --admin-dsn postgresql://admin@host/postgres
```
`drill` restores into a scratch database, compares row counts (and fingerprints with `--deep`) and the Alembic revision, checks that pgvector is installed, that a vector search returns rows and that the FTS/HNSW indexes exist, then drops the scratch database; exit code 1 on any mismatch,
2 on environment errors. Passwords travel in `PGPASSWORD`, never argv. The test-suite runs backup → verify → drill against a real PostgreSQL+pgvector and proves corruption (flipped byte) and row-count mismatches are detected.
Schedule `backup` + `drill` weekly (a CronJob or CI schedule) and alert on a non-zero exit; **an untested backup is a hope, not a backup**. Limits: logical dumps are slow and lock-light but not instant on very large databases, and they
are point-in-time only at the moment they started.

## Runbooks
### A. Restore the database to a point in time (data corruption / accidental deletion)
1. Declare the incident; pause writers: `kubectl -n nexus scale deploy/api deploy/worker --replicas=0`.
2. Choose the target time (just before the damage; audit-log timestamps help).
3. `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier nexus-<env> --target-db-instance-identifier nexus-<env>-restore --restore-time <UTC ISO> --db-subnet-group-name nexus-<env> --vpc-security-group-ids <db sg> --no-multi-az` (add Multi-AZ later).
4. Wait for `available`; connect and run `python infrastructure/scripts/backup.py backup` against it, or inspect directly: row counts, `SELECT version_num FROM alembic_version`, a vector query.
5. Repoint: update `NEXUS_DATABASE_URL` in the secret (or swap the instance identifiers through Terraform import) and scale the workloads back up. Keep the damaged instance until the post-mortem is done.
6. Verify: `/health/ready`, a search, `GET /admin/security/audit/verify` (row signatures; restored rows verify, rows written after the restore point are simply absent).

### B. Restore from an AWS Backup recovery point (instance gone / PITR window exceeded)
`aws backup list-recovery-points-by-backup-vault …` → `aws backup start-restore-job` into a new instance → steps 4–6 above. For a DR-region restore use the `-dr` vault.

### C. Restore from a logical dump
Create an empty database with the `vector` extension available, `pg_restore --no-owner --no-privileges --exit-on-error --dbname <new> <dump>`, then run `drill`-style checks, then repoint as in A5.

### D. Recover documents in S3
Accidental delete creates a delete marker: remove the marker (`aws s3api delete-object --version-id <marker>`) to undelete. Overwrite: copy the previous version over the current key. After noncurrent retention expires only the DR copy remains.
The database stores a content hash per document version, so a restored object can be verified against it. Re-ingest if the vectors need rebuilding.

### E. Region failover (manual, rehearsed rather than automatic)
1. Confirm the primary region is unavailable and not recoverable within the RTO.
2. In the DR region: apply Terraform with `region = <dr>`, a fresh `environments/<env>.tfvars` and the latest recovery point restored into RDS (run B in the DR region); point the app at the replicated S3 bucket (`NEXUS_S3_BUCKET`).
3. Deploy the same signed version with `deploy.yml` (GHCR images are region-independent). Redis starts empty (fine).
4. Switch DNS; verify with the smoke test; announce the data-loss window (time between the last copied recovery point and the incident).
5. Fail back later by repeating the process in the opposite direction; do not run two writable primaries.
This path involves several manual steps and has never been executed end to end; the first rehearsal will find gaps. Budget for that.

### F. Secrets
Redis AUTH token rotation: `terraform apply -replace=random_password.redis` then restart the API/worker (ElastiCache applies the new token; expect a brief auth failure window). RDS master password: rotate in Secrets Manager (managed by RDS). JWT/connector keys: see docs/security; rotating them invalidates sessions (JWT) or requires re-entering connector credentials (connector key).

## Game-day checklist (quarterly)
- [ ] `backup.py drill` on a recent production backup; record `restore_seconds`, backup size, row counts.
- [ ] PITR restore into a scratch instance; time it; run the verification queries.
- [ ] Trigger an RDS Multi-AZ failover in staging (`aws rds reboot-db-instance --force-failover`); watch `/health/ready` and error rate; record the outage.
- [ ] Undelete an S3 object and restore an overwritten one.
- [ ] Deploy a deliberately broken image to staging; confirm the smoke test fails and the rollback restores service.
- [ ] Walk runbook E in a throwaway DR-region stack; update this document with what was wrong.
- [ ] Confirm alarms (`backup_failed`, DB CPU/storage) reach a human.
- [ ] Update the objectives table with *measured* values and the date.
