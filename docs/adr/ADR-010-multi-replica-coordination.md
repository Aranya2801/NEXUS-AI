# ADR-010: Multi-replica coordination (leases, event bus) and Kubernetes packaging

**Status:** accepted (Phase 16)

## Context
Phases 1–15 were correct for one API process. Horizontal scaling exposed three single-process assumptions: per-process `asyncio.Lock` single-flight for connector syncs, a startup reaper that failed *all* `running`
connector runs, and per-process caches (policy, prompts) that stay stale on other replicas.

## Decisions
1. **Leases, not leader election.** Work that must not overlap takes a short expiring lease keyed by the work item (`connector-sync:<id>`), renewed by a background task. Any replica may do the work; none may duplicate it.
2. **Fail closed.** If Redis cannot be reached the lease is *not* granted (work is skipped and retried), and `is_held` reports *held* so reapers never kill live work on a blip.
3. **Single process keeps working without Redis** (`MemoryLease`, `MemoryBus`): local development and small installs are unchanged.
4. **Events are for coherence and notification, not for correctness.** Fan-out is best-effort and every cache keeps its TTL; at-least-once group delivery exists for handlers that are idempotent. Events carry ids only.
5. **Publish after commit** so subscribers re-reading the database see the change.
6. **Kustomize, not Helm**: it can be validated statically with the tooling available here; Helm templates could not be rendered or tested. Secrets are never in the kustomization.
7. **Static validation as a gate** (`validate_k8s.py` + mutation tests) instead of unverifiable claims; images are pinned, pods run restricted-profile.

## Consequences
Redis becomes a hard dependency for multi-replica deployments. The manifests are statically validated only. Autoscaling thresholds are unmeasured.
