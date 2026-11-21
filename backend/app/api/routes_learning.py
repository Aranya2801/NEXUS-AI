"""Continuous-learning admin API: consented feedback review, quality monitoring, retrieval regression evaluation, prompt A/B experiments.
Admin only, org-scoped, every change audited."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_role
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.database.base import utcnow
from app.database.models import AgentRun, Document, EvalCase, EvalRun, Experiment, Feedback, PromptVersion
from app.distributed.bus import TOPIC_EXPERIMENTS, TOPIC_PROMPTS
from app.learning import evaluation, quality
from app.learning.stats import two_proportion_z, wilson
from app.evaluation.metrics import holm

router = APIRouter(prefix="/admin", tags=["learning"])
MIN_RATED = 30


# ===================================================================== quality
@router.get("/quality")
async def quality_report(days: int = Query(14, ge=1, le=90), baseline_days: int = Query(14, ge=1, le=90), p: Principal = Depends(require_role("admin")),
                         db: AsyncSession = Depends(get_db)):
    return await quality.snapshot(db, p.org_id, days, baseline_days)


# ===================================================================== consented feedback
@router.get("/feedback")
async def reviewable_feedback(rating: int = Query(-1), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                              p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Only feedback whose author ticked 'allow admins to review' is listed – the question and answer of everyone else's runs stay private."""
    if rating not in (-1, 1):
        raise ValidationFailed("rating must be -1 or 1")
    rows = (await db.execute(select(Feedback, AgentRun).join(AgentRun, AgentRun.id == Feedback.run_id)
                             .where(Feedback.org_id == p.org_id, Feedback.allow_review.is_(True), Feedback.rating == rating)
                             .order_by(Feedback.created_at.desc()).limit(limit).offset(offset))).all()
    out = []
    for fb, run in rows:
        res = run.result or {}
        out.append({"run_id": run.id, "query": run.query, "answer": res.get("answer"), "answer_status": res.get("answer_status"), "reasons": fb.reasons, "comment": fb.comment,
                    "cited_document_ids": sorted({c["document_id"] for c in res.get("citations", [])}), "created_at": fb.created_at.isoformat()})
    return out


@router.get("/feedback/summary")
async def feedback_summary(days: int = Query(30, ge=1, le=365), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    from datetime import timedelta
    rows = (await db.execute(select(Feedback.rating, Feedback.reasons).where(Feedback.org_id == p.org_id, Feedback.created_at >= utcnow() - timedelta(days=days)))).all()
    reasons: dict[str, int] = {}
    for _, rs in rows:
        for r in rs or []:
            reasons[r] = reasons.get(r, 0) + 1
    up, down = sum(1 for r, _ in rows if r == 1), sum(1 for r, _ in rows if r == -1)
    return {"days": days, "up": up, "down": down, "down_rate": round(down / (up + down), 4) if up + down else None, "down_rate_ci95": [round(x, 4) for x in wilson(down, up + down)],
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}


# ===================================================================== evaluation cases / runs
class CaseIn(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    expected_document_ids: list[str] = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=300)


class CaseFromFeedbackIn(BaseModel):
    run_id: str = Field(max_length=64)
    expected_document_ids: list[str] = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=300)


async def _check_docs(db: AsyncSession, org_id: str, ids: list[str]) -> list[str]:
    ids = sorted(set(ids))
    found = set((await db.execute(select(Document.id).where(Document.org_id == org_id, Document.id.in_(ids)))).scalars())
    if found != set(ids):
        raise NotFound("Unknown document id(s)")              # same answer for missing and other-org documents
    return ids


def _case(c: EvalCase) -> dict:
    return {"id": c.id, "query": c.query, "expected_document_ids": c.expected_document_ids, "note": c.note, "source_run_id": c.source_run_id, "is_active": c.is_active,
            "created_at": c.created_at.isoformat()}


