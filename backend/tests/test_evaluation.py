import json
import math

import httpx
import pytest
import pytest_asyncio

from app.core.config import Settings
from app.evaluation import corpus as C
from app.evaluation import metrics as M
from app.evaluation.calibrate import choose
from app.main import create_app
from tests.helpers import register


# ---- metrics: hand-computed values -------------------------------------------------------------------------------------
def test_retrieval_metrics_known_values():
    ranked, gold = ["a", "x", "b", "y", "c"], {"a", "b", "z"}
    assert M.precision_at_k(ranked, gold, 5) == pytest.approx(2 / 5)
    assert M.recall_at_k(ranked, gold, 5) == pytest.approx(2 / 3)
    assert M.reciprocal_rank(["x", "b"], gold) == pytest.approx(0.5)
    dcg = 1 / math.log2(2) + 1 / math.log2(4)                      # relevant at ranks 1 and 3
    ideal = 1 / math.log2(2) + 1 / math.log2(3) + 1 / math.log2(4)  # 3 gold docs available
    assert M.ndcg_at_k(ranked, gold, 5) == pytest.approx(dcg / ideal)


def test_metrics_dedupe_chunks_of_the_same_document_and_handle_empty_gold():
    assert M.recall_at_k(["a", "a", "a", "b"], {"a", "b"}, 2) == 1.0     # duplicates must not push b out of the top-2
    assert M.ndcg_at_k(["a"], set(), 5) == 0.0 and M.recall_at_k(["a"], set(), 5) == 0.0
    assert M.ndcg_at_k(["a", "b"], {"a", "b"}, 10) == pytest.approx(1.0)


def test_paired_bootstrap_detects_a_real_difference_and_is_deterministic():
    a = [0.9, 0.8, 1.0, 0.95, 0.85, 0.9, 1.0, 0.8] * 4
    b = [x - 0.1 for x in a]
    r1, r2 = M.paired_bootstrap(a, b, n=2000), M.paired_bootstrap(a, b, n=2000)
    assert r1 == r2 and r1["mean_diff"] == pytest.approx(0.1) and r1["ci_low"] > 0.09 and r1["ci_high"] < 0.11
    z = M.paired_bootstrap(a, a, n=500)
    assert z["mean_diff"] == 0 and z["ci_low"] == 0 == z["ci_high"]


def test_wilcoxon_known_case_and_degenerate_input():
    d = M.wilcoxon_signed_rank([i + 1.0 for i in range(1, 21)], list(range(1, 21)))      # all differences equal +1
    assert d["p"] < 1e-3
    assert M.wilcoxon_signed_rank([1, 1, 1], [1, 1, 1])["p"] == 1.0
    sym = M.wilcoxon_signed_rank([1, 3, 2, 4, 3, 5, 4, 6], [2, 2, 3, 3, 4, 4, 5, 5])
    assert sym["p"] > 0.5


def test_holm_and_auroc():
    adj = M.holm({"a": 0.01, "b": 0.04, "c": 0.5})
    assert adj["a"] == pytest.approx(0.03) and adj["b"] == pytest.approx(0.08) and adj["c"] == pytest.approx(0.5)
    assert M.auroc([0.9, 0.8, 0.3, 0.2], [1, 1, 0, 0]) == 1.0
    assert M.auroc([0.5, 0.5], [1, 0]) == 0.5
    assert M.auroc([0.1, 0.2], [1, 1]) is None          # one class missing -> undefined, never a made-up number


# ---- benchmark integrity -----------------------------------------------------------------------------------------------
def test_benchmark_is_large_consistent_deterministic_and_split():
    projects, docs = C.build_corpus(7)
    qs = C.build_questions(projects, 7)
    assert len(qs) >= 100 and C.verify(projects, docs, qs) == []
    assert {q.qclass for q in qs} == {"lookup", "factoid", "comparison", "multi_hop", "exploratory", "unanswerable"}
    p2, d2 = C.build_corpus(7)
    assert docs == d2 and [q.text for q in qs] == [q.text for q in C.build_questions(p2, 7)]
    dev = [q for q in qs if q.split == "dev"]
    assert 0.2 < len(dev) / len(qs) < 0.4 and any(q.split == "test" for q in qs)


def test_verify_catches_wrong_gold():
    projects, docs = C.build_corpus(7)
    qs = C.build_questions(projects, 7)
    q = next(x for x in qs if x.qclass == "factoid")
    q.gold_docs = [next(f for f in docs if f != q.gold_docs[0])]       # point gold at the wrong document
    assert any(p.startswith(q.id) and "no gold document" in p for p in C.verify(projects, docs, qs))


def test_choose_threshold_separates_classes_and_uses_only_given_rows():
    rows = [{"qclass": "factoid", "confidence": {"score": s}} for s in (0.6, 0.7, 0.8)] + \
           [{"qclass": "unanswerable", "confidence": {"score": s}} for s in (0.2, 0.3)]
    t, opt = choose(rows)
    assert 0.3 < t < 0.6 and opt


