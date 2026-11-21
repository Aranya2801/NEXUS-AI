from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_role
from app.auth.access import require_collection
from app.auth.principal import Principal
from app.auth.service import audit
from app.connectors import registry
from app.connectors.security import encrypt_secrets
from app.core.exceptions import Conflict, NotFound
from app.database.models import Connector, ConnectorItem, ConnectorRun

router = APIRouter(prefix="/connectors", tags=["connectors"])


class ConnectorIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: Literal["google_drive", "sharepoint", "slack", "teams", "sql", "rest"]
    collection_id: str
    config: dict = {}
    secrets: dict = Field(default={}, description="Stored encrypted; never returned")
    acl_mode: Literal["strict", "inherit"] = "strict"
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)


class ConnectorPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    acl_mode: Literal["strict", "inherit"] | None = None
    secrets: dict | None = None


class ConnectorOut(BaseModel):
    id: str
    name: str
    kind: str
    collection_id: str
    config: dict
    acl_mode: str
    enabled: bool
    interval_minutes: int | None
    last_sync_at: datetime | None
    has_cursor: bool
    last_run: dict | None = None


async def _out(db: AsyncSession, c: Connector) -> ConnectorOut:
    run = (await db.execute(select(ConnectorRun).where(ConnectorRun.connector_id == c.id).order_by(ConnectorRun.started_at.desc()).limit(1))).scalar_one_or_none()
    cfg = {k: v for k, v in c.config.items()}
    return ConnectorOut(id=c.id, name=c.name, kind=c.kind, collection_id=c.collection_id, config=cfg, acl_mode=c.acl_mode, enabled=c.enabled,
                        interval_minutes=c.interval_minutes, last_sync_at=c.last_sync_at, has_cursor=bool(c.cursor),
                        last_run=None if run is None else {"id": run.id, "status": run.status, "stats": run.stats, "error": run.error,
                                                           "started_at": run.started_at.isoformat(),
                                                           "finished_at": run.finished_at.isoformat() if run.finished_at else None})


async def _get(db: AsyncSession, p: Principal, connector_id: str) -> Connector:
    c = await db.get(Connector, connector_id)
    if c is None or c.org_id != p.org_id:
        raise NotFound("Connector not found")
    return c


@router.get("/kinds")
async def kinds(p: Principal = Depends(require_role("admin"))):
    return {"kinds": [{"kind": k, "config_schema": registry.CONFIGS[k].model_json_schema(), "secrets": list(registry.SECRET_KEYS[k])} for k in registry.KINDS]}


@router.post("", response_model=ConnectorOut, status_code=201)
async def create_connector(body: ConnectorIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    s = request.app.state.settings
    await require_collection(db, p, body.collection_id, write=True)
    if (await db.execute(select(Connector.id).where(Connector.org_id == p.org_id, Connector.name == body.name))).first():
        raise Conflict("Connector name already exists")
    cfg, sec = registry.validate(body.kind, body.config, body.secrets, s)
    c = Connector(org_id=p.org_id, collection_id=body.collection_id, created_by=p.user_id, kind=body.kind, name=body.name, config=cfg,
                  secret_enc=encrypt_secrets(s, sec), acl_mode=body.acl_mode, interval_minutes=body.interval_minutes)
    db.add(c)
    await db.flush()
    await audit(db, "connector.create", org_id=p.org_id, user_id=p.user_id, resource=c.id, kind=body.kind, acl_mode=body.acl_mode)
    return await _out(db, c)


@router.get("", response_model=list[ConnectorOut])
async def list_connectors(p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Connector).where(Connector.org_id == p.org_id).order_by(Connector.created_at))).scalars().all()
    return [await _out(db, c) for c in rows]


@router.get("/{connector_id}", response_model=ConnectorOut)
async def get_connector(connector_id: str, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    return await _out(db, await _get(db, p, connector_id))


@router.patch("/{connector_id}", response_model=ConnectorOut)
async def patch_connector(connector_id: str, body: ConnectorPatch, request: Request, p: Principal = Depends(require_role("admin")),
                          db: AsyncSession = Depends(get_db)):
    c = await _get(db, p, connector_id)
    s = request.app.state.settings
    changes = body.model_dump(exclude_unset=True)
    if "secrets" in changes and changes["secrets"] is not None:
        _, sec = registry.validate(c.kind, c.config, changes.pop("secrets"), s)
        c.secret_enc = encrypt_secrets(s, sec)
        await audit(db, "connector.rotate_secret", org_id=p.org_id, user_id=p.user_id, resource=c.id)
    changes.pop("secrets", None)
    for k, v in changes.items():
        setattr(c, k, v)
    if changes:
        await audit(db, "connector.update", org_id=p.org_id, user_id=p.user_id, resource=c.id, **{k: v for k, v in changes.items()})
    return await _out(db, c)


@router.post("/{connector_id}/sync", status_code=202)
async def sync_now(connector_id: str, request: Request, reset: bool = Query(False, description="Forget the watermark and re-scan everything"),
                   p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    c = await _get(db, p, connector_id)
    if not c.enabled:
        raise Conflict("Connector is disabled")
    if reset:
        c.cursor = None
    await audit(db, "connector.sync_requested", org_id=p.org_id, user_id=p.user_id, resource=c.id)
    await db.commit()
    request.app.state.connectors.trigger(c.id)
    return {"status": "accepted", "connector_id": c.id}


@router.get("/{connector_id}/runs")
async def runs(connector_id: str, limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    c = await _get(db, p, connector_id)
    rows = (await db.execute(select(ConnectorRun).where(ConnectorRun.connector_id == c.id).order_by(ConnectorRun.started_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "status": r.status, "stats": r.stats, "error": r.error, "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]


@router.get("/{connector_id}/items")
async def items(connector_id: str, status: str | None = None, limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require_role("admin")),
                db: AsyncSession = Depends(get_db)):
    c = await _get(db, p, connector_id)
    q = select(ConnectorItem).where(ConnectorItem.connector_id == c.id)
    if status:
        q = q.where(ConnectorItem.status == status)
    rows = (await db.execute(q.order_by(ConnectorItem.synced_at.desc()).limit(limit))).scalars().all()
    return [{"external_id": r.external_id, "name": r.name, "status": r.status, "detail": r.detail, "document_id": r.document_id, "url": r.url,
             "external_acl": r.external_acl} for r in rows]


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(connector_id: str, delete_documents: bool = Query(False), request: Request = None,
                           p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    c = await _get(db, p, connector_id)
    if delete_documents:
        from app.database.models import Document

        for it in (await db.execute(select(ConnectorItem).where(ConnectorItem.connector_id == c.id))).scalars():
            doc = await db.get(Document, it.document_id) if it.document_id else None
            if doc is not None and doc.org_id == p.org_id:
                await request.app.state.ingestion.delete_document(db, p.org_id, doc)
    await db.execute(delete(ConnectorItem).where(ConnectorItem.connector_id == c.id))
    await db.execute(delete(ConnectorRun).where(ConnectorRun.connector_id == c.id))
    await db.delete(c)
    await audit(db, "connector.delete", org_id=p.org_id, user_id=p.user_id, resource=connector_id, delete_documents=delete_documents)
