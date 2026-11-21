"""Rate limiters. `RateLimiter` is an in-process sliding window (single replica). `RedisRateLimiter` is a shared fixed-window
counter (INCR + EXPIRE) so limits hold across API replicas; if Redis is unreachable it falls back to a local limiter (fail-soft:
limits become per-replica rather than disappearing) and logs the degradation."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.core.logging import get_logger

log = get_logger("nexus.ratelimit")


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._clock = clock

    def allow_sync(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    async def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        return self.allow_sync(key, limit, window)


class RedisRateLimiter:
    def __init__(self, url: str, *, prefix: str = "nexus:rl:", fallback: RateLimiter | None = None, clock: Callable[[], float] = time.time) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        self.prefix, self.fallback, self._clock = prefix, fallback or RateLimiter(), clock
        self._degraded = False

    async def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        bucket = int(self._clock() // window)
        k = f"{self.prefix}{key}:{bucket}"
        try:
            pipe = self._r.pipeline(transaction=True)
            pipe.incr(k)
            pipe.expire(k, int(window) + 1)
            count = (await pipe.execute())[0]
            if self._degraded:
                log.info("redis rate limiter recovered")
                self._degraded = False
            return int(count) <= limit
        except Exception as e:  # noqa: BLE001 - never take the API down because the limiter store is unavailable
            if not self._degraded:
                log.warning("redis rate limiter unavailable; using per-replica fallback", extra={"extra_fields": {"error": type(e).__name__}})
                self._degraded = True
            return self.fallback.allow_sync(key, limit, window)

    async def close(self) -> None:
        await self._r.aclose()


def build_limiter(redis_url: str | None):
    return RedisRateLimiter(redis_url) if redis_url else RateLimiter()
