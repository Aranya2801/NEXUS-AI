"""Experiment harness: builds an isolated in-process NEXUS instance, ingests the synthetic benchmark corpus, runs systems S1-S6
(docs/research/adaptive-retrieval-protocol.md) over every question and writes results computed from real runs.

Nothing in the output is hand-written. The environment block records which LLM/embedder/reranker produced the numbers and
`mock` is true whenever a mock component was used, in which case results validate plumbing and relative behaviour of the
strategies on a controlled corpus only, not real-world quality."""
from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.config import Settings
from app.evaluation import corpus as C
from app.evaluation import metrics as M
from app.main import create_app

PW = "correct-horse-battery"

SYSTEMS = {
    "S1": ("Dense only", {"policy": "fixed", "mode": "dense", "rerank": False}),
    "S2": ("Hybrid (RRF)", {"policy": "fixed", "mode": "hybrid", "rerank": False}),
    "S3": ("Hybrid + rerank", {"policy": "fixed", "mode": "hybrid", "rerank": True}),
    "S4": ("Fixed strong (decompose + multi-query + rerank)", {"policy": "fixed", "mode": "hybrid", "rerank": True,
                                                              "decompose": True, "multi_query": 3}),
    "S5": ("Adaptive", {"policy": "adaptive"}),
    "S6": ("Adaptive + graph expansion", {"policy": "adaptive", "graph": True}),
}
TOP_K = 10


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        return out if len(out) == 40 else None   # None before the first commit
    except Exception:
        return None


async def _setup(settings: Settings):
    app = create_app(settings)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval", timeout=120)
    r = await client.post("/auth/register", json={"organization_name": "EvalOrg", "email": "eval@example.com", "password": PW})
    r.raise_for_status()
    tok = (await client.post("/auth/login", json={"email": "eval@example.com", "password": PW})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    cid = (await client.post("/collections", json={"name": "benchmark"}, headers=h)).json()["id"]
    return app, ctx, client, h, cid


async def _ingest(client, app, h, cid, docs: dict[str, str]) -> dict[str, str]:
    ids = {}
    for name, text in docs.items():
        r = await client.post(f"/collections/{cid}/documents", files={"file": (name, text.encode())}, headers=h)
        r.raise_for_status()
        ids[name] = r.json()["document"]["id"]
        await app.state.runner.drain()   # one at a time, as the API tests do (in-memory SQLite shares one connection)
    return ids


def _summarise(rows: list[dict], keys: list[str]) -> dict[str, float]:
    return {k: round(statistics.fmean(r[k] for r in rows), 4) if rows else 0.0 for k in keys}


async def run(out_dir: Path, *, settings: Settings | None = None, repeats: int = 3, seed: int = 7, limit: int | None = None,
              split: str = "test", systems: list[str] | None = None, abstain_threshold: float = 0.0) -> dict:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="nexus-eval-")
    settings = settings or Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=f"{tmp}/s", log_json=False,
                                    embedding_dim=128, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4,
                                    rate_limit_per_minute=10**9, login_rate_limit_per_minute=10**9,
                                    human_review_mode="never",   # runs must not pause for a human reviewer
                                    abstain_confidence_threshold=abstain_threshold)
    projects, docs = C.build_corpus(seed)
    questions = C.build_questions(projects, seed)
    problems = C.verify(projects, docs, questions)
    if problems:
        raise RuntimeError(f"benchmark gold is inconsistent: {problems[:5]}")
    app, ctx, client, h, cid = await _setup(settings)
    try:
        ids = await _ingest(client, app, h, cid, docs)
        fn_of = {v: k for k, v in ids.items()}
        answerable = [q for q in questions if q.qclass != "unanswerable"]
        if limit:
            answerable = answerable[:limit]
        sysids = systems or list(SYSTEMS)
        per_query: dict[str, dict[str, dict]] = {s: {} for s in sysids}

        async def call(sid: str, q: C.Question):
            body = {"query": q.text, "top_k": TOP_K, "collection_ids": [cid], **SYSTEMS[sid][1]}
            t = time.perf_counter()
            r = await client.post("/search/advanced", json=body, headers=h)
            ms = (time.perf_counter() - t) * 1000
            r.raise_for_status()
            return r.json(), ms

        for q in answerable[:3]:   # warm-up pass (protocol): imports, caches, connection pools
            for sid in sysids:
                await call(sid, q)
        failures: dict[str, int] = defaultdict(int)
        for sid in sysids:
            for q in answerable:
                lat, res = [], None
                try:
                    for _ in range(repeats):
                        res, ms = await call(sid, q)
                        lat.append(ms)
                except Exception as e:   # recorded, never hidden
                    failures[sid] += 1
                    per_query[sid][q.id] = {"error": repr(e)[:200]}
                    continue
                ranked = [fn_of.get(x["document_id"], x["document_id"]) for x in res["hits"]]
                gold = set(q.gold_docs)
                row = M.retrieval_metrics(ranked, gold)
                row.update(latency_ms=statistics.median(lat), llm_calls=res["llm_usage"]["calls"],
                           prompt_tokens=res["llm_usage"]["prompt_tokens"], completion_tokens=res["llm_usage"]["completion_tokens"],
                           confidence=res["confidence"]["score"], plan=res["plan"]["name"], levels=len(res["attempts"]), ranked=ranked[:10])
                per_query[sid][q.id] = row

        gen = await _generation_eval(client, h, cid, questions, fn_of, limit) if "S5" in sysids else None
        report = _aggregate(questions, answerable, per_query, sysids, gen, failures, split, repeats, seed, settings, len(questions))
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2))
    (out_dir / "per_query.json").write_text(json.dumps({"per_query": per_query, "generation": gen}, indent=2))
    return report


