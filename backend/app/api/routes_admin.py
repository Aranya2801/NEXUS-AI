"""Administrative monitoring (org admins only, always scoped to the admin's own organisation)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_role
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import Conflict, NotFound
from app.database.models import (AgentRun, AuditLog, Chunk, Collection, Conversation, Document, Feedback, IngestionJob, Report, User)

router = APIRouter(prefix="/admin", tags=["admin"])


class UserPatch(BaseModel):
    role: Literal["admin", "editor", "viewer"] | None = None
    is_active: bool | None = None


async def _group(db: AsyncSession, col, org_col, org_id: str) -> dict[str, int]:
    return {k: n for k, n in (await db.execute(select(col, func.count()).where(org_col == org_id).group_by(col))).all()}


async def _count(db: AsyncSession, model, org_id: str) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(model.org_id == org_id))).scalar_one()


@router.get("/overview")
async def overview(request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    o, s = p.org_id, request.app.state.settings
    fb = await _group(db, Feedback.rating, Feedback.org_id, o)
    return {
        "counts": {"users": await _count(db, User, o), "collections": await _count(db, Collection, o), "chunks": await _count(db, Chunk, o),
                   "conversations": await _count(db, Conversation, o)},
        "documents": await _group(db, Document.status, Document.org_id, o),
        "ingestion_jobs": await _group(db, IngestionJob.status, IngestionJob.org_id, o),
        "agent_runs": await _group(db, AgentRun.status, AgentRun.org_id, o),
        "reports": await _group(db, Report.status, Report.org_id, o),
        "graph": await request.app.state.graph.stats(db, o),
        "feedback": {"up": fb.get(1, 0), "down": fb.get(-1, 0)},
        "runtime": {"env": s.env, "llm": request.app.state.llm.name, "embedder": request.app.state.retriever.embedder.name,
                    "reranker": request.app.state.pipeline.reranker.name, "graph_extractor": request.app.state.graph.extractor.name,
                    "human_review_mode": s.human_review_mode, "max_retrieval_loops": s.max_retrieval_loops},
    }


@router.get("/users")
async def list_users(p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(User).where(User.org_id == p.org_id).order_by(User.created_at))).scalars().all()
    return [{"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role, "department": u.department, "is_active": u.is_active} for u in rows]


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user_id)
    if u is None or u.org_id != p.org_id:
        raise NotFound("User not found")
    new_role = body.role or u.role
    new_active = u.is_active if body.is_active is None else body.is_active
    if u.role == "admin" and u.is_active and (new_role != "admin" or not new_active):
        admins = (await db.execute(select(func.count()).select_from(User).where(User.org_id == p.org_id, User.role == "admin", User.is_active.is_(True)))).scalar_one()
        if admins <= 1:
            raise Conflict("An organisation must keep at least one active admin")
    changes = {k: v for k, v in (("role", new_role), ("is_active", new_active)) if getattr(u, k) != v}
    u.role, u.is_active = new_role, new_active  # tokens stay valid but are re-checked against these fields on every request
    await audit(db, "user.update", org_id=p.org_id, user_id=p.user_id, resource=u.id, **changes)
    return {"id": u.id, "email": u.email, "role": u.role, "is_active": u.is_active}


@router.get("/audit")
async def audit_log(action: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    q = select(AuditLog).where(AuditLog.org_id == p.org_id)
    if action:
        q = q.where(AuditLog.action == action)
    rows = (await db.execute(q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": r.id, "action": r.action, "user_id": r.user_id, "resource": r.resource, "detail": r.detail, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/jobs")
async def ingestion_jobs(status: str | None = None, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(require_role("admin")),
                         db: AsyncSession = Depends(get_db)):
    q = (select(IngestionJob, Document.title).join(Document, Document.id == IngestionJob.document_id).where(IngestionJob.org_id == p.org_id))
    if status:
        q = q.where(IngestionJob.status == status)
    rows = (await db.execute(q.order_by(IngestionJob.created_at.desc()).limit(limit))).all()
    return [{"id": j.id, "document_id": j.document_id, "title": t, "status": j.status, "stage": j.stage, "attempts": j.attempts,
             "max_attempts": j.max_attempts, "error": j.error, "updated_at": j.updated_at.isoformat()} for j, t in rows]


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, p: Principal = Depends(require_role("admin"))):
    """Prometheus text exposition (process-local). Admin-only: scrape with an admin API token, or keep it behind the proxy."""
    reg = request.app.state.metrics
    if reg is None:
        raise NotFound("Metrics are disabled (NEXUS_METRICS_ENABLED=false)")
    return PlainTextResponse(reg.render(), media_type="text/plain; version=0.0.4")


@router.get("/observability")
async def observability(request: Request, p: Principal = Depends(require_role("admin"))):
    s = request.app.state.settings
    return {"metrics": request.app.state.metrics is not None, "tracing": request.app.state.tracing,
            "otel_endpoint_configured": bool(s.otel_endpoint)}
