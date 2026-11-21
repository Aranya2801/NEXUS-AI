from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal, budget_guard
from app.auth.principal import Principal
from app.core.exceptions import NotFound
from app.database.models import Conversation, Message
from app.workflows.runner import ResearchRunner

router = APIRouter(tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    stream: bool = False
    review: Literal["auto", "always"] = "auto"
    research_context_id: str | None = Field(default=None, max_length=64)


class ResumeIn(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    answer: str | None = Field(default=None, max_length=8000)
    note: str | None = Field(default=None, max_length=500)


def _runner(request: Request) -> ResearchRunner:
    return request.app.state.runner_agents


def _sse(events: AsyncIterator[dict]) -> StreamingResponse:
    async def gen():
        async for ev in events:
            yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/chat")
async def chat(body: ChatIn, request: Request, p: Principal = Depends(budget_guard)):
    runner = _runner(request)
    kw = dict(conversation_id=body.conversation_id, collection_ids=body.collection_ids, review=body.review,
              research_context_id=body.research_context_id)
    if body.stream:
        return _sse(runner.stream(p, body.message, **kw))
    return await runner.run(p, body.message, **kw)


@router.get("/agents/runs")
async def list_runs(request: Request, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(get_principal)):
    return await _runner(request).list_runs(p, limit)


@router.get("/agents/runs/{run_id}")
async def inspect_run(run_id: str, request: Request, p: Principal = Depends(get_principal)):
    return await _runner(request).inspect(p, run_id)


@router.post("/agents/runs/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeIn, request: Request, stream: bool = False, p: Principal = Depends(get_principal)):
    runner = _runner(request)
    if stream:
        return _sse(runner.resume_stream(p, run_id, body.decision, body.answer, body.note))
    return await runner.resume(p, run_id, body.decision, body.answer, body.note)


@router.get("/conversations")
async def list_conversations(limit: int = Query(50, ge=1, le=200), p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Conversation).where(Conversation.org_id == p.org_id, Conversation.user_id == p.user_id)
                             .order_by(Conversation.updated_at.desc()).limit(limit))).scalars().all()
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.org_id != p.org_id or conv.user_id != p.user_id:
        raise NotFound("Conversation not found")
    rows = (await db.execute(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id))).scalars().all()
    return [{"id": m.id, "role": m.role, "content": m.content, "citations": m.citations, "verification": m.verification,
             "created_at": m.created_at.isoformat()} for m in rows]


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, request: Request, p: Principal = Depends(get_principal)):
    await _runner(request).delete_conversation(p, conversation_id)
