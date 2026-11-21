"""Security & governance admin API (org admins only, org-scoped, every write audited)."""
from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_role
from app.auth.principal import Principal
from app.auth.service import audit
from app.database.base import utcnow
from app.database.models import AuditLog
from app.distributed.bus import TOPIC_POLICY
from app.security import injection, pii
from app.security.auditsig import verify_row
from app.security.policy import Policy

router = APIRouter(prefix="/admin/security", tags=["security"])


@router.get("/policy")
async def get_policy(request: Request, p: Principal = Depends(require_role("admin"))):
    return (await request.app.state.policies.get(p.org_id)).model_dump()


@router.put("/policy")
async def put_policy(body: Policy, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    store = request.app.state.policies
    old = (await store.get(p.org_id)).model_dump()
    await store.put(db, p.org_id, p.user_id, body)
    new = body.model_dump()
    await audit(db, "security.policy_update", org_id=p.org_id, user_id=p.user_id, changes={k: [old[k], new[k]] for k in new if old.get(k) != new[k]})
    await db.commit()                                              # replicas re-read the row on invalidation, so it must be durable first
    await request.app.state.bus.publish(TOPIC_POLICY, {"org_id": p.org_id})
    return new


class ScanIn(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)


@router.post("/scan")
async def scan(body: ScanIn, request: Request, p: Principal = Depends(require_role("admin"))):
    """Dry-run the detectors against a sample. Returns kinds/counts and the REDACTED text only – detected values are never echoed back."""
    pol = await request.app.state.policies.get(p.org_id)
    redacted, found = pii.redact(body.text, pol.pii_kinds)
    return {"findings": found, "redacted": redacted, "instruction_like": injection.scan(body.text), "kinds_checked": pol.pii_kinds}


@router.get("/events")
async def events(days: int = Query(7, ge=1, le=90), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    since = utcnow() - timedelta(days=days)
    rows = (await db.execute(select(AuditLog.action, func.count()).where(AuditLog.org_id == p.org_id, AuditLog.created_at >= since,
                                                                          (AuditLog.action.like("policy.%")) | (AuditLog.action.like("security.%")))
                             .group_by(AuditLog.action))).all()
    return {"days": days, "events": {a: n for a, n in rows}}


@router.get("/audit/verify")
async def verify_audit(limit: int = Query(5000, ge=1, le=50_000), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Recomputes each row's HMAC. Detects edited rows; cannot detect deleted ones (see docs/security/governance.md)."""
    rows = (await db.execute(select(AuditLog).where(AuditLog.org_id == p.org_id).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit))).scalars().all()
    res = {"ok": 0, "unsigned": 0, "tampered": 0}
    bad = []
    for r in rows:
        v = verify_row(r)
        res[v] += 1
        if v == "tampered":
            bad.append(r.id)
    return {"checked": len(rows), **res, "tampered_ids": bad[:100]}


@router.get("/audit/export", response_class=PlainTextResponse)
async def export_audit(format: str = Query("csv", pattern="^(csv|jsonl)$"), days: int = Query(30, ge=1, le=3650), request: Request = None,
                       p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    since = utcnow() - timedelta(days=days)
    rows = (await db.execute(select(AuditLog).where(AuditLog.org_id == p.org_id, AuditLog.created_at >= since).order_by(AuditLog.created_at, AuditLog.id).limit(100_000))).scalars().all()
    await audit(db, "security.audit_export", org_id=p.org_id, user_id=p.user_id, rows=len(rows), format=format)
    if format == "jsonl":
        body = "\n".join(json.dumps({"id": r.id, "at": r.created_at.isoformat(), "user_id": r.user_id, "action": r.action, "resource": r.resource,
                                     "detail": r.detail, "sig": r.sig}, ensure_ascii=False) for r in rows)
        return PlainTextResponse(body, media_type="application/x-ndjson")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "created_at", "user_id", "action", "resource", "detail", "sig"])
    for r in rows:
        w.writerow([r.id, r.created_at.isoformat(), r.user_id or "", _csv_safe(r.action), _csv_safe(r.resource or ""),
                    _csv_safe(json.dumps(r.detail, ensure_ascii=False)), r.sig or ""])
    return PlainTextResponse(out.getvalue(), media_type="text/csv")


def _csv_safe(v: str) -> str:
    """Neutralise spreadsheet formula injection (values starting with = + - @)."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