@router.post("/eval/cases", status_code=201)
async def create_case(body: CaseIn, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    ids = await _check_docs(db, p.org_id, body.expected_document_ids)
    c = EvalCase(org_id=p.org_id, query=body.query.strip(), expected_document_ids=ids, note=body.note, created_by=p.user_id)
    db.add(c)
    await db.flush()
    await audit(db, "eval.case_create", org_id=p.org_id, user_id=p.user_id, resource=c.id)
    return _case(c)


@router.post("/eval/cases/from-feedback", status_code=201)
async def case_from_feedback(body: CaseFromFeedbackIn, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Promote a consented thumbs-down run into a regression case. The admin supplies the documents that SHOULD have been found."""
    run = await db.get(AgentRun, body.run_id)
    consent = (await db.execute(select(Feedback.id).where(Feedback.run_id == body.run_id, Feedback.org_id == p.org_id, Feedback.allow_review.is_(True)))).first()
    if run is None or run.org_id != p.org_id or consent is None:
        raise NotFound("Run not found or its author did not allow review")
    ids = await _check_docs(db, p.org_id, body.expected_document_ids)
    c = EvalCase(org_id=p.org_id, query=run.query, expected_document_ids=ids, source_run_id=run.id, note=body.note, created_by=p.user_id)
    db.add(c)
    await db.flush()
    await audit(db, "eval.case_from_feedback", org_id=p.org_id, user_id=p.user_id, resource=c.id, run=run.id)
    return _case(c)


@router.get("/eval/cases")
async def list_cases(p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    return [_case(c) for c in (await db.execute(select(EvalCase).where(EvalCase.org_id == p.org_id).order_by(EvalCase.created_at))).scalars()]


@router.delete("/eval/cases/{case_id}")
async def delete_case(case_id: str, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    c = await db.get(EvalCase, case_id)
    if c is None or c.org_id != p.org_id:
        raise NotFound("Case not found")
    await db.delete(c)
    await audit(db, "eval.case_delete", org_id=p.org_id, user_id=p.user_id, resource=case_id)
    return {"deleted": case_id}


class EvalRunIn(BaseModel):
    mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    top_k: int = Field(default=10, ge=1, le=50)
    label: str = Field(default="", max_length=120)


@router.post("/eval/run", status_code=201)
async def run_eval(body: EvalRunIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    run = await evaluation.run_cases(db, request.app.state.retriever, p, mode=body.mode, top_k=body.top_k, label=body.label or f"{body.mode} k={body.top_k}")
    await audit(db, "eval.run", org_id=p.org_id, user_id=p.user_id, resource=run.id, mode=body.mode, cases=run.metrics["cases"])
    return {"id": run.id, "label": run.label, "config": run.config, "metrics": run.metrics,
            "warning": f"Only {run.metrics['cases']} cases: means are noisy." if run.metrics["cases"] < evaluation.MIN_CASES else None}


@router.get("/eval/runs")
async def list_eval_runs(limit: int = Query(30, ge=1, le=100), p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(EvalRun).where(EvalRun.org_id == p.org_id).order_by(EvalRun.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "label": r.label, "config": r.config, "metrics": r.metrics, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/eval/compare")
async def compare_eval_runs(a: str, b: str, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    ra, rb = await db.get(EvalRun, a), await db.get(EvalRun, b)
    if ra is None or rb is None or ra.org_id != p.org_id or rb.org_id != p.org_id:
        raise NotFound("Evaluation run not found")
    return evaluation.compare(ra, rb)


# ===================================================================== experiments
class VariantIn(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    prompt_version: int | Literal["builtin"]
    weight: float = Field(default=1.0, gt=0, le=100)


class ExperimentIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    task: str = Field(min_length=2, max_length=40)
    variants: list[VariantIn] = Field(min_length=2, max_length=5)

    @field_validator("variants")
    @classmethod
    def _unique(cls, v):
        if len({x.name for x in v}) != len(v):
            raise ValueError("variant names must be unique")
        if len({str(x.prompt_version) for x in v}) != len(v):
            raise ValueError("two variants use the same prompt version")
        return v


def _exp(e: Experiment) -> dict:
    return {"id": e.id, "name": e.name, "task": e.task, "variants": e.variants, "status": e.status, "winner": e.winner, "created_at": e.created_at.isoformat(),
            "ended_at": e.ended_at.isoformat() if e.ended_at else None}


@router.post("/experiments", status_code=201)
async def create_experiment(body: ExperimentIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    store = request.app.state.llmops["prompts"]
    if body.task not in store.builtin:
        raise ValidationFailed(f"Unknown prompt task '{body.task}'")
    for v in body.variants:
        if v.prompt_version != "builtin":
            ok = (await db.execute(select(PromptVersion.id).where(PromptVersion.org_id == p.org_id, PromptVersion.task == body.task, PromptVersion.version == v.prompt_version))).first()
            if ok is None:
                raise ValidationFailed(f"Variant '{v.name}': prompt version {v.prompt_version} does not exist for task '{body.task}'")
    clash = (await db.execute(select(Experiment.id).where(Experiment.org_id == p.org_id, Experiment.status == "running", Experiment.task == body.task))).first()
    if clash:
        raise Conflict(f"A running experiment already covers task '{body.task}' – stop or conclude it first")
    if (await db.execute(select(Experiment.id).where(Experiment.org_id == p.org_id, Experiment.name == body.name))).first():
        raise Conflict("An experiment with this name already exists")
    e = Experiment(org_id=p.org_id, name=body.name, task=body.task, variants=[v.model_dump() for v in body.variants], created_by=p.user_id)
    db.add(e)
    await db.flush()
    await audit(db, "experiment.create", org_id=p.org_id, user_id=p.user_id, resource=e.id, task=body.task)
    await db.commit()
    request.app.state.experiments.invalidate(p.org_id)
    await request.app.state.bus.publish(TOPIC_EXPERIMENTS, {"org_id": p.org_id})
    return _exp(e)


@router.get("/experiments")
async def list_experiments(p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    return [_exp(e) for e in (await db.execute(select(Experiment).where(Experiment.org_id == p.org_id).order_by(Experiment.created_at.desc()))).scalars()]


async def _own(db: AsyncSession, org_id: str, exp_id: str) -> Experiment:
    e = await db.get(Experiment, exp_id)
    if e is None or e.org_id != org_id:
        raise NotFound("Experiment not found")
    return e


@router.get("/experiments/{exp_id}")
async def experiment_results(exp_id: str, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    e = await _own(db, p.org_id, exp_id)
    end = e.ended_at or utcnow()
    runs = (await db.execute(select(AgentRun).where(AgentRun.org_id == p.org_id, AgentRun.created_at >= e.created_at, AgentRun.created_at <= end)
                             .order_by(AgentRun.created_at.desc()).limit(quality.MAX_RUNS))).scalars().all()
    mine = [r for r in runs if exp_id in (r.variants or {})]
    ratings = {r: v for r, v in (await db.execute(select(Feedback.run_id, Feedback.rating).where(Feedback.org_id == p.org_id, Feedback.run_id.in_([r.id for r in mine])))).all()} if mine else {}
    by: dict[str, list[dict]] = {v["name"]: [] for v in e.variants}
    for r in mine:
        by.setdefault(r.variants[exp_id]["variant"], []).append(quality.run_signals(r, ratings.get(r.id)))
    out = []
    for v in e.variants:
        s = quality.summarise(by[v["name"]])
        rated = [x for x in by[v["name"]] if x["rating"] is not None]
        down = sum(1 for x in rated if x["rating"] == -1)
        lo, hi = wilson(down, len(rated))
        out.append({"variant": v["name"], "prompt_version": v["prompt_version"], "weight": v["weight"], **s, "thumbs_down_ci95": [round(lo, 4), round(hi, 4)]})
    ctl = out[0]
    pv: dict[str, float] = {}
    cmp_rows = []
    for o in out[1:]:
        rc, rv = [x for x in by[ctl["variant"]] if x["rating"] is not None], [x for x in by[o["variant"]] if x["rating"] is not None]
        _, p_down = two_proportion_z(sum(x["rating"] == -1 for x in rv), len(rv), sum(x["rating"] == -1 for x in rc), len(rc))
        ac, av = [x for x in by[ctl["variant"]] if x["answered"]], [x for x in by[o["variant"]] if x["answered"]]
        _, p_ins = two_proportion_z(sum(x["insufficient"] for x in av), len(av), sum(x["insufficient"] for x in ac), len(ac))
        pv[f"{o['variant']}:thumbs_down"], pv[f"{o['variant']}:insufficient_evidence"] = p_down, p_ins
        cmp_rows.append({"variant": o["variant"], "vs": ctl["variant"]})
    adj = holm(pv) if pv else {}
    for c in cmp_rows:
        c["thumbs_down"] = {"p_raw": round(pv[f"{c['variant']}:thumbs_down"], 6), "p_holm": round(adj[f"{c['variant']}:thumbs_down"], 6)}
        c["insufficient_evidence"] = {"p_raw": round(pv[f"{c['variant']}:insufficient_evidence"], 6), "p_holm": round(adj[f"{c['variant']}:insufficient_evidence"], 6)}
    short = [o["variant"] for o in out if o["rated"] < MIN_RATED]
    if short:
        verdict = {"status": "insufficient_data", "detail": f"Need >= {MIN_RATED} rated runs per variant; short: {', '.join(short)}"}
    else:
        sig = [c for c in cmp_rows if c["thumbs_down"]["p_holm"] < 0.05]
        verdict = ({"status": "difference_detected", "detail": "Thumbs-down rate differs significantly (Holm-adjusted p < 0.05) for: " + ", ".join(c["variant"] for c in sig)}
                   if sig else {"status": "no_significant_difference", "detail": "No Holm-adjusted p < 0.05 on thumbs-down rate."})
    return {**_exp(e), "results": out, "comparisons": cmp_rows, "verdict": verdict,
            "notes": ["Runs are assigned to variants by hash, independently per run; analysis treats runs as independent (users with many runs weigh more).",
                      "Fixed-sample tests: repeatedly checking and stopping at the first significant result inflates false positives. Decide a sample size up front.",
                      "Thumbs are self-selected feedback, not a random sample of runs. Nothing is concluded automatically."]}


@router.post("/experiments/{exp_id}/stop")
async def stop_experiment(exp_id: str, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    e = await _own(db, p.org_id, exp_id)
    if e.status != "running":
        raise Conflict("Experiment is not running")
    e.status, e.ended_at = "stopped", utcnow()
    await audit(db, "experiment.stop", org_id=p.org_id, user_id=p.user_id, resource=e.id)
    await db.commit()
    request.app.state.experiments.invalidate(p.org_id)
    await request.app.state.bus.publish(TOPIC_EXPERIMENTS, {"org_id": p.org_id})
    return _exp(e)


class ConcludeIn(BaseModel):
    winner: str


@router.post("/experiments/{exp_id}/conclude")
async def conclude_experiment(exp_id: str, body: ConcludeIn, request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Human decision: ends the experiment and makes the chosen variant's prompt the org's active version for the task (rollback stays one call away)."""
    e = await _own(db, p.org_id, exp_id)
    if e.status != "running":
        raise Conflict("Experiment is not running")
    win = next((v for v in e.variants if v["name"] == body.winner), None)
    if win is None:
        raise ValidationFailed("winner must be one of the experiment's variants")
    label = await request.app.state.llmops["prompts"].activate(db, p.org_id, e.task, win["prompt_version"])
    e.status, e.winner, e.ended_at = "concluded", win["name"], utcnow()
    await audit(db, "experiment.conclude", org_id=p.org_id, user_id=p.user_id, resource=e.id, winner=win["name"], active_prompt=label)
    await db.commit()
    request.app.state.experiments.invalidate(p.org_id)
    await request.app.state.bus.publish(TOPIC_EXPERIMENTS, {"org_id": p.org_id})
    await request.app.state.bus.publish(TOPIC_PROMPTS, {"org_id": p.org_id})
    return {**_exp(e), "active_prompt": label}
