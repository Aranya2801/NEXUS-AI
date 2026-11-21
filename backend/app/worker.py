"""Standalone ingestion worker: `python -m app.worker`.

Polls the `ingestion_jobs` table and processes queued jobs. Safe to run N copies against one database: `process_job` claims a job
with an atomic compare-and-set, so a job is never executed twice. Failed attempts go back to `queued` and are retried with
exponential backoff (measured from the job's `updated_at`); jobs abandoned by a crashed worker are recovered after a timeout.
Use together with NEXUS_INGESTION_MODE=external on the API."""
from __future__ import annotations

import asyncio
from pathlib import Path
import signal
from datetime import timedelta

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.database.models import IngestionJob
from app.database.session import make_engine, make_session_factory
from app.embeddings.factory import build_embedder
from app.ingestion.service import IngestionService
from app.knowledge_graph.factory import build_extractor
from app.knowledge_graph.indexer import GraphIndexer
from app.distributed.bus import build_bus, wire_cache_invalidation
from app.llmops.stack import build_stack
from app.security import auditsig
from app.multimodal.vision import build_vision
from app.services.storage import build_storage

log = get_logger("nexus.worker")
STUCK_AFTER = timedelta(minutes=15)


class PollingWorker:
    def __init__(self, service: IngestionService, settings: Settings) -> None:
        self.service, self.settings = service, settings
        self._stop = asyncio.Event()

    def _due(self, job: IngestionJob, now) -> bool:
        if job.attempts == 0:
            return True
        delay = self.settings.ingestion_retry_backoff_seconds * (2 ** max(job.attempts - 1, 0))
        return now - job.updated_at.replace(tzinfo=now.tzinfo) >= timedelta(seconds=delay)

    async def run_once(self, limit: int = 10) -> int:
        """Process up to `limit` due jobs; returns how many were attempted."""
        from app.database.base import utcnow
        now = utcnow()
        async with self.service.sf() as db:
            rows = list((await db.execute(select(IngestionJob).where(IngestionJob.status == "queued")
                                          .order_by(IngestionJob.created_at).limit(limit * 4))).scalars())
        due = [j.id for j in rows if self._due(j, now)][:limit]
        await asyncio.gather(*(self._safe(j) for j in due))
        return len(due)

    async def _safe(self, job_id: str) -> None:
        try:
            await self.service.process_job(job_id)
        except Exception:  # noqa: BLE001 - a worker must never die
            log.exception("worker crashed on job", extra={"extra_fields": {"job": job_id}})

    async def run_forever(self) -> None:
        beat = asyncio.create_task(self._heartbeat())
        try:
            await self._run_forever()
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)

    async def _run_forever(self) -> None:
        last_recover = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            if loop.time() - last_recover > 60:
                await self.service.recover_stuck(older_than=STUCK_AFTER)
                last_recover = loop.time()
            n = await self.run_once(limit=max(1, self.settings.ingestion_workers))
            if n == 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), self.settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def _heartbeat(self) -> None:
        """Touches a file every 15 s from the event loop. The Kubernetes liveness probe fails when it goes stale, i.e. when the loop is wedged
        (long jobs do NOT stall it: parsing/OCR run in threads)."""
        path = Path(self.settings.worker_heartbeat_path)
        while True:
            try:
                path.touch()
            except OSError:
                log.warning("cannot write heartbeat file")
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)

    def stop(self) -> None:
        self._stop.set()


async def amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    stack = build_stack(settings, factory, None)     # same routing/metering/policy stack as the API
    llm = stack.llm
    auditsig.set_key(settings.jwt_secret)
    service = IngestionService(factory, build_storage(settings), settings, build_embedder(settings),
                               GraphIndexer(build_extractor(settings, llm), settings), build_vision(settings))
    service.policies = stack.policies
    bus = build_bus(settings.redis_url)      # policy/prompt changes made on API replicas reach this process
    await wire_cache_invalidation(bus, stack.policies, stack.prompts)
    worker = PollingWorker(service, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    log.info("ingestion worker started")
    try:
        await worker.run_forever()
    finally:
        await bus.stop()
        await stack.stop()
        await engine.dispose()
        log.info("ingestion worker stopped")


if __name__ == "__main__":
    asyncio.run(amain())
