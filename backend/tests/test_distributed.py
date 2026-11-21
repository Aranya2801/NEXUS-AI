"""Phase 16: leases, event bus (fanout + at-least-once groups with DLQ), cross-replica behaviour — against a real redis-server."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.database.base import utcnow
from app.database.models import Connector, ConnectorRun, User
from app.distributed.bus import MemoryBus, RedisBus
from app.distributed.lease import MemoryLease, RedisLease, hold
from app.main import create_app
from tests.helpers import register
from tests.test_connectors import DRIVE_CFG, LONG, drive_file, mk, setup  # noqa: F401
from tests.test_phase10 import redis_url  # noqa: F401  (fixture)
from tests.fake_sources import FakeWorld, iso


async def eventually(fn, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        v = await fn()
        if v:
            return v
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


# ===================================================================== leases
async def test_memory_lease_exclusive_expires_renews_and_checks_token():
    t = [0.0]
    L = MemoryLease(clock=lambda: t[0])
    a = await L.acquire("k", 10)
    assert a and await L.acquire("k", 10) is None and await L.is_held("k")
    assert not await L.renew("k", "wrong", 10)
    t[0] = 9
    assert await L.renew("k", a, 10)
    t[0] = 18
    assert await L.acquire("k", 10) is None            # renewal extended it
    t[0] = 20
    b = await L.acquire("k", 10)                       # expired -> a crashed holder cannot block forever
    assert b and not await L.renew("k", a, 10)         # the old holder can no longer renew
    await L.release("k", a)                            # releasing with a stale token is a no-op
    assert await L.is_held("k")
    await L.release("k", b)
    assert not await L.is_held("k")


async def test_redis_lease_is_shared_between_replicas_and_token_safe(redis_url):
    r1, r2 = RedisLease(redis_url, "t16a:"), RedisLease(redis_url, "t16a:")
    tok = await r1.acquire("job", 0.4)
    assert tok and await r2.acquire("job", 0.4) is None and await r2.is_held("job")
    assert not await r2.renew("job", "nope", 1)
    await r2.release("job", "nope")                    # wrong token: must not free someone else's lease
    assert await r1.is_held("job")
    await asyncio.sleep(0.6)                           # expiry frees a dead holder
    tok2 = await r2.acquire("job", 5)
    assert tok2 and not await r1.renew("job", tok, 5)  # the original holder lost it and learns so on renew
    await r2.release("job", tok2)
    assert not await r1.is_held("job")
    await r1.close()
    await r2.close()


async def test_redis_lease_fails_closed_when_redis_is_down():
    d = RedisLease("redis://127.0.0.1:1/0", "x:")
    assert await d.acquire("k", 5) is None             # no lease -> guarded work is skipped, never duplicated
    assert await d.is_held("k") is True                # unknown -> "held": never reap other replicas' live work on a blip
    await d.close()


async def test_hold_auto_renews_beyond_ttl_and_releases(redis_url):
    a, b = RedisLease(redis_url, "t16b:"), RedisLease(redis_url, "t16b:")
    async with hold(a, "long", ttl=0.3) as h:
        assert h.acquired
        for _ in range(4):                              # 0.8 s of work with a 0.3 s TTL
            await asyncio.sleep(0.2)
            assert await b.acquire("long", 1) is None
        assert not h.lost
    tok = await b.acquire("long", 1)
    assert tok
    async with hold(a, "long", ttl=1) as h2:
        assert not h2.acquired                          # someone else owns it
    await b.release("long", tok)
    await a.close()
    await b.close()


# ===================================================================== bus
async def test_memory_bus_fanout_group_retry_and_dlq():
    bus, got, calls = MemoryBus(max_deliveries=3), [], []
    await bus.subscribe_fanout("t", lambda e: _append(got, e))
    await bus.subscribe_fanout("t", lambda e: _append(got, e))

    async def flaky(e):
        calls.append(e)
        if len(calls) < 3:
            raise RuntimeError("boom")

    async def poison(e):
        raise RuntimeError("always")
    await bus.subscribe_group("t", "g1", flaky)
    await bus.subscribe_group("t", "g2", poison)
    await bus.publish("t", {"n": 1})
    assert len(got) == 2 and len(calls) == 3 and bus.dlq["t"] == [{"n": 1}]     # g1 succeeded on 3rd try; g2 dead-lettered
    with pytest.raises(TypeError):
        await bus.publish("t", {"bad": object()})                                # payload contract is enforced in-memory too


async def _append(lst, e):
    lst.append(e)


async def test_redis_bus_fanout_reaches_every_replica_and_survives_bad_handlers(redis_url):
    a, b, c = (RedisBus(redis_url, prefix="t16f:", block_ms=100) for _ in range(3))
    seen_a, seen_b = [], []

    async def bad(e):
        raise RuntimeError("handler bug")
    await a.subscribe_fanout("x", lambda e: _append(seen_a, e))
    await b.subscribe_fanout("x", lambda e: _append(seen_b, e))
    await b.subscribe_fanout("x", bad)
    await c.publish("x", {"org_id": "o1"})
    await c.publish("x", {"org_id": "o2"})
    await eventually(lambda: _ready(len(seen_a) == 2 and len(seen_b) == 2))
    assert [e["org_id"] for e in seen_a] == ["o1", "o2"]
    for x in (a, b, c):
        await x.stop()


async def _ready(v):
    return v


async def test_redis_bus_publish_never_raises_when_redis_is_down():
    d = RedisBus("redis://127.0.0.1:1/0")
    await d.publish("x", {"a": 1})                    # fail-soft: the request that triggered the event must not fail
    await d.stop()


async def test_redis_bus_group_work_is_shared_acked_redelivered_and_dead_lettered(redis_url):
    kw = dict(prefix="t16g:", block_ms=100, idle_ms=200, max_deliveries=3)
    p, w1, w2 = RedisBus(redis_url, **kw), RedisBus(redis_url, **kw), RedisBus(redis_url, **kw)
    done: list[str] = []

    async def make(name):
        async def h(e):
            done.append(f"{name}:{e['n']}")
        return h
    await w1.subscribe_group("jobs", "workers", await make("w1"), consumer="w1")
    await w2.subscribe_group("jobs", "workers", await make("w2"), consumer="w2")
    for n in range(6):
        await p.publish("jobs", {"n": n})
    await eventually(lambda: _ready(len(done) == 6))
    assert sorted(int(x.split(":")[1]) for x in done) == list(range(6))          # each event processed exactly once across the group

    # a consumer that dies after reading (never acks): the entry is claimed and processed by a live consumer
    raw = RedisBus(redis_url, **kw)
    await raw._r.xgroup_create("t16g:jobs2", "workers", id="0", mkstream=True)
    await p.publish("jobs2", {"n": 99})
    got = await raw._r.xreadgroup("workers", "dead-consumer", {"t16g:jobs2": ">"}, count=1)
    assert got                                                                    # read, then "crash" without ack
    rescued: list[dict] = []
    await w1.subscribe_group("jobs2", "workers", lambda e: _append(rescued, e), consumer="w1")
    await eventually(lambda: _ready(rescued == [{"n": 99}]), timeout=6)
    await asyncio.sleep(0.4)
    assert len(rescued) == 1                                                      # acked after rescue: not redelivered again

    # poison message: fails every time -> dead-letter stream after max_deliveries, then acked (queue is not blocked)
    attempts = []

    async def poison(e):
        attempts.append(e)
        raise RuntimeError("poison")
    await w2.subscribe_group("jobs3", "workers", poison, consumer="w2")
    await p.publish("jobs3", {"n": 666})
    await eventually(lambda: _dlq(p), timeout=10)
    assert len(attempts) >= 3
    for x in (p, w1, w2, raw):
        await x.stop()


async def _dlq(p):
    return (await p.dlq_len("jobs3")) == 1


# ===================================================================== two API replicas sharing Redis + database
def replica_cfg(tmp_path, redis_url, **kw):
    return Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/shared.db", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                    ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, connector_dns_check=False, connector_scheduler_seconds=3600, redis_url=redis_url,
                    rate_limit_per_minute=100000, checkpoint_path=str(tmp_path / "ck.sqlite"), **kw)


async def test_policy_change_on_one_replica_invalidates_the_others_cache(tmp_path, redis_url):
    A, B = create_app(replica_cfg(tmp_path, redis_url)), create_app(replica_cfg(tmp_path, redis_url))
    async with A.router.lifespan_context(A), B.router.lifespan_context(B):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A), base_url="http://a") as ca:
            h = await register(ca, "Org A", "a@example.com")
            async with A.state.session_factory() as db:
                org = (await db.execute(select(User.org_id))).scalar_one()
            assert (await B.state.policies.get(org)).pii_answers == "off"          # B caches the default policy
            assert (await ca.put("/admin/security/policy", json={"pii_answers": "redact"}, headers=h)).status_code == 200

            async def b_sees():
                return (await B.state.policies.get(org)).pii_answers == "redact"
            await eventually(b_sees, timeout=5)                                    # well under the 10 s cache TTL: it was the event, not expiry
            b_prompts = B.state.llmops["prompts"]
            b_prompts._cache[(org, "rewrite")] = (10**12, "9", "stale")
            r = await ca.post("/admin/llm/prompts", json={"task": "rewrite", "text": "TASK: rewrite\nnew prompt text"}, headers=h)
            assert r.status_code == 201
            await eventually(lambda: _ready((org, "rewrite") not in b_prompts._cache), timeout=5)


async def test_connector_sync_is_single_flight_across_replicas_and_reaper_respects_live_leases(tmp_path, redis_url):
    world = FakeWorld()
    A, B = create_app(replica_cfg(tmp_path, redis_url)), create_app(replica_cfg(tmp_path, redis_url))
    for a in (A, B):
        a.state.connector_client_factory = lambda: httpx.AsyncClient(transport=world.transport())
    async with A.router.lifespan_context(A), B.router.lifespan_context(B):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A), base_url="http://a") as ca:
            h, cid = await setup(ca)
            world.drive_files = [drive_file(1, "good.txt", LONG, iso(d=1))]
            conn = (await mk(ca, h, cid, "google_drive", *DRIVE_CFG)).json()["id"]
            other = RedisLease(redis_url)                                           # "replica C" is mid-sync on this connector
            tok = await other.acquire(f"connector-sync:{conn}", 30)
            assert await A.state.connectors.sync(conn) is None and await B.state.connectors.sync(conn) is None
            async with A.state.session_factory() as db:
                assert (await db.execute(select(ConnectorRun))).scalars().all() == []      # nothing started, nothing recorded

            # replica restart while another replica's run is live: the run is NOT reaped
            async with A.state.session_factory() as db:
                org = (await db.get(Connector, conn)).org_id
                db.add(ConnectorRun(org_id=org, connector_id=conn, started_at=utcnow()))
                await db.commit()
            await B.state.connectors.start()
            async with A.state.session_factory() as db:
                assert (await db.execute(select(ConnectorRun.status))).scalar_one() == "running"
            await other.release(f"connector-sync:{conn}", tok)                      # holder died / finished: now it IS orphaned
            await B.state.connectors.start()
            async with A.state.session_factory() as db:
                assert (await db.execute(select(ConnectorRun.status))).scalar_one() == "failed"

            # lease free: exactly one of two simultaneous syncs actually runs
            results = await asyncio.gather(A.state.connectors.sync(conn), B.state.connectors.sync(conn))
            assert sorted(r is None for r in results) == [False, True]
            await A.state.runner.drain()
            async with A.state.session_factory() as db:
                runs = (await db.execute(select(ConnectorRun).where(ConnectorRun.status != "failed"))).scalars().all()
            assert len(runs) == 1
            await other.close()


async def test_queue_depth_gauge_is_exported_for_autoscaling(tmp_path):
    cfg = Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/g.db", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                   bcrypt_rounds=4, queue_gauge_seconds=0.1)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = await register(c, "Org A", "a@example.com")

            async def has():
                return 'nexus_ingestion_queue_depth{status="queued"} 0' in (await c.get("/admin/metrics", headers=h)).text
            await eventually(has, timeout=3)
            assert "# TYPE nexus_ingestion_queue_depth gauge" in (await c.get("/admin/metrics", headers=h)).text