async def _generation_eval(client, h, cid, questions, fn_of, limit):
    """End-to-end answers through the real 8-agent workflow (settings.human_review_mode == "never" so runs do not pause)."""
    sample = [q for q in questions if q.qclass != "exploratory"]
    if limit:
        sample = sample[:limit]
    rows = []
    for q in sample:
        t = time.perf_counter()
        r = await client.post("/chat", json={"message": q.text, "collection_ids": [cid]}, headers=h)
        ms = (time.perf_counter() - t) * 1000
        await client.delete("/memory", params={"kind": "session"}, headers=h)   # independent runs: stay under the per-user session cap
        if r.status_code != 200:
            rows.append({"id": q.id, "qclass": q.qclass, "split": q.split, "error": r.text[:200], "latency_ms": ms})
            continue
        j = r.json()
        cited = {fn_of.get(c["document_id"], c["document_id"]) for c in j["citations"]}
        low = j["answer"].lower()
        rows.append({
            "id": q.id, "qclass": q.qclass, "split": q.split, "answer_status": j.get("answer_status"), "latency_ms": ms,
            "answer_contains_gold": (all(a.lower() in low for a in q.answers) if q.answers else None),
            "citation_recall": (len(cited & set(q.gold_docs)) / len(q.gold_docs) if q.gold_docs else None),
            "citation_precision": (len(cited & set(q.gold_docs)) / len(cited) if cited and q.gold_docs else None),
            "n_citations": len(cited), "confidence": j.get("confidence", {}), "quality": j.get("quality", {}),
            "abstained": (j.get("answer_status") not in ("grounded", "partial")) if q.qclass == "unanswerable" else None,
            "claims_supported_rate": ((j["verification"].get("supported") or 0) / j["verification"]["claims_total"]
                                      if j["verification"].get("claims_total") else None),
        })
    return rows


def _ci(vals):
    m, lo, hi = M.bootstrap_mean_ci(vals)
    return {"mean": round(m, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4)}