# ---- API ---------------------------------------------------------------------------------------------------------------
@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                   ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, evaluation_results_dir=str(tmp_path / "res"))
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield app, c, tmp_path


async def test_evaluation_endpoint_is_honest_when_empty_and_serves_written_results(env):
    app, c, tmp = env
    h = await register(c, "Acme", "a@example.com")
    assert (await c.get("/evaluation/results")).status_code == 401
    assert (await c.get("/evaluation/results", headers=h)).status_code == 404
    (tmp / "res").mkdir()
    payload = {"generated_at": "2026-01-01T00:00:00Z", "dataset": {"name": "d", "n_questions": 1, "synthetic": True},
               "environment": {"llm": "x", "embedder": "y", "mock": True}, "systems": [], "notes": []}
    (tmp / "res" / "latest.json").write_text(json.dumps(payload))
    r = await c.get("/evaluation/results", headers=h)
    assert r.status_code == 200 and r.json() == payload
    (tmp / "res" / "latest.json").write_text("{not json")
    assert (await c.get("/evaluation/results", headers=h)).status_code == 404


async def test_metrics_endpoint_is_admin_only_and_uses_route_templates(env):
    app, c, _ = env
    admin = await register(c, "Acme", "a@example.com")
    await c.get("/health/live")
    await c.post("/search", json={"query": "q"}, headers=admin)
    assert (await c.get("/admin/metrics")).status_code == 401
    body = (await c.get("/admin/metrics", headers=admin)).text
    assert 'nexus_http_requests_total{method="POST",route="/search",status="200"} 1' in body
    assert "nexus_http_request_duration_seconds_bucket" in body and 'le="+Inf"' in body and "nexus_uptime_seconds" in body
    assert (await c.get("/admin/observability", headers=admin)).json()["tracing"] == {"otel": "disabled", "langsmith": "disabled"}


async def test_metrics_histogram_is_cumulative():
    from app.observability.metrics import build_registry
    r = build_registry()
    for v in (0.001, 0.02, 3.0):
        r.observe("nexus_http_request_duration_seconds", v, "GET", "/x")
    txt = r.render()
    assert 'le="0.005"} 1' in txt and 'le="0.025"} 2' in txt and 'le="+Inf"} 3' in txt and "_count{method=\"GET\",route=\"/x\"} 3" in txt


def test_tracing_degrades_gracefully_and_can_really_trace(tmp_path):
    from app.observability.tracing import setup_tracing
    from fastapi import FastAPI
    cfg = Settings(env="test", otel_enabled=True, langsmith_enabled=True, storage_dir=str(tmp_path))
    st = setup_tracing(FastAPI(), cfg)
    assert st["otel"].startswith("enabled") and "LANGSMITH_API_KEY" in st["langsmith"]


# ---- the calibrated abstention gate -------------------------------------------------------------------------------------
async def test_abstain_gate_suppresses_answer_and_citations_only_below_threshold(tmp_path):
    from tests.helpers import make_collection, seed
    answers = {}
    for thr in (0.0, 0.99):
        cfg = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / f"s{thr}"), log_json=False, embedding_dim=64,
                       ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, human_review_mode="never", abstain_confidence_threshold=thr)
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                h = await register(c, "Acme", "a@example.com")
                await seed(c, app, h, await make_collection(c, h))
                r = await c.post("/chat", json={"message": "kubernetes container orchestration pods"}, headers=h)
                answers[thr] = r.json()
    assert answers[0.0]["answer_status"] in ("grounded", "partial") and answers[0.0]["citations"]
    assert answers[0.99]["answer_status"] == "insufficient_evidence" and answers[0.99]["citations"] == []


# ---- harness end to end (tiny) ------------------------------------------------------------------------------------------
async def test_harness_produces_real_nonfabricated_results(tmp_path):
    from app.evaluation import harness
    rep = await harness.run(tmp_path / "out", repeats=1, limit=8, split="test", systems=["S2", "S3", "S5"])
    assert (tmp_path / "out" / "latest.json").is_file() and rep["environment"]["mock"] is True
    assert any("MOCK" in n for n in rep["notes"]) and rep["dataset"]["synthetic"] is True
    for s in rep["systems"]:
        assert 0.0 <= s["retrieval"]["ndcg_at_10"] <= 1.0 and s["engineering"]["latency_mean_ms"] > 0
    per = json.loads((tmp_path / "out" / "per_query.json").read_text())["per_query"]
    row = next(iter(per["S2"].values()))
    assert set(row["ranked"]) <= {f"{p.name.lower()}.txt" for p in C.build_corpus(7)[0]}   # real document ids from real retrieval
