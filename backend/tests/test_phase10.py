"""Phase 10 infrastructure: S3/MinIO storage, Redis rate limiting, atomic job claiming, external workers."""
import asyncio
import shutil
import socket
import subprocess
import time

import httpx
import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.exceptions import NotFound
from app.core.ratelimit import RateLimiter, RedisRateLimiter
from app.main import create_app
from app.services.storage import S3Storage, build_storage
from tests.helpers import make_collection, register, search, seed


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfg(tmp_path, **kw) -> Settings:
    base = dict(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4)
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------------------------------------- S3 / MinIO
@pytest.fixture(scope="module")
def s3_server():
    from moto.server import ThreadedMotoServer
    port = free_port()
    srv = ThreadedMotoServer(port=port, verbose=False)
    srv.start()
    yield f"http://127.0.0.1:{port}"
    srv.stop()


async def test_s3_storage_roundtrip_missing_and_key_validation(s3_server):
    st = S3Storage("nexus-test-1", endpoint=s3_server, access_key="k", secret_key="s")
    await st.put("org/doc/v1.bin", b"\x00\x01payload")
    assert await st.get("org/doc/v1.bin") == b"\x00\x01payload"
    await st.put("org/doc/v1.bin", b"overwritten")
    assert await st.get("org/doc/v1.bin") == b"overwritten"
    await st.delete("org/doc/v1.bin")
    with pytest.raises(NotFound):
        await st.get("org/doc/v1.bin")
    for bad in ("../x", "/abs", "a/../b", ""):
        with pytest.raises(ValueError):
            await st.put(bad, b"x")


def test_build_storage_selects_backend_and_rejects_bad_config(tmp_path, s3_server):
    assert type(build_storage(cfg(tmp_path))).__name__ == "LocalStorage"
    assert isinstance(build_storage(cfg(tmp_path, storage_backend="s3", s3_endpoint=s3_server, s3_bucket="nexus-test-2", s3_access_key="k", s3_secret_key="s")), S3Storage)


async def test_end_to_end_ingestion_and_search_with_s3_backend(tmp_path, s3_server):
    c = cfg(tmp_path, storage_backend="s3", s3_endpoint=s3_server, s3_bucket="nexus-e2e", s3_access_key="k", s3_secret_key="s")
    app = create_app(c)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
            h = await register(cl, "Acme", "a@example.com")
            ids = await seed(cl, app, h, await make_collection(cl, h))
            r = await search(cl, h, "kubernetes container orchestration pods", top_k=3)
            assert r.json()["hits"][0]["document_id"] == ids["kubernetes.txt"]
    import boto3
    keys = [o["Key"] for o in boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="k", aws_secret_access_key="s",
                                           region_name="us-east-1").list_objects_v2(Bucket="nexus-e2e").get("Contents", [])]
    assert len(keys) == 3                      # the three seeded documents' original bytes live in the bucket, not on local disk
    assert not (tmp_path / "s").exists() or not any((tmp_path / "s").rglob("*.*"))


