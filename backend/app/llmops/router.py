"""RoutingLLM: implements the LLM protocol, so every existing call site gets routing, fallback, caching, prompt versions, metering and
budgets without change. Behaviour per call:
  1. task := the `TASK:` line; active prompt override (per org) is substituted; version + hash recorded.
  2. exact-match cache (temperature 0 only, tenant-scoped key).
  3. budget: >= cap -> BudgetExceeded (HTTP 429); >= degrade_at -> forced onto the cheap chain.
  4. chain walk with per-model circuit breakers; any provider error falls through to the next model; all failing -> LLMUnavailable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from app.core.config import Settings
from app.core.context import current_org, current_user
from app.core.exceptions import BudgetExceeded, ConfigurationError, LLMUnavailable
from app.core.logging import get_logger
from app.llm.base import LLM, LLMResponse
from app.llmops.breaker import CircuitBreaker
from app.llmops.config import RoutingConfig
from app.llmops.prompts import PromptStore, sha, task_of
from app.llmops.recorder import UsageRecorder
from app.security.pii import redact

log = get_logger("nexus.llmops")
NEVER_FALL_BACK = (ConfigurationError, BudgetExceeded)


class RoutedModel:
    def __init__(self, name: str, llm: LLM, price_in: float, price_out: float, timeout: float, breaker: CircuitBreaker) -> None:
        self.name, self.llm, self.price_in, self.price_out, self.timeout, self.breaker = name, llm, price_in, price_out, timeout, breaker
        self.calls = 0

    def cost(self, pt: int, ct: int) -> float:
        return (pt * self.price_in + ct * self.price_out) / 1_000_000


class RoutingLLM:
    def __init__(self, models: dict[str, RoutedModel], cfg: RoutingConfig, recorder: UsageRecorder, prompts: PromptStore | None = None,
                 cache=None, metrics=None) -> None:
        self.models, self.cfg, self.recorder, self.prompts, self.cache, self.metrics = models, cfg, recorder, prompts, cache, metrics
        self.policies = None      # set by main: PolicyStore (Phase 15)
        self.name = cfg.models[cfg.default_chain[0]].model if len(cfg.models) == 1 else "router(" + ">".join(cfg.default_chain) + ")"

    # ------------------------------------------------------------------ helpers
    def _chain(self, task: str, degraded: bool) -> list[str]:
        if degraded and self.cfg.budget.degrade_chain:
            return self.cfg.budget.degrade_chain
        return self.cfg.task_chains.get(task) or self.cfg.default_chain

    def _key(self, org: str | None, chain: list[str], system: str, user: str, max_tokens: int) -> str:
        blob = json.dumps([org, chain, system, user, max_tokens], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _emit(self, **row) -> None:
        self.recorder.record(**row)
        if self.metrics is not None:
            self.metrics.inc("nexus_llm_calls_total", row["model"], row["task"], row["status"])
            if row["cost_usd"]:
                self.metrics.inc("nexus_llm_cost_usd_total", row["model"], by=row["cost_usd"])
            self.metrics.inc("nexus_llm_tokens_total", row["model"], "prompt", by=row["prompt_tokens"])
            self.metrics.inc("nexus_llm_tokens_total", row["model"], "completion", by=row["completion_tokens"])

    # ------------------------------------------------------------------ main entry
    async def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> LLMResponse:
        org, uid, task = current_org(), current_user(), task_of(system)
        version = "builtin"
        if self.prompts is not None:
            system, version = await self.prompts.resolve(org, task, system)
        if self.policies is not None and org:
            pol = await self.policies.get(org)
            if pol.pii_llm_prompts == "redact":
                user, found = redact(user, pol.pii_kinds)
                if self.metrics is not None:
                    for k, n in found.items():
                        self.metrics.inc("nexus_pii_findings_total", "llm_prompt", k, "redacted", by=n)
        psha = sha(system)
        base = {"org_id": org, "user_id": uid, "task": task, "prompt_version": version, "prompt_sha": psha}

        degraded = False
        b = self.cfg.budget
        if org and b.daily_usd_per_org:
            spent = await self.recorder.spent_today(org)
            if spent >= b.daily_usd_per_org:
                raise BudgetExceeded("Daily LLM budget for this organisation is exhausted")
            degraded = spent >= b.daily_usd_per_org * b.degrade_at
        chain = self._chain(task, degraded)

        key = None
        if self.cache is not None and temperature == 0:
            key = self._key(org, chain, system, user, max_tokens)
            hit = await self.cache.get(key)
            if hit is not None:
                self._emit(**base, model=hit["route"], provider_model=hit["model"], status="cache_hit", fallback_from=None, prompt_tokens=0,
                           completion_tokens=0, cost_usd=0.0, latency_ms=0.0)
                return LLMResponse(hit["text"], hit["model"], hit["pt"], hit["ct"], 0.0)

        last: Exception | None = None
        skipped_from: str | None = None
        for name in chain:
            m = self.models[name]
            if not m.breaker.allow():
                skipped_from = skipped_from or name
                continue
            t0 = time.perf_counter()
            try:
                r = await asyncio.wait_for(m.llm.complete(system, user, max_tokens=max_tokens, temperature=temperature), m.timeout)
            except NEVER_FALL_BACK:
                raise
            except Exception as exc:  # noqa: BLE001 - provider/network/timeouts: try the next model
                m.breaker.failure()
                last, skipped_from = exc, skipped_from or name
                self._emit(**base, model=name, provider_model="", status="error", fallback_from=None, prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
                           latency_ms=round((time.perf_counter() - t0) * 1000, 1))
                log.warning("model failed, falling back", extra={"extra_fields": {"model": name, "task": task, "error": type(exc).__name__}})
                continue
            m.breaker.success()
            m.calls += 1
            cost = m.cost(r.prompt_tokens, r.completion_tokens)
            self._emit(**base, model=name, provider_model=r.model, status="ok", fallback_from=skipped_from if skipped_from != name else None,
                       prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens, cost_usd=cost,
                       latency_ms=round(r.latency_ms or (time.perf_counter() - t0) * 1000, 1))
            if key is not None:
                await self.cache.set(key, {"text": r.text, "model": r.model, "route": name, "pt": r.prompt_tokens, "ct": r.completion_tokens})
            return r
        raise LLMUnavailable(f"No model available for task '{task}'" + (f" (last error: {type(last).__name__})" if last else " (all circuits open)"))

    def health(self) -> list[dict]:
        return [{"name": n, "provider_model": getattr(m.llm, "name", ""), "state": m.breaker.state, "calls": m.calls, "failures": m.breaker.total_failures,
                 "price_in_per_mtok": m.price_in, "price_out_per_mtok": m.price_out, "in_default_chain": n in self.cfg.default_chain,
                 "tasks": sorted(t for t, ch in self.cfg.task_chains.items() if n in ch)} for n, m in self.models.items()]


def build_router(settings: Settings, base_llm: LLM, recorder: UsageRecorder, prompts: PromptStore | None, cache, metrics, cfg: RoutingConfig | None,
                 builder=None) -> RoutingLLM:
    """`cfg is None` -> single-model passthrough (current behaviour) that still gets metering, cache and prompt versions."""
    if cfg is None:
        from app.llmops.config import ModelCfg

        cfg = RoutingConfig(models={"default": ModelCfg(provider="mock", model=base_llm.name, price_in_per_mtok=settings.llm_price_in_per_mtok,
                                                         price_out_per_mtok=settings.llm_price_out_per_mtok)}, default_chain=["default"])
        instances = {"default": base_llm}
    else:
        from app.llm.factory import build_llm_from

        instances = {n: (builder or build_llm_from)(m) for n, m in cfg.models.items()}
    models = {n: RoutedModel(n, instances[n], m.price_in_per_mtok, m.price_out_per_mtok, m.timeout_seconds,
                             CircuitBreaker(cfg.breaker_threshold, cfg.breaker_window_seconds, cfg.breaker_cooldown_seconds)) for n, m in cfg.models.items()}
    return RoutingLLM(models, cfg, recorder, prompts, cache, metrics)
