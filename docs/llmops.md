# LLMOps (Phase 14)

Every LLM call in NEXUS goes through `RoutingLLM` (`backend/app/llmops/router.py`), which implements the same `LLM` protocol as the
provider adapters, so no call site changed. Per call it does, in order:

1. **Task + prompt version** – the task comes from the `TASK: <name>` first line of the system prompt. If the org has an *active override* for that
   task it replaces the built-in text. The version label and a SHA-256 prefix of the text actually sent are recorded.
2. **Budget** – at `degrade_at` (default 80 %) of the org's daily budget the call is forced onto `degrade_chain`; at 100 % it raises `BudgetExceeded`.
3. **Cache** – exact-match, temperature 0 only, key includes the org. Off by default.
4. **Chain walk** – models of the task's chain (else `default_chain`) are tried in order; each has a circuit breaker. Any provider error or timeout falls
   through to the next model. All failing → `LLMUnavailable` (503).
5. **Metering** – a row per call (model, task, status, tokens, USD cost, latency, prompt version/hash). **Prompts and completions are never stored.**

## Configuration

Single model (default): `NEXUS_LLM_PROVIDER/MODEL/...` as before; optionally `NEXUS_LLM_PRICE_IN_PER_MTOK` / `..._OUT_...` to get cost figures.

Multi-model: `NEXUS_LLM_ROUTING_CONFIG` = inline JSON or `@/path/routing.json`:

```json
{"models": {"strong": {"provider": "anthropic", "model": "<model id>", "api_key_env": "ANTHROPIC_API_KEY", "price_in_per_mtok": 3, "price_out_per_mtok": 15},
            "cheap":  {"provider": "openai", "model": "<model id>", "api_key_env": "OPENAI_API_KEY", "base_url": "https://…/v1", "price_in_per_mtok": 0.2, "price_out_per_mtok": 0.8}},
 "default_chain": ["strong", "cheap"],
 "task_chains": {"rewrite": ["cheap"], "verify": ["cheap", "strong"]},
 "budget": {"daily_usd_per_org": 25, "degrade_at": 0.8, "degrade_chain": ["cheap"]}}
```

Keys are **never** in the file – a model names an environment variable (`api_key_env`), and startup fails if it is unset. Unknown keys, unknown model names in
chains and an empty default chain are rejected at startup. Prices are whatever you configure; NEXUS does not know provider prices.

## Admin API (`/admin/llm`, admins only, org-scoped, writes audited)

| Endpoint | Purpose |
|---|---|
| `GET /models` | routing table, circuit-breaker state, call/failure counts, budget, cache stats (no secrets) |
| `GET /usage?days=` | totals, by model / task / prompt version, cache-hit rate, today's budget state |
| `GET/POST /prompts`, `GET /prompts/{task}/versions/{v}` | list / create numbered overrides (must start with `TASK: <task>`) |
| `POST /prompts/{task}/activate` | `{"version": 2}` or `{"version": "builtin"}` (rollback) |
| `DELETE /cache` | clear the response cache |

`POST /chat`, `/reports` and `/agentic/runs` return **429 `budget_exceeded`** up front once the org's budget is spent. Inside a run that is already in flight,
the existing call sites degrade gracefully (an LLM failure there yields a weaker answer, not a crash), so the guard at the door is what makes exhaustion visible.

## Limits – read before relying on this

* Tested with fake/mock models and a real Redis only. **No real provider was called**; the OpenAI/Anthropic adapters are the same ones from earlier phases.
* Budget accounting is an in-memory running total seeded from the database: with several API replicas each has its own approximation, and a process restart
  re-seeds from flushed rows (up to `llm_usage_flush_seconds` of spend can be missed after a crash).
* Cost = configured price × reported token counts. If a provider omits usage, the mock/estimated counts apply; treat dollar figures as estimates.
* Prompt overrides are not evaluated automatically – changing `answer`/`verify` prompts can break the claim-verification contracts. Run `make eval` first
  (Phase 17 adds A/B tooling).
* No semantic cache by design (cross-tenant timing side channel); the exact cache helps mostly for repeated identical queries and re-runs.
* The cache is off by default so that evaluation numbers are not inflated by cache hits.
