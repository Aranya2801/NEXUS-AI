"""Distributed mutual exclusion with expiring leases. Used so that, with several API replicas, a connector sync (or a periodic housekeeping loop) runs on exactly
one replica at a time. `RedisLease` = SET NX PX + token-checked renew/release (Lua), `MemoryLease` = same semantics inside one process (single replica / tests).
Fail-CLOSED: if Redis is unreachable nobody gets the lease, the guarded work is skipped this tick and retried later (duplicate work is worse than delayed work)."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator, Callable

from app.core.logging import get_logger

log = get_logger("nexus.lease")

_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class MemoryLease:
    backend = "memory"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._clock = clock

    async def acquire(self, key: str, ttl: float) -> str | None:
        cur = self._held.get(key)
        if cur and cur[1] > self._clock():
            return None
        tok = secrets.token_hex(8)
        self._held[key] = (tok, self._clock() + ttl)
        return tok

    async def renew(self, key: str, token: str, ttl: float) -> bool:
        cur = self._held.get(key)
        if not cur or cur[0] != token or cur[1] <= self._clock():
            return False
        self._held[key] = (token, self._clock() + ttl)
        return True

    async def release(self, key: str, token: str) -> None:
        cur = self._held.get(key)
        if cur and cur[0] == token:
            del self._held[key]

    async def is_held(self, key: str) -> bool:
        cur = self._held.get(key)
        return bool(cur and cur[1] > self._clock())

    async def close(self) -> None:
        return None


class RedisLease:
    backend = "redis"

    def __init__(self, url: str, prefix: str = "nexus:lease:") -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        self.prefix = prefix

    async def acquire(self, key: str, ttl: float) -> str | None:
        tok = secrets.token_hex(8)
        try:
            return tok if await self._r.set(self.prefix + key, tok, nx=True, px=int(ttl * 1000)) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("lease backend unavailable; skipping guarded work", extra={"extra_fields": {"error": type(exc).__name__}})
            return None

    async def renew(self, key: str, token: str, ttl: float) -> bool:
        try:
            return bool(await self._r.eval(_RENEW, 1, self.prefix + key, token, int(ttl * 1000)))
        except Exception:  # noqa: BLE001
            return False

    async def release(self, key: str, token: str) -> None:
        with contextlib.suppress(Exception):
            await self._r.eval(_RELEASE, 1, self.prefix + key, token)

    async def is_held(self, key: str) -> bool:
        try:
            return bool(await self._r.exists(self.prefix + key))
        except Exception:  # noqa: BLE001
            return True      # unknown -> assume held: callers use this to decide whether something is orphaned, and wrongly "reaping" live work is worse

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._r.aclose()


def build_lease(redis_url: str | None):
    return RedisLease(redis_url) if redis_url else MemoryLease()


class Held:
    """Result of `hold`: `.acquired` tells whether the caller owns the lease; `.lost` flips if a renewal failed (the work should be considered suspect)."""

    def __init__(self) -> None:
        self.acquired = False
        self.lost = False


@contextlib.asynccontextmanager
async def hold(lease, key: str, ttl: float = 30.0) -> AsyncIterator[Held]:
    """`async with hold(lease, 'sync:42') as h:` – if `h.acquired`, a background task renews every ttl/3 until the block exits."""
    h = Held()
    tok = await lease.acquire(key, ttl)
    if tok is None:
        yield h
        return
    h.acquired = True

    async def renewer() -> None:
        while True:
            await asyncio.sleep(ttl / 3)
            if not await lease.renew(key, tok, ttl):
                h.lost = True
                log.error("lease lost while work was running", extra={"extra_fields": {"key": key}})
                return

    task = asyncio.create_task(renewer())
    try:
        yield h
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await lease.release(key, tok)