# ---------------------------------------------------------------------------------------------- Redis limiter
@pytest.fixture(scope="module")
def redis_url():
    exe = shutil.which("redis-server")
    if not exe:
        pytest.skip("redis-server not installed")
    port = free_port()
    p = subprocess.Popen([exe, "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"redis://127.0.0.1:{port}/0"
    p.terminate()
    p.wait(5)


async def test_redis_limiter_is_shared_across_replicas_and_windows_reset(redis_url):
    clock = [1000.0]
    a = RedisRateLimiter(redis_url, prefix="t1:", clock=lambda: clock[0])
    b = RedisRateLimiter(redis_url, prefix="t1:", clock=lambda: clock[0])      # a second API replica
    assert [await a.allow("ip", 3), await b.allow("ip", 3), await a.allow("ip", 3)] == [True, True, True]
    assert await b.allow("ip", 3) is False and await a.allow("ip", 3) is False   # limit is global, not per replica
    assert await a.allow("other-ip", 3) is True                                   # keys are independent
    clock[0] += 61
    assert await b.allow("ip", 3) is True                                         # new window
    await a.close()
    await b.close()


async def test_redis_limiter_falls_back_to_local_limits_when_redis_is_down():
    lim = RedisRateLimiter(f"redis://127.0.0.1:{free_port()}/0")                  # nothing listening
    assert [await lim.allow("ip", 2) for _ in range(3)] == [True, True, False]    # still limited, per replica
    await lim.close()


async def test_api_returns_429_through_redis_limiter(tmp_path, redis_url):
    app = create_app(cfg(tmp_path, redis_url=redis_url, rate_limit_per_minute=3))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await app.state.limiter._r.flushdb()
            codes = [(await c.get("/documents")).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_in_process_limiter_unchanged():
    t = [0.0]
    rl = RateLimiter(clock=lambda: t[0])
    assert [rl.allow_sync("k", 2) for _ in range(3)] == [True, True, False]
    t[0] = 61
    assert rl.allow_sync("k", 2)


# ---------------------------------------------------------------------------------------------- jobs / workers
async def test_job_is_claimed_atomically_and_runs_once(tmp_path, monkeypatch):
    app = create_app(cfg(tmp_path, ingestion_mode="external"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = await register(c, "Acme", "a@example.com")
            cid = await make_collection(c, h)
            r = await c.post(f"/collections/{cid}/documents", files={"file": ("a.txt", b"alpha beta gamma delta " * 20)}, headers=h)
            job_id = r.json()["job"]["id"] if "job" in r.json() else r.json()["job_id"]
            svc = app.state.ingestion
            calls = []
            orig = svc._run

            async def slow_run(db, job):
                calls.append(job.id)
                await asyncio.sleep(0.05)
                return await orig(db, job)
            monkeypatch.setattr(svc, "_run", slow_run)
            results = await asyncio.gather(*(svc.process_job(job_id) for _ in range(5)))
            assert calls == [job_id]                                   # exactly one worker ran it
            assert results.count("succeeded") == 1 and all(x in ("succeeded", "running") for x in results)


async def test_external_mode_api_only_enqueues_and_polling_worker_does_the_work(tmp_path):
    from app.worker import PollingWorker
    app = create_app(cfg(tmp_path, ingestion_mode="external"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = await register(c, "Acme", "a@example.com")
            cid = await make_collection(c, h)
            up = await c.post(f"/collections/{cid}/documents", files={"file": ("k.txt", b"kubernetes container orchestration pods " * 20)}, headers=h)
            assert up.status_code == 202
            await asyncio.sleep(0.2)
            assert (await search(c, h, "kubernetes pods")).json()["hits"] == []          # nothing processed by the API process
            worker = PollingWorker(app.state.ingestion, app.state.settings)
            assert await worker.run_once() == 1 and await worker.run_once() == 0           # processed once, queue now empty
            assert (await search(c, h, "kubernetes pods")).json()["hits"]                  # searchable after the worker ran


async def test_worker_retries_with_backoff_and_recovers_stuck_jobs(tmp_path, monkeypatch):
    from sqlalchemy import update
    from app.database.base import utcnow
    from app.database.models import IngestionJob
    from app.worker import PollingWorker
    app = create_app(cfg(tmp_path, ingestion_mode="external", ingestion_retry_backoff_seconds=3600.0))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = await register(c, "Acme", "a@example.com")
            cid = await make_collection(c, h)
            await c.post(f"/collections/{cid}/documents", files={"file": ("k.txt", b"kubernetes container orchestration pods " * 20)}, headers=h)
            svc = app.state.ingestion

            async def boom(db, job):
                raise ConnectionError("embedding backend down")      # transient -> retried
            monkeypatch.setattr(svc, "_run", boom)
            w = PollingWorker(svc, app.state.settings)
            assert await w.run_once() == 1                           # first attempt fails and is re-queued
            assert await w.run_once() == 0                           # not due again for an hour (exponential backoff)
            async with svc.sf() as db:
                job = (await db.execute(__import__("sqlalchemy").select(IngestionJob))).scalars().one()
                assert job.status == "queued" and job.attempts == 1
                await db.execute(update(IngestionJob).values(status="running"))   # simulate a crashed worker
                await db.commit()
            assert await svc.recover_stuck(older_than=__import__("datetime").timedelta(0))
            async with svc.sf() as db:
                assert (await db.execute(__import__("sqlalchemy").select(IngestionJob.status))).scalar_one() == "queued"
