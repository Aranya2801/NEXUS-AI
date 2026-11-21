"""Exact-match response cache (temperature 0 only). Keys include the tenant, so a tenant can never learn whether another tenant sent a
prompt (timing side channel) — there is deliberately NO semantic/embedding cache, which would cross that boundary."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable

from app.core.logging import get_logger

log = get_logger("nexus.llmops")


class MemoryCache:
    backend = "memory"

    def __init__(self, maxsize: int = 1000, ttl: float = 3600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.maxsize, self.ttl, self.clock = maxsize, ttl, clock
        self._d: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.hits = self.misses = self.evictions = 0

    async def get(self, key: str) -> dict | None:
        item = self._d.get(key)
        if item is None or self.clock() - item[0] > self.ttl:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return item[1]

    async def set(self, key: str, value: dict) -> None:
        self._d[key] = (self.clock(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)
            self.evictions += 1

    async def clear(self) -> int:
        n = len(self._d)
        self._d.clear()
        return n

    def stats(self) -> dict:
        return {"backend": self.backend, "size": len(self._d), "hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "hit_rate": round(self.hits / (self.hits + self.misses), 3) if self.hits + self.misses else None}


class RedisCache:
    """Shared across replicas. Fail-soft: a Redis outage is a cache miss, never an error."""

    backend = "redis"

    def __init__(self, url: str, ttl: float = 3600.0, prefix: str = "nexus:llm:") -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        self.ttl, self.prefix = ttl, prefix
        self.hits = self.misses = self.errors = 0

    async def get(self, key: str) -> dict | None:
        try:
            raw = await self._r.get(self.prefix + key)
        except Exception:  # noqa: BLE001
            self.errors += 1
            self.misses += 1
            return None
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(raw)

    async def set(self, key: str, value: dict) -> None:
        try:
            await self._r.set(self.prefix + key, json.dumps(value), ex=int(self.ttl))
        except Exception:  # noqa: BLE001
            self.errors += 1

    async def clear(self) -> int:
        n = 0
        try:
            async for k in self._r.scan_iter(match=self.prefix + "*", count=200):
                await self._r.delete(k)
                n += 1
        except Exception:  # noqa: BLE001
            self.errors += 1
        return n

    def stats(self) -> dict:
        return {"backend": self.backend, "hits": self.hits, "misses": self.misses, "errors": self.errors,
                "hit_rate": round(self.hits / (self.hits + self.misses), 3) if self.hits + self.misses else None}

    async def close(self) -> None:
        await self._r.aclose()
