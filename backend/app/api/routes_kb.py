from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import schemas as s
from app.api.deps import get_db, get_ingestion, get_principal, get_runner, get_settings_dep, require_role
from app.auth.access import accessible_collection_ids, require_collection
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.config import Settings
from app.core.exceptions import Conflict, NotFound
from app.database.models import Chunk, Collection, CollectionAccess, Document, DocumentVersion, IngestionJob, User
from app.ingestion.runner import JobRunner
from app.ingestion.service import IngestionService

router = APIRouter(tags=["knowledge-base"])


@router.post("/collections", response_model=s.CollectionOut, status_code=201)
async def create_collection(body: s.CollectionIn, p: Principal = Depends(require_role("editor")), db: AsyncSession = Depends(get_db)):
    if (await db.execute(select(Collection.id).where(Collection.org_id == p.org_id, Collection.name == body.name))).first():
        raise Conflict("Collection name already exists")
    col = Collection(org_id=p.org_id, **body.model_dump())
    db.add(col)
    await db.flush()
    await audit(db, "collection.create", org_id=p.org_id, user_id=p.user_id, resource=col.id)
    return col


@router.get("/collections", response_model=list[s.CollectionOut])
async def list_collections(p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    ids = await accessible_collection_ids(db, p)
    if not ids:
        return []
    return (await db.execute(select(Collection).where(Collection.id.in_(ids)).order_by(Collection.name))).scalars().all()


@router.post("/collections/{collection_id}/access", status_code=204)
async def grant_access(collection_id: str, body: s.AccessGrantIn, p: Principal = Depends(require_role("admin")),
                       db: AsyncSession = Depends(get_db)):
    await require_collection(db, p, collection_id)
    user = await db.get(User, body.user_id)
    if user is None or user.org_id != p.org_id:
        raise NotFound("User not found")
    exists = (await db.execute(select(CollectionAccess.id).where(
        CollectionAccess.collection_id == collection_id, CollectionAccess.user_id == user.id))).first()
    if not exists:
        db.add(CollectionAccess(collection_id=collection_id, user_id=user.id))
        await audit(db, "collection.grant", org_id=p.org_id, user_id=p.user_id, resource=collection_id, grantee=user.id)


@router.post("/collections/{collection_id}/documents", response_model=s.UploadOut, status_code=202)
async def upload_document(
    collection_id: str, file: UploadFile = File(...), department: str | None = Form(default=None, max_length=100),
    p: Principal = Depends(require_role("editor")), db: AsyncSession = Depends(get_db),
    ingestion: IngestionService = Depends(get_ingestion), runner: JobRunner = Depends(get_runner),
    settings: Settings = Depends(get_settings_dep),
):
    data = await file.read(settings.max_upload_bytes + 1)  # bounded read: never buffer unbounded uploads
    res = await ingestion.submit(db, p, collection_id, file.filename or "", data, department)
    await db.commit()  # the job must be visible to the worker before it is enqueued
    if res.job:
        runner.enqueue(res.job.id)
    return s.UploadOut(outcome=res.outcome, document=s.DocumentOut.model_validate(res.document),
                       job=s.JobOut.model_validate(res.job) if res.job else None)


async def _visible_document(db: AsyncSession, p: Principal, document_id: str) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None or doc.org_id != p.org_id or doc.collection_id not in await accessible_collection_ids(db, p):
        raise NotFound("Document not found")
    return doc


@router.get("/documents", response_model=list[s.DocumentOut])
async def list_documents(collection_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    ids = await accessible_collection_ids(db, p)
    if collection_id:
        if collection_id not in ids:
            raise NotFound("Collection not found")
        ids = {collection_id}
    if not ids:
        return []
    q = select(Document).where(Document.org_id == p.org_id, Document.collection_id.in_(ids)).order_by(
        Document.created_at.desc()).limit(limit).offset(offset)
    return (await db.execute(q)).scalars().all()


@router.get("/documents/{document_id}", response_model=s.DocumentOut)
async def get_document(document_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _visible_document(db, p, document_id)


@router.get("/documents/{document_id}/chunks", response_model=list[s.ChunkOut])
async def document_chunks(document_id: str, request: Request, level: str = Query("child", pattern="^(child|parent)$"),
                          limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                          p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    """Source viewer: current-version chunks with page/section/offset provenance."""
    doc = await _visible_document(db, p, document_id)
    if offset == 0:
        await request.app.state.memory.record_document_interactions(db, p, [doc.id], "viewed")
    q = select(Chunk).where(Chunk.document_id == doc.id, Chunk.org_id == p.org_id, Chunk.is_current.is_(True),
                            Chunk.level == level).order_by(Chunk.ordinal).limit(limit).offset(offset)
    return (await db.execute(q)).scalars().all()


@router.get("/documents/{document_id}/job", response_model=s.JobOut)
async def document_job(document_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    doc = await _visible_document(db, p, document_id)
    job = (await db.execute(select(IngestionJob).where(IngestionJob.document_id == doc.id).order_by(
        IngestionJob.created_at.desc()))).scalars().first()
    if job is None:
        raise NotFound("No ingestion job")
    return job


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, p: Principal = Depends(require_role("editor")), db: AsyncSession = Depends(get_db),
                          ingestion: IngestionService = Depends(get_ingestion)):
    doc = await _visible_document(db, p, document_id)
    await ingestion.delete_document(db, p.org_id, doc)
    await audit(db, "document.delete", org_id=p.org_id, user_id=p.user_id, resource=document_id)
