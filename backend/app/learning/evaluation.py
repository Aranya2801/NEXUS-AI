"""Org-scoped retrieval regression evaluation: run the org's saved cases (query -> expected documents) through the live retriever and score them with the
same metric code as the research harness. Use it to compare configurations (e.g. dense vs hybrid) or to catch regressions after re-embedding/ingestion changes."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.exceptions import ValidationFailed
from app.database.models import EvalCase, EvalRun
from app.evaluation.metrics import dedupe, paired_bootstrap, retrieval_metrics

METRICS = ("precision_at_5", "recall_at_5", "ndcg_at_5", "precision_at_10", "recall_at_10", "ndcg_at_10", "mrr")
MIN_CASES = 5


async def run_cases(db: AsyncSession, retriever, p: Principal, *, mode: str, top_k: int, label: str) -> EvalRun:
    cases = (await db.execute(select(EvalCase).where(EvalCase.org_id == p.org_id, EvalCase.is_active.is_(True)).order_by(EvalCase.created_at, EvalCase.id))).scalars().all()
    if not cases:
        raise ValidationFailed("No evaluation cases yet")
    per_case = []
    for c in cases:
        res = await retriever.search(db, p, c.query, mode=mode, top_k=max(top_k, 10))
        ranked = dedupe([h.document_id for h in res.hits])
        m = retrieval_metrics(ranked, set(c.expected_document_ids))
        per_case.append({"case_id": c.id, "ranked": ranked[:top_k], **{k: round(v, 4) for k, v in m.items()}})
    means = {k: round(sum(x[k] for x in per_case) / len(per_case), 4) for k in METRICS}
    run = EvalRun(org_id=p.org_id, label=label[:120], config={"mode": mode, "top_k": top_k}, metrics={**means, "cases": len(per_case)}, per_case=per_case, created_by=p.user_id)
    db.add(run)
    await db.flush()
    return run


def compare(a: EvalRun, b: EvalRun) -> dict:
    """Paired comparison of two runs over the cases both contain. Positive deltas mean `b` is better than `a`."""
    ai, bi = {x["case_id"]: x for x in a.per_case}, {x["case_id"]: x for x in b.per_case}
    shared = [c for c in ai if c in bi]
    out: dict = {"shared_cases": len(shared), "a": {"id": a.id, "label": a.label, "config": a.config}, "b": {"id": b.id, "label": b.label, "config": b.config}, "metrics": {}, "regressions": []}
    for m in ("mrr", "ndcg_at_10", "recall_at_10", "precision_at_5"):
        d = paired_bootstrap([bi[c][m] for c in shared], [ai[c][m] for c in shared], n=2000)
        out["metrics"][m] = {"a": round(sum(ai[c][m] for c in shared) / len(shared), 4) if shared else None, "b": round(sum(bi[c][m] for c in shared) / len(shared), 4) if shared else None,
                             "mean_diff": round(d["mean_diff"], 4), "ci_low": round(d["ci_low"], 4), "ci_high": round(d["ci_high"], 4)}
    out["regressions"] = [c for c in shared if bi[c]["mrr"] < ai[c]["mrr"]]
    out["improvements"] = [c for c in shared if bi[c]["mrr"] > ai[c]["mrr"]]
    out["warning"] = (f"Only {len(shared)} shared cases (< {MIN_CASES}): treat differences as anecdotes." if len(shared) < MIN_CASES
                      else "Confidence intervals are bootstrap intervals over cases; they say nothing about queries unlike your cases.")
    return out
