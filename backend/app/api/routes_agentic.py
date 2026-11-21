from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.sandbox import SandboxViolation, run_python
from app.agentic.tools import ToolContext
from app.api.deps import get_db, get_principal, require_role, budget_guard
from app.auth.access import accessible_collection_ids
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import NotFound, PermissionDenied, ValidationFailed
from app.database.base import utcnow
from app.database.models import AutonomousRun

router = APIRouter(prefix="/agentic", tags=["agentic"])


class RunIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    collection_ids: list[str] | None = Field(default=None, max_length=50)
    allow_code: bool = Field(default=False, description="Let the planner use the sandboxed python tool (needs editor role + server flag)")
    max_steps: int | None = Field(default=None, ge=1, le=50)


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=8000)


def _out(r: AutonomousRun) -> dict:
    return {"id": r.id, "goal": r.goal, "status": r.status, "options": r.options, "result": r.result, "error": r.error, "created_at": r.created_at.isoformat()}


@router.get("/tools")
async def tools(request: Request, allow_code: bool = False, p: Principal = Depends(get_principal)):
    reg, s = request.app.state.tool_registry, request.app.state.settings
    return {"tools": [t.spec() for t in reg.visible(p.role, s, allow_code)], "code_execution_enabled": s.agentic_code_execution_enabled,
            "limits": {"max_waves": s.agentic_max_waves, "max_steps": s.agentic_max_steps, "run_timeout_seconds": s.agentic_run_timeout_seconds}}


@router.post("/runs", status_code=201)
async def create_run(body: RunIn, request: Request, p: Principal = Depends(budget_guard), db: AsyncSession = Depends(get_db)):
    st, s = request.app.state, request.app.state.settings
    if body.allow_code and not (s.agentic_code_execution_enabled and p.at_least("editor")):
        raise PermissionDenied("Code execution is disabled on this server or requires the editor role")
    if body.collection_ids is not None:
        allowed = await accessible_collection_ids(db, p)
        if any(c not in allowed for c in body.collection_ids):
            raise NotFound("Collection not found")      # same answer for missing and hidden
    run = AutonomousRun(org_id=p.org_id, user_id=p.user_id, goal=body.goal, options=body.model_dump(exclude={"goal"}))
    db.add(run)
    await db.commit()
    ctx = ToolContext(st.agent_deps, p.user_id, p.org_id, body.collection_ids, body.allow_code)
    try:
        result = await asyncio.wait_for(st.autonomous.run(body.goal, ctx, p.role, max_steps=body.max_steps), s.agentic_run_timeout_seconds + 15)
        run.status, run.result = "completed", result
    except asyncio.TimeoutError:
        run.status, run.error = "failed", "run exceeded the wall-clock budget"
    except Exception as exc:  # noqa: BLE001
        run.status, run.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
    if st.metrics is not None:
        st.metrics.inc("nexus_agentic_runs_total", run.status)
    await audit(db, "agentic.run", org_id=p.org_id, user_id=p.user_id, resource=run.id, status=run.status, code=body.allow_code,
                steps=len((run.result or {}).get("steps", [])), goal_sha=hashlib.sha256(body.goal.encode()).hexdigest()[:16])
    await db.commit()
    return _out(run)


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100), p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    q = select(AutonomousRun).where(AutonomousRun.org_id == p.org_id)
    if not p.at_least("admin"):
        q = q.where(AutonomousRun.user_id == p.user_id)
    rows = (await db.execute(q.order_by(AutonomousRun.created_at.desc()).limit(limit))).scalars().all()
    return [{k: v for k, v in _out(r).items() if k != "result"} | {"answer_status": (r.result or {}).get("answer_status")} for r in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    r = await db.get(AutonomousRun, run_id)
    if r is None or r.org_id != p.org_id or (r.user_id != p.user_id and not p.at_least("admin")):
        raise NotFound("Run not found")
    return _out(r)


@router.post("/code")
async def run_code(body: CodeIn, request: Request, p: Principal = Depends(require_role("editor")), db: AsyncSession = Depends(get_db)):
    """Direct sandbox execution (feature-flagged, editor+). Audited by code hash, never by content."""
    s = request.app.state.settings
    if not s.agentic_code_execution_enabled:
        raise PermissionDenied("Code execution is disabled on this server (NEXUS_AGENTIC_CODE_EXECUTION_ENABLED)")
    try:
        r = await run_python(body.code, timeout=s.agentic_sandbox_timeout_seconds, memory_mb=s.agentic_sandbox_memory_mb)
    except SandboxViolation as exc:
        await audit(db, "agentic.code_rejected", org_id=p.org_id, user_id=p.user_id, code_sha=hashlib.sha256(body.code.encode()).hexdigest()[:16])
        await db.commit()     # the rejection must be recorded even though the request fails
        raise ValidationFailed(f"Rejected by sandbox: {exc}") from exc
    await audit(db, "agentic.code", org_id=p.org_id, user_id=p.user_id, code_sha=hashlib.sha256(body.code.encode()).hexdigest()[:16], ok=r.ok, killed=r.killed)
    return {"ok": r.ok, "stdout": r.stdout, "error": r.error, "killed": r.killed, "duration_ms": r.duration_ms}
