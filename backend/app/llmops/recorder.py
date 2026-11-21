from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.database.models import LLMCall

log = get_logger("nexus.llmops")
MAX_BUFFER = 10_000


class UsageRecorder:
    """Buffers metered calls and writes them in batches (the hot path never waits for the database)."""

    def __init__(self, sf: async_sessionmaker[AsyncSession], flush_seconds: float = 2.0) -> None:
        self.sf, self.flush_seconds = sf, flush_seconds
        self._buf: list[dict] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._spent: dict[tuple[str, str], float] = {}     # (org, utc date) -> USD, maintained in memory for budget checks

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.flush()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()

    def record(self, **row) -> None:
        if len(self._buf) < MAX_BUFFER:
            self._buf.append(row)
        org = row.get("org_id")
        if org and row.get("cost_usd"):
            k = (org, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            self._spent[k] = self._spent.get(k, 0.0) + row["cost_usd"]

    async def flush(self) -> int:
        async with self._lock:
            rows, self._buf = self._buf, []
            if not rows:
                return 0
            try:
                async with self.sf() as db:
                    db.add_all([LLMCall(**r) for r in rows])
                    await db.commit()
            except Exception:  # noqa: BLE001 - keep the rows, retry on the next tick
                log.exception("usage flush failed")
                self._buf = (rows + self._buf)[:MAX_BUFFER]
                return 0
            return len(rows)

    async def spent_today(self, org_id: str) -> float:
        """In-memory running total, initialised once per (org, day) from the database. Per-replica approximation with several API replicas."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        k = (org_id, day)
        if k not in self._spent:
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            await self.flush()
            async with self.sf() as db:
                total = (await db.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.org_id == org_id, LLMCall.created_at >= start))).scalar_one()
            self._spent[k] = max(self._spent.get(k, 0.0), float(total))
        return self._spent[k]
