"""In-process async job runner with bounded concurrency and exponential-backoff retries.

Jobs are durable in the database; this runner only schedules them. On start-up, queued jobs and jobs
orphaned by a crash are re-enqueued, so a restart never loses work. A Redis/Arq-backed runner can
replace this behind the same interface for multi-replica deployments (see ADR-003)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.database.models import IngestionJob
from app.ingestion.service import IngestionService

log = get_logger("nexus.runner")


class JobRunner:
    def __init__(self, service: IngestionService, workers: int = 2, backoff: float = 2.0, external: bool = False) -> None:
        self.service, self.n_workers, self.backoff, self.external = service, workers, backoff, external
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._delayed: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self.external:   # jobs stay 'queued' in the database; `python -m app.worker` processes pick them up
            return
        await self.service.recover_stuck(older_than=timedelta(0))
        async with self.service.sf() as db:
            for jid in (await db.execute(select(IngestionJob.id).where(IngestionJob.status == "queued"))).scalars():
                self._q.put_nowait(jid)
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.n_workers)]

    def enqueue(self, job_id: str) -> None:
        if not self.external:
            self._q.put_nowait(job_id)

    async def _worker(self) -> None:
        while True:
            jid = await self._q.get()
            try:
                status = await self.service.process_job(jid)
                if status == "queued":  # retry with exponential backoff
                    attempts = await self._attempts(jid)
                    delay = self.backoff * (2 ** max(attempts - 1, 0))
                    t = asyncio.create_task(self._requeue_later(jid, delay))
                    self._delayed.add(t)
                    t.add_done_callback(self._delayed.discard)
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("job runner crashed on job", extra={"extra_fields": {"job": jid}})
            finally:
                self._q.task_done()

    async def _attempts(self, jid: str) -> int:
        async with self.service.sf() as db:
            job = await db.get(IngestionJob, jid)
            return job.attempts if job else 1

    async def _requeue_later(self, jid: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self._q.put_nowait(jid)

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait until the queue is empty and no retries are pending (used by tests and graceful shutdown)."""
        async def _wait() -> None:
            while True:
                await self._q.join()
                if not self._delayed:
                    return
                await asyncio.gather(*list(self._delayed), return_exceptions=True)
        await asyncio.wait_for(_wait(), timeout)

    async def stop(self) -> None:
        for t in [*self._tasks, *self._delayed]:
            t.cancel()
        await asyncio.gather(*self._tasks, *self._delayed, return_exceptions=True)
