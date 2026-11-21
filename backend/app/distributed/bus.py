"""Small event bus. Two delivery modes:

* **fanout** – every subscriber process sees every event (cache invalidation, notifications). Best-effort: a replica that is down misses events (so every cache
  also has a TTL). Redis: plain XREAD from "now", no server-side state.
* **group** – each event goes to ONE consumer of the group, is acknowledged after the handler succeeds, redelivered when a consumer dies or the handler fails
  (XAUTOCLAIM of entries idle longer than `idle_ms`) and moved to `<topic>:dlq` after `max_deliveries` attempts (at-least-once: handlers must be idempotent).

`MemoryBus` has the same semantics inside one process. Payloads are JSON objects; never put secrets or document text in events."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable

from app.core.logging import get_logger

log = get_logger("nexus.bus")
Handler = Callable[[dict], Awaitable[None]]


class MemoryBus:
    backend = "memory"

    def __init__(self, max_deliveries: int = 3) -> None:
        self._fan: dict[str, list[Handler]] = defaultdict(list)
        self._groups: dict[tuple[str, str], list[Handler]] = defaultdict(list)
        self.dlq: dict[str, list[dict]] = defaultdict(list)
        self.max_deliveries = max_deliveries
        self._rr: dict[tuple[str, str], int] = defaultdict(int)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def subscribe_fanout(self, topic: str, handler: Handler) -> None:
        self._fan[topic].append(handler)

    async def subscribe_group(self, topic: str, group: str, handler: Handler, **_) -> None:
        self._groups[(topic, group)].append(handler)

    async def publish(self, topic: str, data: dict) -> None:
        json.dumps(data)                                    # same contract as Redis: must be JSON-serialisable
        for h in list(self._fan[topic]):
            try:
                await h(dict(data))
            except Exception:  # noqa: BLE001
                log.exception("fanout handler failed", extra={"extra_fields": {"topic": topic}})
        for (t, g), hs in list(self._groups.items()):
            if t != topic or not hs:
                continue
            h = hs[self._rr[(t, g)] % len(hs)]
            self._rr[(t, g)] += 1
            for attempt in range(1, self.max_deliveries + 1):
                try:
                    await h(dict(data))
                    break
                except Exception:  # noqa: BLE001
                    log.warning("group handler failed", extra={"extra_fields": {"topic": topic, "attempt": attempt}})
            else:
                self.dlq[topic].append(data)


class RedisBus:
    backend = "redis"

    def __init__(self, url: str, prefix: str = "nexus:ev:", maxlen: int = 10_000, max_deliveries: int = 3, idle_ms: int = 30_000, block_ms: int = 1000) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=block_ms / 1000 + 5)
        self.prefix, self.maxlen, self.max_deliveries, self.idle_ms, self.block_ms = prefix, maxlen, max_deliveries, idle_ms, block_ms
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._r.aclose()

    async def publish(self, topic: str, data: dict) -> None:
        try:
            await self._r.xadd(self.prefix + topic, {"d": json.dumps(data)}, maxlen=self.maxlen, approximate=True)
        except Exception as exc:  # noqa: BLE001 - publishing must never break the request that triggered it
            log.warning("event publish failed", extra={"extra_fields": {"topic": topic, "error": type(exc).__name__}})

    # ------------------------------------------------------------------ fanout
    async def subscribe_fanout(self, topic: str, handler: Handler) -> None:
        try:
            last = (await self._r.xinfo_stream(self.prefix + topic))["last-generated-id"]
            last = last.decode() if isinstance(last, bytes) else last
        except Exception:  # noqa: BLE001 - stream does not exist yet
            last = "0-0"
        self._tasks.append(asyncio.create_task(self._fan_loop(topic, handler, last)))

    async def _fan_loop(self, topic: str, handler: Handler, last: str) -> None:
        key = self.prefix + topic
        while not self._stop.is_set():
            try:
                res = await self._r.xread({key: last}, block=self.block_ms, count=100)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)
                continue
            for _, entries in res or []:
                for eid, fields in entries:
                    last = eid.decode() if isinstance(eid, bytes) else eid
                    await self._dispatch(topic, handler, fields)

    @staticmethod
    async def _dispatch(topic: str, handler: Handler, fields) -> bool:
        raw = fields.get(b"d") if b"d" in fields else fields.get("d")
        try:
            await handler(json.loads(raw))
            return True
        except Exception:  # noqa: BLE001
            log.exception("event handler failed", extra={"extra_fields": {"topic": topic}})
            return False

    # ------------------------------------------------------------------ group (at-least-once, ack, claim, DLQ)
    async def subscribe_group(self, topic: str, group: str, handler: Handler, consumer: str = "c1", **_) -> None:
        key = self.prefix + topic
        with contextlib.suppress(Exception):
            await self._r.xgroup_create(key, group, id="0", mkstream=True)     # BUSYGROUP when it already exists is fine
        self._tasks.append(asyncio.create_task(self._group_loop(topic, group, consumer, handler)))

    async def _group_loop(self, topic: str, group: str, consumer: str, handler: Handler) -> None:
        key = self.prefix + topic
        while not self._stop.is_set():
            try:
                await self._reclaim(key, topic, group, consumer, handler)
                res = await self._r.xreadgroup(group, consumer, {key: ">"}, count=10, block=self.block_ms)
                for _, entries in res or []:
                    for eid, fields in entries:
                        if await self._dispatch(topic, handler, fields):
                            await self._r.xack(key, group, eid)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)

    async def _reclaim(self, key: str, topic: str, group: str, consumer: str, handler: Handler) -> None:
        """Takes over entries a dead/failed consumer left pending; poison messages go to the dead-letter stream."""
        pending = await self._r.xpending_range(key, group, min="-", max="+", count=50, idle=self.idle_ms)
        for p in pending:
            eid = p["message_id"]
            if p["times_delivered"] >= self.max_deliveries:
                got = await self._r.xrange(key, eid, eid)
                if got:
                    await self._r.xadd(key + ":dlq", {"d": got[0][1].get(b"d", b"{}"), "from": eid if isinstance(eid, str) else eid.decode(), "group": group})
                await self._r.xack(key, group, eid)
                log.error("event moved to dead-letter stream", extra={"extra_fields": {"topic": topic, "group": group}})
                continue
            claimed = await self._r.xclaim(key, group, consumer, min_idle_time=self.idle_ms, message_ids=[eid])
            for cid, fields in claimed:
                if await self._dispatch(topic, handler, fields):
                    await self._r.xack(key, group, cid)

    async def dlq_len(self, topic: str) -> int:
        with contextlib.suppress(Exception):
            return int(await self._r.xlen(self.prefix + topic + ":dlq"))
        return 0


def build_bus(redis_url: str | None, **kw):
    return RedisBus(redis_url, **kw) if redis_url else MemoryBus()


TOPIC_POLICY = "policy.changed"
TOPIC_PROMPTS = "prompts.changed"
TOPIC_EXPERIMENTS = "experiments.changed"


async def wire_cache_invalidation(bus, policies, prompts, experiments=None) -> None:
    """Every replica drops its cached copy of an org's policy / prompt overrides when another replica changes them."""
    async def on_policy(ev: dict) -> None:
        policies.invalidate(ev.get("org_id"))

    async def on_prompts(ev: dict) -> None:
        prompts.invalidate(ev.get("org_id"))

    await bus.subscribe_fanout(TOPIC_POLICY, on_policy)
    await bus.subscribe_fanout(TOPIC_PROMPTS, on_prompts)
    if experiments is not None:
        async def on_experiments(ev: dict) -> None:
            experiments.invalidate(ev.get("org_id"))

        await bus.subscribe_fanout(TOPIC_EXPERIMENTS, on_experiments)
