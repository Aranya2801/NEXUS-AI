from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal, budget_guard
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import Conflict, NotFound
from app.database.models import Report
from app.reporting import export
from app.reporting.access import redact_for

router = APIRouter(prefix="/reports", tags=["reports"])
CONTENT_TYPES = {"md": "text/markdown; charset=utf-8", "pdf": "application/pdf",
                 "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


class ReportIn(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    title: str | None = Field(default=None, max_length=300)
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    document_ids: list[str] | None = Field(default=None, max_length=100)
    max_documents: int | None = Field(default=None, ge=1, le=15)
    include_graph: bool = True


async def _owned(db: AsyncSession, p: Principal, report_id: str) -> Report:
    r = await db.get(Report, report_id)
    if r is None or r.org_id != p.org_id or (r.user_id != p.user_id and p.role != "admin"):
        raise NotFound("Report not found")
    return r


@router.post("", status_code=202)
async def create_report(body: ReportIn, request: Request, p: Principal = Depends(budget_guard), db: AsyncSession = Depends(get_db)):
    svc = request.app.state.reports
    r = await svc.create(db, p, topic=body.topic, title=body.title, collection_ids=body.collection_ids, document_ids=body.document_ids,
                         max_documents=body.max_documents or request.app.state.settings.report_max_documents, include_graph=body.include_graph)
    await db.commit()  # the background task reads the row from its own session
    svc.schedule(r.id, p)
    return {"id": r.id, "status": r.status, "title": r.title}


@router.get("")
async def list_reports(limit: int = Query(50, ge=1, le=200), p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    q = select(Report).where(Report.org_id == p.org_id)
    if p.role != "admin":
        q = q.where(Report.user_id == p.user_id)
    rows = (await db.execute(q.order_by(Report.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "title": r.title, "topic": r.topic, "status": r.status, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/{report_id}")
async def get_report(report_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    r = await _owned(db, p, report_id)
    content = await redact_for(db, p, r.content) if r.content else None
    return {"id": r.id, "title": r.title, "topic": r.topic, "status": r.status, "progress": r.progress, "error": r.error, "content": content,
            "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()}


@router.get("/{report_id}/export")
async def export_report(report_id: str, request: Request, format: Literal["md", "docx", "pdf"] = "pdf",
                        p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    r = await _owned(db, p, report_id)
    if r.status != "ready" or not r.content:
        raise Conflict(f"Report is {r.status}; only ready reports can be exported")
    content = await redact_for(db, p, r.content)  # same redaction as the JSON view
    if format == "md":
        data = export.to_markdown(content).encode("utf-8")
    elif format == "docx":
        data = export.to_docx(content)
    else:
        data = export.to_pdf(content, request.app.state.settings.pdf_font_path)
    await audit(db, "report.export", org_id=p.org_id, user_id=p.user_id, resource=report_id, format=format, bytes=len(data))
    return Response(data, media_type=CONTENT_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{export.safe_filename(r.title, format)}"', "Cache-Control": "no-store"})


@router.delete("/{report_id}", status_code=204)
async def delete_report(report_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    r = await _owned(db, p, report_id)
    await db.delete(r)
    await audit(db, "report.delete", org_id=p.org_id, user_id=p.user_id, resource=report_id)