def _aggregate(questions, answerable, per_query, sysids, gen, failures, split, repeats, seed, settings, n_total):
    qmap = {q.id: q for q in questions}
    keys = ["precision_at_5", "recall_at_5", "ndcg_at_5", "precision_at_10", "recall_at_10", "ndcg_at_10", "mrr"]
    rep_ids = [q.id for q in answerable if qmap[q.id].split == split]
    systems = []
    for sid in sysids:
        rows = [per_query[sid][i] for i in rep_ids if "error" not in per_query[sid].get(i, {"error": 1})]
        ret = _summarise(rows, keys)
        ci = _ci([r["ndcg_at_10"] for r in rows])
        ret.update(ndcg_at_10_ci_low=ci["ci_low"], ndcg_at_10_ci_high=ci["ci_high"])
        lat = [r["latency_ms"] for r in rows]
        calls = [r["llm_calls"] for r in rows]
        eng = {"latency_mean_ms": round(statistics.fmean(lat), 2) if lat else 0, "latency_p50_ms": round(M.percentile(lat, 50), 2),
               "latency_p95_ms": round(M.percentile(lat, 95), 2), "llm_calls_mean": round(statistics.fmean(calls), 3) if calls else 0,
               "llm_calls_median": float(statistics.median(calls)) if calls else 0,
               "prompt_tokens_mean": round(statistics.fmean(r["prompt_tokens"] for r in rows), 1) if rows else 0,
               "failure_rate": round(failures.get(sid, 0) / max(1, len(answerable)), 4), "n_queries": len(rows)}
        by_class = {}
        for c in sorted({qmap[i].qclass for i in rep_ids}):
            cr = [per_query[sid][i] for i in rep_ids if qmap[i].qclass == c and "error" not in per_query[sid][i]]
            by_class[c] = {"n": len(cr), **_summarise(cr, ["ndcg_at_10", "recall_at_10", "mrr", "llm_calls", "latency_ms"])}
        systems.append({"id": sid, "name": SYSTEMS[sid][0], "retrieval": ret, "engineering": eng, "by_class": by_class})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"name": "synthetic-projects-v1", "n_questions": n_total, "synthetic": True,
                    "description": f"{len({q.gold_docs[0] for q in answerable if q.gold_docs})} synthetic project documents; retrieval metrics on the "
                                   f"'{split}' split ({len(rep_ids)} answerable questions); gold programmatically derived, not human-annotated"},
        "environment": {"llm": settings.llm_model, "embedder": f"{settings.embedding_model}:{settings.embedding_dim}",
                        "mock": settings.llm_provider == "mock" or settings.embedding_provider == "mock",
                        "reranker": settings.reranker_provider, "python": platform.python_version(), "git_commit": _git_commit(),
                        "repeats": repeats, "seed": seed, "split": split, "human_review_mode": settings.human_review_mode,
                        "abstain_confidence_threshold": settings.abstain_confidence_threshold,
                        "evidence_sufficiency_threshold": settings.evidence_sufficiency_threshold,
                        "max_retrieval_loops": settings.max_retrieval_loops},
        "systems": systems, "hypotheses": _hypotheses(per_query, rep_ids, qmap, sysids), "generation": _gen_summary(gen),
        "classifier": _classifier_report(answerable) if "S5" in sysids else None, "notes": [],
    }
    if report["environment"]["mock"]:
        report["notes"].append("MOCK COMPONENTS (hashing embedder and/or MockLLM): these numbers validate plumbing and the relative "
                               "behaviour of retrieval strategies on a controlled synthetic corpus. They are NOT evidence of real-world quality.")
    report["notes"].append("Synthetic template corpus; gold labels are correct by construction, not human annotated. Small corpus (24 documents).")
    return report


def _hypotheses(per_query, ids, qmap, sysids):
    out = {}

    def col(sid, key, sel=ids):
        return [per_query[sid][i][key] for i in sel if "error" not in per_query[sid].get(i, {"error": 1})]
    if {"S4", "S5"} <= set(sysids):
        d = M.paired_bootstrap(col("S5", "ndcg_at_10"), col("S4", "ndcg_at_10"))
        d["non_inferior_margin_0.02"] = d["ci_low"] > -0.02
        out["H1_quality_S5_vs_S4_ndcg10"] = d
        a, b = col("S5", "llm_calls"), col("S4", "llm_calls")
        out["H2_cost_llm_calls"] = {"S5_mean": round(statistics.fmean(a), 3), "S4_mean": round(statistics.fmean(b), 3),
                                    "S5_median": statistics.median(a), "S4_median": statistics.median(b),
                                    "supported": statistics.fmean(a) < statistics.fmean(b) and statistics.median(a) <= statistics.median(b)}
        la, lb = col("S5", "latency_ms"), col("S4", "latency_ms")
        out["H3_latency"] = {"S5_mean_ms": round(statistics.fmean(la), 2), "S4_mean_ms": round(statistics.fmean(lb), 2),
                             "S5_p95_ms": round(M.percentile(la, 95), 2), "S4_p95_ms": round(M.percentile(lb, 95), 2),
                             "supported": statistics.fmean(la) < statistics.fmean(lb) and M.percentile(la, 95) < M.percentile(lb, 95)}
    if {"S2", "S3", "S5"} <= set(sysids):
        sel = [i for i in ids if qmap[i].qclass == "lookup"]
        d = M.paired_bootstrap(col("S5", "ndcg_at_10", sel), col("S2", "ndcg_at_10", sel))
        out["H4_lookup_S5_vs_S2_ndcg10"] = {**d, "matches_or_beats": d["ci_low"] > -0.02 or d["mean_diff"] >= 0,
                                            "S5_latency_ms": round(statistics.fmean(col("S5", "latency_ms", sel)), 2),
                                            "S3_latency_ms": round(statistics.fmean(col("S3", "latency_ms", sel)), 2),
                                            "S5_cheaper_than_S3_latency": statistics.fmean(col("S5", "latency_ms", sel)) < statistics.fmean(col("S3", "latency_ms", sel))}
    if "S5" in sysids:
        sc = col("S5", "confidence")
        succ = [int(v >= 1.0) for v in col("S5", "recall_at_10")]
        au = M.auroc(sc, succ)
        out["H5_confidence_auroc_vs_recall10_full"] = {"auroc": au, "n": len(sc), "n_success": int(sum(succ)),
                                                       "supported": (au is not None and au > 0.65),
                                                       "note": "AUROC undefined if every query succeeded or failed" if au is None else ""}
        top1 = [int(v >= 1.0) for v in col("S5", "mrr")]
        out["H5_posthoc_confidence_auroc_vs_gold_at_rank1"] = {
            "auroc": M.auroc(sc, top1), "n": len(sc), "n_success": int(sum(top1)),
            "note": "POST-HOC, not pre-registered: the pre-registered label (Recall@10 = 1) was degenerate on this corpus."}
    if {"S4", "S5"} <= set(sysids):   # per-class Wilcoxon S5 vs S4 with Holm correction
        raw, detail = {}, {}
        for c in sorted({qmap[i].qclass for i in ids}):
            sel = [i for i in ids if qmap[i].qclass == c]
            a5, a4 = col("S5", "ndcg_at_10", sel), col("S4", "ndcg_at_10", sel)
            w = M.wilcoxon_signed_rank(a5, a4)
            raw[c] = w["p"]
            detail[c] = {**w, "mean_diff": round(statistics.fmean(x - y for x, y in zip(a5, a4)), 4)}
        adj = M.holm(raw)
        out["per_class_wilcoxon_S5_vs_S4_ndcg10"] = {c: {**detail[c], "p_holm": round(adj[c], 4)} for c in detail}
    return out


