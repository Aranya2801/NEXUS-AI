"""Background quality monitor: periodically evaluates every active org and records NEW alerts (audit action `quality.alert`, one per org+metric per day)
so regressions are noticed without anyone opening a dashboard. Runs on one replica at a time (lease)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import audit
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import AgentRun, AuditLog, Organization
from app.distributed.lease import hold
from app.learning import quality

log = get_logger("nexus.quality")


class QualityMonitor:
    def __init__(self, sf: async_sessionmaker[AsyncSession], lease, interval: float, metrics=None, days: int = 14) -> None:
        self.sf, self.lease, self.interval, self.metrics, self.days = sf, lease, interval, metrics, days
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.interval > 0:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                async with hold(self.lease, "housekeeping:quality-monitor", max(self.interval, 60)) as h:
                    if h.acquired:
                        await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("quality monitor tick failed")

    async def run_once(self) -> dict[str, list[str]]:
        """Returns {org_id: [metrics newly alerted]}."""
        out: dict[str, list[str]] = {}
        since = utcnow() - timedelta(days=self.days * 2)
        async with self.sf() as db:
            org_ids = (await db.execute(select(AgentRun.org_id).where(AgentRun.created_at >= since).distinct())).scalars().all()
        active = 0
        for org in org_ids:
            async with self.sf() as db:
                snap = await quality.snapshot(db, org, self.days, self.days)
                regs = [a for a in snap["alerts"] if a["direction"] == "regression"]
                active += len(regs)
                recent = {a for (a,) in (await db.execute(select(AuditLog.resource).where(AuditLog.org_id == org, AuditLog.action == "quality.alert",
                                                                                        AuditLog.created_at >= utcnow() - timedelta(hours=24)))).all()}
                new = [a for a in regs if a["metric"] not in recent]
                for a in new:
                    await audit(db, "quality.alert", org_id=org, resource=a["metric"], baseline=a["baseline"], recent=a["recent"], delta=a["delta"],
                                p_value=round(a["p_value"], 6), n_recent=a["n_recent"], n_baseline=a["n_baseline"])
                await db.commit()
                if new:
                    out[org] = [a["metric"] for a in new]
        if self.metrics is not None:
            self.metrics.set("nexus_quality_regressions_active", active)
        return out
