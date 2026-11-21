"""Automated quality monitoring from signals the system already records for every run: answer status (grounded / partial / insufficient evidence), retrieval
confidence, unsupported claims removed by verification, failures, human-review requests and user thumbs. Compares a recent window against a preceding baseline
with two-sample tests and raises alerts only with enough data. These are *proxies*: a drop in confidence or a rise in abstentions says something changed, not what."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.database.models import AgentRun, Feedback
from app.learning.stats import two_proportion_z, welch_z

MIN_N = 30                 # per window
MIN_ABS_RATE_CHANGE = 0.05
MIN_MEAN_CHANGE = 0.05
ALPHA = 0.01
MAX_RUNS = 20_000

# metric -> (is a "bad" event rate, label). "bad_when_up": an increase is a regression.
RATE_METRICS = {
    "failed_rate": ("Runs that failed", True),
    "insufficient_evidence_rate": ("Answers that abstained for lack of evidence", True),
    "partial_rate": ("Answers with removed/weak claims", True),
    "needs_review_rate": ("Runs routed to human review", True),
    "thumbs_down_rate": ("Thumbs-down among rated runs", True),
}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def run_signals(run: AgentRun, rating: int | None) -> dict:
    res = run.result or {}
    ver = res.get("verification") or {}
    return {
        "day": _utc(run.created_at).strftime("%Y-%m-%d"), "ts": _utc(run.created_at),
        "failed": run.status == "failed", "needs_review": bool(run.review) or run.status == "needs_review",
        "insufficient": res.get("answer_status") == "insufficient_evidence", "partial": res.get("answer_status") == "partial",
        "answered": run.status in ("completed",) and res.get("answer_status") is not None,
        "confidence": (res.get("confidence") or {}).get("score"), "unsupported": ver.get("unsupported"), "rating": rating,
    }


async def load_signals(db: AsyncSession, org_id: str, since: datetime) -> list[dict]:
    runs = (await db.execute(select(AgentRun).where(AgentRun.org_id == org_id, AgentRun.created_at >= since).order_by(AgentRun.created_at.desc()).limit(MAX_RUNS))).scalars().all()
    ratings = {r: v for r, v in (await db.execute(select(Feedback.run_id, Feedback.rating).where(Feedback.org_id == org_id,
                                                                                                 Feedback.run_id.in_([r.id for r in runs])))).all()} if runs else {}
    return [run_signals(r, ratings.get(r.id)) for r in runs]


def _rate(rows: list[dict], key: str, among: str | None = None) -> tuple[int, int]:
    pool = [r for r in rows if (r[among] if among else True)]
    return sum(1 for r in pool if r[key]), len(pool)


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    conf = [r["confidence"] for r in rows if r["confidence"] is not None]
    rated = [r for r in rows if r["rating"] is not None]
    unsup = [r["unsupported"] for r in rows if isinstance(r["unsupported"], (int, float))]
    return {"runs": n, "failed_rate": _div(sum(r["failed"] for r in rows), n), "needs_review_rate": _div(sum(r["needs_review"] for r in rows), n),
            "insufficient_evidence_rate": _div(sum(r["insufficient"] for r in rows), sum(r["answered"] for r in rows)),
            "partial_rate": _div(sum(r["partial"] for r in rows), sum(r["answered"] for r in rows)),
            "mean_confidence": round(statistics.fmean(conf), 4) if conf else None, "mean_unsupported_claims": round(statistics.fmean(unsup), 3) if unsup else None,
            "rated": len(rated), "thumbs_down_rate": _div(sum(1 for r in rated if r["rating"] == -1), len(rated))}


def _div(a, b):
    return round(a / b, 4) if b else None


def daily(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r)
    return [{"day": d, **summarise(v)} for d, v in sorted(by.items())]


def detect_alerts(recent: list[dict], baseline: list[dict]) -> list[dict]:
    alerts = []
    for metric, (label, bad_up) in RATE_METRICS.items():
        key, among = {"failed_rate": ("failed", None), "needs_review_rate": ("needs_review", None), "insufficient_evidence_rate": ("insufficient", "answered"),
                      "partial_rate": ("partial", "answered"), "thumbs_down_rate": (None, None)}[metric]
        if metric == "thumbs_down_rate":
            r_pool, b_pool = [x for x in recent if x["rating"] is not None], [x for x in baseline if x["rating"] is not None]
            k1, n1, k2, n2 = sum(x["rating"] == -1 for x in r_pool), len(r_pool), sum(x["rating"] == -1 for x in b_pool), len(b_pool)
        else:
            k1, n1 = _rate(recent, key, among)
            k2, n2 = _rate(baseline, key, among)
        if min(n1, n2) < MIN_N:
            continue
        z, p = two_proportion_z(k1, n1, k2, n2)
        delta = k1 / n1 - k2 / n2
        if p < ALPHA and abs(delta) >= MIN_ABS_RATE_CHANGE:
            alerts.append({"metric": metric, "label": label, "baseline": round(k2 / n2, 4), "recent": round(k1 / n1, 4), "delta": round(delta, 4), "p_value": p,
                           "n_recent": n1, "n_baseline": n2, "direction": "regression" if (delta > 0) == bad_up else "improvement"})
    a = [x["confidence"] for x in recent if x["confidence"] is not None]
    b = [x["confidence"] for x in baseline if x["confidence"] is not None]
    if min(len(a), len(b)) >= MIN_N:
        z, p = welch_z(a, b)
        d = statistics.fmean(a) - statistics.fmean(b)
        if p < ALPHA and abs(d) >= MIN_MEAN_CHANGE:
            alerts.append({"metric": "mean_confidence", "label": "Mean retrieval confidence", "baseline": round(statistics.fmean(b), 4), "recent": round(statistics.fmean(a), 4),
                           "delta": round(d, 4), "p_value": p, "n_recent": len(a), "n_baseline": len(b), "direction": "regression" if d < 0 else "improvement"})
    return alerts


async def snapshot(db: AsyncSession, org_id: str, days: int = 14, baseline_days: int = 14) -> dict:
    now = utcnow()
    rows = await load_signals(db, org_id, now - timedelta(days=days + baseline_days))
    cut = now - timedelta(days=days)
    recent, base = [r for r in rows if r["ts"] >= cut], [r for r in rows if r["ts"] < cut]
    return {"window_days": days, "baseline_days": baseline_days, "recent": summarise(recent), "baseline": summarise(base), "daily": daily(recent),
            "alerts": detect_alerts(recent, base),
            "notes": ["Proxies, not ground truth: they show that behaviour changed, not why.",
                      f"Alerts need >= {MIN_N} observations per window, p < {ALPHA} and an absolute change of at least {MIN_ABS_RATE_CHANGE:.0%} (rates) or {MIN_MEAN_CHANGE} (mean confidence).",
                      f"At most {MAX_RUNS} most recent runs are analysed."]}