def _gen_summary(gen):
    if not gen:
        return None
    ok = [r for r in gen if "error" not in r]

    def mean(key, sel=ok):
        v = [r[key] for r in sel if r.get(key) is not None]
        return (round(statistics.fmean(v), 4), len(v)) if v else (None, 0)
    ans = [r for r in ok if r["qclass"] != "unanswerable"]
    unans = [r for r in ok if r["qclass"] == "unanswerable"]
    return {"n_runs": len(gen), "n_errors": len(gen) - len(ok),
            "answer_contains_gold": dict(zip(("rate", "n"), mean("answer_contains_gold", ans))),
            "citation_recall": dict(zip(("mean", "n"), mean("citation_recall", ans))),
            "citation_precision": dict(zip(("mean", "n"), mean("citation_precision", ans))),
            "claims_supported_rate": dict(zip(("mean", "n"), mean("claims_supported_rate", ok))),
            "abstention_on_unanswerable": dict(zip(("rate", "n"), mean("abstained", unans))),
            "false_abstention_on_answerable": {"rate": round(statistics.fmean(r["answer_status"] not in ("grounded", "partial") for r in ans), 4) if ans else None, "n": len(ans)},
            "latency_ms_mean": round(statistics.fmean(r["latency_ms"] for r in ok), 1) if ok else None,
            "by_class": {c: dict(zip(("answer_contains_gold", "n"), mean("answer_contains_gold", [r for r in ans if r["qclass"] == c])))
                         for c in sorted({r["qclass"] for r in ans})}}


def _classifier_report(answerable):
    from app.retrieval.adaptive import extract_features
    classes = ["lookup", "factoid", "comparison", "multi_hop", "exploratory"]
    conf = {t: {p: 0 for p in classes} for t in classes}
    for q in answerable:
        conf[q.qclass][extract_features(q.text).query_class] += 1
    per = {c: round(conf[c][c] / max(1, sum(conf[c].values())), 4) for c in classes}
    tot = sum(conf[c][c] for c in classes)
    return {"accuracy": round(tot / max(1, len(answerable)), 4), "per_class_recall": per, "confusion": conf,
            "note": "Labels = benchmark question class assigned at generation time (rule-based classifier vs template-derived label)."}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="Run the NEXUS retrieval/answer evaluation and write <out>/latest.json")
    ap.add_argument("--out", default="../experiments/results")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--split", choices=["dev", "test"], default="test")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--systems", nargs="*")
    ap.add_argument("--abstain-threshold", default="0", help="float, or 'calibrated' to read <out>/calibration.json")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    thr = (json.loads((out / "calibration.json").read_text())["chosen_threshold"]
           if a.abstain_threshold == "calibrated" else float(a.abstain_threshold))
    rep = asyncio.run(run(out, repeats=a.repeats, split=a.split, limit=a.limit, systems=a.systems, abstain_threshold=thr))
    json.dump({"written": str(out / "latest.json"), "environment": rep["environment"],
               "ndcg_at_10": {s["id"]: s["retrieval"]["ndcg_at_10"] for s in rep["systems"]}}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
