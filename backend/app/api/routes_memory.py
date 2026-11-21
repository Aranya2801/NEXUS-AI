from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.auth.principal import Principal
from app.auth.service import audit
from app.database.models import Conversation
from app.memory.service import MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


def _mem(request: Request) -> MemoryService:
    return request.app.state.memory


class PreferenceIn(BaseModel):
    value: Any


class ContextIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=1000)


class FindingIn(BaseModel):
    chunk_id: str = Field(max_length=64)
    note: str = Field(default="", max_length=500)


class PinIn(BaseModel):
    document_id: str = Field(max_length=64)


@router.get("")
async def list_memory(request: Request, kind: str | None = None, scope_id: str | None = None,
                      p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    """Everything the system remembers about the caller, for transparency. Admins cannot read other users' memory."""
    return await _mem(request).list_items(db, p, kind, scope_id)


@router.put("/preferences/{key}")
async def set_preference(key: str, body: PreferenceIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).set_preference(db, p, key, body.value)


@router.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: str, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await _mem(request).delete_item(db, p, item_id)


@router.delete("")
async def forget(request: Request, kind: str | None = None, include_conversations: bool = False,
                 p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    """User-controlled deletion. `include_conversations=true` also erases conversations, runs and workflow checkpoints."""
    n = await _mem(request).delete_all(db, p, kind)
    convs = 0
    if include_conversations:
        await db.commit()
        ids = list((await db.execute(select(Conversation.id).where(Conversation.org_id == p.org_id, Conversation.user_id == p.user_id))).scalars())
        for cid in ids:
            await request.app.state.runner_agents.delete_conversation(p, cid)
        convs = len(ids)
    await audit(db, "memory.forget", org_id=p.org_id, user_id=p.user_id, kind=kind, items=n, conversations=convs)
    return {"deleted_items": n, "deleted_conversations": convs}


@router.get("/documents/recent")
async def recent_documents(request: Request, limit: int = Query(20, ge=1, le=100), p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).recent_documents(db, p, limit)


@router.post("/research-contexts", status_code=201)
async def create_context(body: ContextIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).create_context(db, p, body.name, body.goal)


@router.get("/research-contexts")
async def list_contexts(request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).list_contexts(db, p)


@router.get("/research-contexts/{context_id}")
async def get_context(context_id: str, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).get_context(db, p, context_id)


@router.post("/research-contexts/{context_id}/findings", status_code=201)
async def add_finding(context_id: str, body: FindingIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await _mem(request).add_finding(db, p, context_id, body.chunk_id, body.note)


@router.post("/research-contexts/{context_id}/pin", status_code=204)
async def pin_document(context_id: str, body: PinIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await _mem(request).pin_document(db, p, context_id, body.document_id)


@router.delete("/research-contexts/{context_id}", status_code=204)
async def delete_context(context_id: str, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await _mem(request).delete_context(db, p, context_id)
