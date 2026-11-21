from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_role
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import NotFound, ValidationFailed
from app.database.base import utcnow
from app.database.models import LLMCall, PromptVersion
from app.distributed.bus import TOPIC_PROMPTS
from app.llmops.prompts import sha

router = APIRouter(prefix="/admin/llm", tags=["llmops"])


def _ops(request: Request) -> dict:
    return request.app.state.llmops


@router.get("/models")
async def models(request: Request, p: Principal = Depends(require_role("admin"))):
    """Routing table + circuit-breaker health. Never includes keys (config only names environment variables)."""
    r = request.app.state.llm
    cfg = r.cfg
    return {"models": r.health(), "default_chain": cfg.default_chain, "task_chains": cfg.task_chains,
            "budget": cfg.budget.model_dump(), "cache": (_ops(request)["cache"].stats() if _ops(request)["cache"] else None)}


@router.get("/usage")
async def usage(request: Request, days: int = Query(7, ge=1, le=90), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    await _ops(request)["recorder"].flush()
    since = utcnow() - timedelta(days=days)
    base = (LLMCall.org_id == p.org_id, LLMCall.created_at >= since)

    async def grouped(col):
        q = select(col, func.count(), func.coalesce(func.sum(LLMCall.prompt_tokens), 0), func.coalesce(func.sum(LLMCall.completion_tokens), 0),
                   func.coalesce(func.sum(LLMCall.cost_usd), 0.0), func.coalesce(func.avg(LLMCall.latency_ms), 0.0)).where(*base).group_by(col).order_by(func.sum(LLMCall.cost_usd).desc(), col)
        return [{"key": k, "calls": n, "prompt_tokens": int(pt), "completion_tokens": int(ct), "cost_usd": round(float(c), 6), "avg_latency_ms": round(float(l), 1)}
                for k, n, pt, ct, c, l in (await db.execute(q)).all()]

    tot = (await db.execute(select(func.count(), func.coalesce(func.sum(LLMCall.prompt_tokens), 0), func.coalesce(func.sum(LLMCall.completion_tokens), 0),
                                   func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(*base))).one()
    status = {k: n for k, n in (await db.execute(select(LLMCall.status, func.count()).where(*base).group_by(LLMCall.status))).all()}
    calls_ok = status.get("ok", 0)
    hits = status.get("cache_hit", 0)
    day = func.substr(func.cast(LLMCall.created_at, type_=__import__("sqlalchemy").String), 1, 10)
    spent = await _ops(request)["recorder"].spent_today(p.org_id)
    b = request.app.state.llm.cfg.budget
    return {"days": days, "totals": {"calls": tot[0], "prompt_tokens": int(tot[1]), "completion_tokens": int(tot[2]), "cost_usd": round(float(tot[3]), 6)},
            "status": status, "cache_hit_rate": round(hits / (hits + calls_ok), 3) if hits + calls_ok else None,
            "by_model": await grouped(LLMCall.model), "by_task": await grouped(LLMCall.task), "by_prompt_version": await grouped(LLMCall.prompt_version),
            "budget": {"daily_usd": b.daily_usd_per_org, "spent_today_usd": round(spent, 6), "degrade_at": b.degrade_at,
                       "state": "unlimited" if not b.daily_usd_per_org else "exhausted" if spent >= b.daily_usd_per_org else "degraded" if spent >= b.daily_usd_per_org * b.degrade_at else "ok"}}


# ------------------------------------------------------------------------------------------------ cache
@router.delete("/cache")
async def clear_cache(request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cache = _ops(request)["cache"]
    if cache is None:
        raise NotFound("The LLM cache is disabled (NEXUS_LLM_CACHE_ENABLED=false)")
    n = await cache.clear()
    await audit(db, "llm.cache_clear", org_id=p.org_id, user_id=p.user_id, entries=n)
    return {"cleared": n}


# ------------------------------------------------------------------------------------------------ prompt versions
class PromptIn(BaseModel):
    task: str = Field(min_length=2, max_length=40)
    text: str = Field(min_length=10, max_length=20_000)
    note: str = Field(default="", max_length=300)
    activate: bool = False


class ActivateIn(BaseModel):
    version: int | Literal["builtin"]


@router.get("/prompts")
async def list_prompts(request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    store = _ops(request)["prompts"]
    rows = (await db.execute(select(PromptVersion).where(PromptVersion.org_id == p.org_id).order_by(PromptVersion.task, PromptVersion.version))).scalars().all()
    out = []
    for task, text in sorted(store.builtin.items()):
        vs = [r for r in rows if r.task == task]
        active = next((str(r.version) for r in vs if r.is_active), "builtin")
        out.append({"task": task, "active": active, "builtin_sha": sha(text),
                    "versions": [{"version": r.version, "note": r.note, "is_active": r.is_active, "sha": sha(r.text), "created_at": r.created_at.isoformat()} for r in vs]})
    return out


@router.get("/prompts/{task}/versions/{version}")
async def get_prompt(task: str, version: str, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    store = _ops(request)["prompts"]
    if version == "builtin":
        if task not in store.builtin:
            raise NotFound("Unknown prompt task")
        return {"task": task, "version": "builtin", "text": store.builtin[task]}
    row = (await db.execute(select(PromptVersion).where(PromptVersion.org_id == p.org_id, PromptVersion.task == task,
                                                         PromptVersion.version == int(version)))).scalar_one_or_none() if version.isdigit() else None
    if row is None:
        raise NotFound("Prompt version not found")
    return {"task": task, "version": row.version, "text": row.text, "note": row.note, "is_active": row.is_active}


@router.post("/prompts", status_code=201)
async def create_prompt(body: PromptIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    row = await _ops(request)["prompts"].create(db, p.org_id, p.user_id, body.task, body.text, body.note, body.activate)
    await audit(db, "llm.prompt_create", org_id=p.org_id, user_id=p.user_id, resource=row.id, task=body.task, version=row.version, activated=body.activate,
                sha=sha(body.text))
    await db.commit()
    await request.app.state.bus.publish(TOPIC_PROMPTS, {"org_id": p.org_id})
    return {"task": body.task, "version": row.version, "is_active": row.is_active}


@router.post("/prompts/{task}/activate")
async def activate_prompt(task: str, body: ActivateIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Activate an override, or `"builtin"` to roll back to the shipped prompt."""
    label = await _ops(request)["prompts"].activate(db, p.org_id, task, body.version)
    await audit(db, "llm.prompt_activate", org_id=p.org_id, user_id=p.user_id, task=task, active=label)
    await db.commit()
    await request.app.state.bus.publish(TOPIC_PROMPTS, {"org_id": p.org_id})
    return {"task": task, "active": label}
