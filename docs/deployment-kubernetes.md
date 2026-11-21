# Kubernetes deployment (Phase 16)

Manifests live in `deploy/k8s` (Kustomize): `base/` (api, worker, frontend, config, ingress, HPAs, PDBs, NetworkPolicies), `migrate/` (schema Job), `optional/` (KEDA), `overlays/example/`.

> **Verification status — read this.** There is no cluster, `kubectl`, `kustomize` or Docker daemon in the build environment. The manifests are checked **statically**
> by `infrastructure/scripts/validate_k8s.py` (`make k8s-validate`, also run in `pytest`): wiring (selectors, ports, config/secret references, HPA/PDB/Ingress targets), hardening
> rules, pinned images, and that every `ConfigMap` key is a real `NEXUS_` setting. The validator is itself tested by mutation (28 injected regressions must each be caught).
> **They have never been applied to a cluster.** Run `kubectl apply -k … --dry-run=server` (or `kubeconform`) first; expect to adjust ingress class, storage and network policy.

## Topology
```
Ingress ─▶ frontend (nginx-unprivileged :8080) ─/api─▶ Service api :8000 ─▶ api pods (≥2, HPA 2–10)
                                                         worker pods (≥2, HPA 2–8)  ── claim jobs atomically from Postgres
                          external, managed:  PostgreSQL+pgvector · Redis · S3-compatible storage
```
Postgres, Redis and object storage are **not** deployed by these manifests – use managed services (or your operator of choice). **Redis is required once you run more than one API replica**: it backs the
shared rate limiter, the cross-replica connector/housekeeping leases, and the event bus that propagates policy/prompt changes.

## First deploy
1. Build and push images: `backend/Dockerfile` → `…/nexus/api:<tag>` (also used by the worker and the migration Job); `frontend/Dockerfile.unprivileged` → `…/nexus/frontend:<tag>`.
2. Copy `overlays/example`, set registry/tag/host. Create the `nexus-secrets` Secret out of band (`base/secret.example.yaml` documents the keys; it is deliberately not part of the kustomization).
3. Create the namespace and secret, run the migration Job, then apply the overlay:
   ```bash
   kubectl apply -f deploy/k8s/base/namespace.yaml
   kubectl -n nexus create secret generic nexus-secrets --from-env-file=prod.secrets.env
   kubectl -n nexus delete job nexus-migrate --ignore-not-found && kubectl apply -k deploy/k8s/migrate
   kubectl -n nexus wait --for=condition=complete job/nexus-migrate --timeout=300s
   kubectl apply -k deploy/k8s/overlays/<yours>
   ```

## Fault tolerance — what is actually in place
* **Rollouts**: `maxUnavailable: 0` for API/frontend; readiness gates traffic (`/health/ready` checks the database); startup probe tolerates slow boots; `preStop` sleep + 60 s grace for in-flight requests and SSE streams.
* **Workers**: jobs are claimed with an atomic compare-and-set, retried with backoff, dead-lettered after `max_attempts`; a worker killed mid-job leaves it `running` and `recover_stuck` re-queues it after 15 min.
  SIGTERM stops polling and lets the current batch finish (120 s grace). A heartbeat file written from the event loop drives the liveness probe.
* **Disruptions**: PodDisruptionBudgets (`minAvailable: 1`), topology spread across nodes.
* **Multi-replica correctness** (found and fixed while writing this phase): connector syncs and the memory-purge loop were guarded only by in-process locks, so two API replicas would have run them concurrently, and a
  restarting replica marked another replica's live sync as "interrupted". They now use an expiring **Redis lease** (fail-closed: no Redis → the guarded work is skipped, never duplicated), and the startup reaper
  skips runs whose lease is still held.
* **Cache coherence**: policy and prompt-override caches are invalidated on all replicas by an event (Redis Streams fan-out) published after the change is committed; the 10 s TTL remains the fallback.
* **Event bus** (`app/distributed/bus.py`): fan-out mode (best-effort) and consumer-group mode (at-least-once, ack, redelivery of entries held by a dead consumer, dead-letter stream after N attempts) — tested against a real Redis.
  No built-in feature consumes group mode yet; it is infrastructure for follow-up work (e.g. webhooks).

## Autoscaling
* API: CPU 70 %, slow scale-down (long SSE streams). Workers: CPU 75 % (ingestion is CPU/memory-bound), scale-down stabilisation 10 min so a burst's tail is not killed mid-document.
* The API exports `nexus_ingestion_queue_depth{status}` (queued/running/dead) at `/admin/metrics`; `optional/keda-worker.yaml` scales on it. **Untested**: needs KEDA and a Prometheus scrape job with an admin token.
* Thresholds are starting points, **not measured**: no load test has been run. Tune from real traffic.

## Known gaps
* No egress NetworkPolicy (differs per cluster), no service mesh/mTLS, no PodSecurity admission config beyond the namespace label, no Helm chart (cannot be rendered or tested here — Kustomize was chosen for that reason).
* Budget accounting (Phase 14) is still per-replica; the live-run counter in `agent_runs` is not distributed.
* The Python code-execution sandbox is not a security boundary; keep `NEXUS_AGENTIC_CODE_EXECUTION_ENABLED=false` on shared clusters.
* `frontend/Dockerfile.unprivileged` has not been built (no Docker daemon).
