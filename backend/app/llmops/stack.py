"""One place that assembles the LLM stack (router + recorder + prompt store + cache + policy store) so the API process and the standalone ingestion
worker behave identically (metering, budgets, routing, PII egress policy)."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.llm.factory import build_llm
from app.llmops.cache import MemoryCache, RedisCache
from app.llmops.config import load_routing_config
from app.llmops.prompts import PromptStore
from app.llmops.recorder import UsageRecorder
from app.llmops.router import RoutingLLM, build_router
from app.security.policy import PolicyStore


@dataclass
class LLMStack:
    llm: RoutingLLM
    recorder: UsageRecorder
    prompts: PromptStore
    cache: object | None
    policies: PolicyStore

    async def stop(self) -> None:
        await self.recorder.stop()
        if hasattr(self.cache, "close"):
            await self.cache.close()


def build_stack(settings: Settings, factory, metrics, builder=None) -> LLMStack:
    recorder = UsageRecorder(factory, settings.llm_usage_flush_seconds)
    recorder.start()
    prompts, policies = PromptStore(factory), PolicyStore(factory)
    cache = None
    if settings.llm_cache_enabled:
        cache = (RedisCache(settings.redis_url, settings.llm_cache_ttl_seconds) if settings.redis_url
                 else MemoryCache(settings.llm_cache_max_entries, settings.llm_cache_ttl_seconds))
    llm = build_router(settings, build_llm(settings), recorder, prompts, cache, metrics, load_routing_config(settings), builder=builder)
    llm.policies = policies
    return LLMStack(llm, recorder, prompts, cache, policies)
