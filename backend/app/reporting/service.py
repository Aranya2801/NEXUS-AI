"""Asynchronous report generation with status/progress tracking. Reports run as background tasks (bounded concurrency);
on start-up, reports left `queued`/`running` by a previous process are marked failed (`interrupted`) so users can regenerate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.deps import AgentDeps
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.logging import get_logger
from app.database.models import Report
from app.memory.service import MemoryService
from app.reporting.builder import ReportBuilder

log = get_logger("nexus.reports")


class ReportService:
    def __init__(self, deps: AgentDeps, memory: MemoryService, concurrency: int = 2) -> None:
        self.deps, self.memory = deps, memory
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    async def create(self, db: AsyncSession, p: Principal, *, topic: str, title: str | None, collection_ids: list[str] | None,
                     document_ids: list[str] | None, max_documents: int, include_graph: bool) -> Report:
        prefs = await self.memory.preferences_context(db, p)
        r = Report(org_id=p.org_id, user_id=p.user_id, title=(title or f"Research report: {topic}")[:300], topic=topic, status="queued",
                   params={"collection_ids": collection_ids, "document_ids": document_ids, "max_documents": max_documents,
                           "include_graph": include_graph, "preferences": prefs}, progress=[])
        db.add(r)
        await db.flush()
        await audit(db, "report.create", org_id=p.org_id, user_id=p.user_id, resource=r.id)
        return r

    def schedule(self, report_id: str, p: Principal) -> None:
        t = asyncio.create_task(self._run(report_id, {"user_id": p.user_id, "org_id": p.org_id, "role": p.role, "email": p.email}))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _progress(self, report_id: str, step: str, detail: str) -> None:
        async with self.deps.session_factory() as db:
            r = await db.get(Report, report_id)
            r.progress = [*(r.progress or []), {"step": step, "detail": detail, "at": datetime.now(timezone.utc).isoformat()}]
            await db.commit()

    async def _run(self, report_id: str, principal: dict) -> None:
        async with self._sem:
            sf = self.deps.session_factory
            async with sf() as db:
                r = await db.get(Report, report_id)
                topic, params, title = r.topic, dict(r.params), r.title
                r.status = "running"
                await db.commit()
            try:
                async def progress(step: str, detail: str) -> None:
                    await self._progress(report_id, step, detail)

                content = await ReportBuilder(self.deps).build(
                    principal, topic=topic, title=title, collection_ids=params.get("collection_ids"), document_ids=params.get("document_ids"),
                    max_documents=params.get("max_documents") or self.deps.settings.report_max_documents,
                    include_graph=params.get("include_graph", True), preferences=params.get("preferences"), progress=progress)
                status, error = "ready", None
            except Exception as exc:  # noqa: BLE001
                log.exception("report failed", extra={"extra_fields": {"report": report_id}})
                content, status, error = None, "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
            async with sf() as db:
                r = await db.get(Report, report_id)
                r.status, r.content, r.error = status, content, error
                await db.commit()

    async def recover(self) -> int:
        async with self.deps.session_factory() as db:
            res = await db.execute(update(Report).where(Report.status.in_(("queued", "running"))).values(
                status="failed", error="interrupted: the server restarted while this report was being generated"))
            await db.commit()
            return res.rowcount or 0

    async def drain(self, timeout: float = 60.0) -> None:
        if self._tasks:
            await asyncio.wait_for(asyncio.gather(*list(self._tasks), return_exceptions=True), timeout)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
