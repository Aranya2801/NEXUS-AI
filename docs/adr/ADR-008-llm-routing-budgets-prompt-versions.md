# ADR-008: LLM routing, budgets and prompt versioning

**Status:** accepted (Phase 14)

## Context
Earlier phases called one configured model directly. Production needs fallback when a provider fails, cost control per tenant, and the ability to change
prompts without a deploy – without touching the many call sites and without creating new data-leak paths.

## Decisions
1. **Decorator over the `LLM` protocol** (`RoutingLLM`), not a new interface. All call sites are unchanged; `llm.name` stays a string (single-model setups keep the
   model's own name).
2. **Task from the prompt's first line** (`TASK: x`) rather than a new parameter – it already existed and the offline mock depends on it. Overrides must keep it.
3. **Attribution by ContextVar** set in `get_principal` and at job start, so deep workflow calls are metered to a tenant without threading a principal through.
4. **Fallback only on provider errors**; configuration and budget errors never fall back. Per-model circuit breaker (threshold/window/cooldown, single half-open probe).
5. **Budget = degrade then cap**, per org per UTC day, plus an HTTP-level guard on LLM-spending endpoints.
6. **Exact-match, tenant-keyed cache; no semantic cache.** A shared semantic cache lets one tenant infer another's queries via latency/hit behaviour. Default off.
7. **Metering stores hashes and counts, never text.** Rows are buffered and batch-written so the hot path never awaits the database.
8. **Prompt versions are org-scoped, numbered and immutable; `builtin` is the rollback target.** Activation is a pointer flip, audited.

## Consequences
* Cost/budget figures are estimates and per-replica approximate (documented).
* A bad prompt override can degrade answer quality; validation checks structure only. Evaluation-gated rollout is deferred to Phase 17.
* Failures inside runs are swallowed by existing graceful-degradation code, which is why the 429 is enforced at the endpoint.
