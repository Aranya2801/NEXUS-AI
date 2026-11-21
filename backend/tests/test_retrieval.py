import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, RetrievalError
from app.database.models import AuditLog
from app.embeddings.hashing import HashingEmbedder
from app.embeddings.openai_compat import OpenAICompatEmbedder
from app.main import create_app
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.sparse import BM25, tokenize
from tests.fixtures import make_pdf
from tests.helpers import DOCS, PW, login, make_collection, register, search, seed, upload


# ------------------------------------------------------------------ unit
def test_rrf_known_values_and_ranks():
    fused = reciprocal_rank_fusion({"a": ["d1", "d2", "d3"], "b": ["d2", "d3", "d1"]}, k=60)
    assert [d for d, _, _ in fused] == ["d2", "d1", "d3"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0][2] == {"a": 2, "b": 1}


def test_rrf_weights_and_single_list_docs():
    fused = reciprocal_rank_fusion({"a": ["x"], "b": ["y"]}, k=60, weights={"b": 3.0})
    assert [d for d, _, _ in fused] == ["y", "x"]
    assert reciprocal_rank_fusion({}, k=60) == []


def test_bm25_rare_term_wins_and_scores_nonnegative():
    docs = [tokenize(t) for t in ["the cat sat on the mat", "the dog sat on the log", "zyxwv rare token here"]]
    bm = BM25(docs)
    assert all(v >= 0 for v in bm.idf.values())
    s = bm.score(tokenize("zyxwv"))
    assert s[2] > 0 and s[0] == s[1] == 0
    assert bm.score(["sat"])[0] > 0  # common term still non-negative


async def test_openai_embedder_batching_order_retry_and_dim_check():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer sk-test"
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "boom"})
        # return out of order to prove the client re-sorts by index
        data = [{"index": i, "embedding": [float(len(t)), 0.0, 1.0]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    emb = OpenAICompatEmbedder(model="text-embedding-3-small", dim=3, api_key="sk-test", base_url="https://api.openai.com/v1",
                               client=client, batch_size=2, backoff=0.0)
    out = await emb.embed(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [v[0] for v in out] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(calls) == 4  # 1 failed + 3 batches (2,2,1)
    assert all(c["dimensions"] == 3 for c in calls)

    bad = OpenAICompatEmbedder(model="m", dim=5, api_key="sk-test", base_url="https://api.openai.com/v1",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(
                                   lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}))))
    with pytest.raises(RetrievalError):
        await bad.embed(["x"])
    with pytest.raises(ConfigurationError):
        OpenAICompatEmbedder(model="m", dim=3, api_key=None, base_url="https://api.openai.com/v1")


# ------------------------------------------------------------------ API
@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False,
                    embedding_dim=64, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4)


@pytest_asyncio.fixture
async def app(cfg):
    a = create_app(cfg)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
async def test_each_mode_ranks_the_right_document_first(client, app, mode):
    h = await register(client, "Acme", "a@example.com")
    ids = await seed(client, app, h, await make_collection(client, h))
    for query, expected in [("self-attention transformer tokens", "attention.txt"),
                            ("kubernetes container orchestration pods", "kubernetes.txt"),
                            ("invoice ledger audit reconcile", "billing.txt")]:
        r = await search(client, h, query, mode=mode, top_k=3)
        assert r.status_code == 200, r.text
        hits = r.json()["hits"]
        assert hits and hits[0]["document_id"] == ids[expected], (mode, query, [x["filename"] for x in hits])


async def test_exact_token_needs_sparse_and_hybrid_keeps_it(client, app):
    h = await register(client, "Acme", "a@example.com")
    ids = await seed(client, app, h, await make_collection(client, h))
    for mode in ("sparse", "hybrid"):
        hits = (await search(client, h, "zyxwv42", mode=mode)).json()["hits"]
        assert hits[0]["document_id"] == ids["billing.txt"]
    body = (await search(client, h, "zyxwv42 reconcile", mode="hybrid")).json()
    top = body["hits"][0]
    assert set(top["ranks"]) <= {"dense", "sparse"} and top["score"] > 0
    assert {"scope", "embed", "dense", "sparse", "fuse", "hydrate", "total"} <= set(body["timings_ms"])
    assert body["candidates"]["sparse"] > 0 and body["collections_searched"] == 1


async def test_hit_provenance_pages_sections_and_parent_context(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    pdf = make_pdf([["1. INTRODUCTION", "Attention is all you need for sequence transduction."],
                    ["2. RESULTS", "The base model reached 27.3 BLEU on translation."]])
    await upload(client, app, h, cid, "paper.pdf", pdf)
    hit = (await search(client, h, "BLEU translation", mode="sparse", include_parent=True)).json()["hits"][0]
    assert (hit["page_start"], hit["section"], hit["filename"], hit["version"], hit["is_current"]) == (2, "2. RESULTS", "paper.pdf", 1, True)
    assert hit["parent_text"] and hit["text"] in hit["parent_text"]


async def test_tenant_isolation_in_search(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    cid_a = await make_collection(client, ha)
    await seed(client, app, ha, cid_a)
    for mode in ("dense", "sparse", "hybrid"):
        body = (await search(client, hb, "zyxwv42 invoice kubernetes attention", mode=mode)).json()
        assert body["hits"] == [] and body["collections_searched"] == 0
        # explicitly naming the other tenant's collection must not widen scope either
        body = (await search(client, hb, "zyxwv42", mode=mode, collection_ids=[cid_a])).json()
        assert body["hits"] == []
    cid_b = await make_collection(client, hb, "mine")
    await upload(client, app, hb, cid_b, "own.txt", b"BravoCorp quarterly planning notes about zyxwv42 budgets " * 5)
    hits = (await search(client, hb, "zyxwv42")).json()["hits"]
    assert hits and all(x["collection_id"] == cid_b for x in hits)


async def test_restricted_collection_is_enforced_inside_search(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    open_c = await make_collection(client, ha, "open")
    hr_c = await make_collection(client, ha, "hr", restricted=True)
    await upload(client, app, ha, open_c, "handbook.txt", b"General handbook about office hours and holidays " * 8)
    await upload(client, app, ha, hr_c, "salaries.txt", b"Confidential salary bands for engineering staff " * 8)
    uid = (await client.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)).json()["id"]
    he = await login(client, "e@example.com")
    for mode in ("dense", "sparse", "hybrid"):
        got = (await search(client, he, "confidential salary bands", mode=mode)).json()["hits"]
        assert all(x["collection_id"] != hr_c for x in got)  # nothing from the restricted collection ever surfaces
        assert (await search(client, he, "salary", mode=mode, collection_ids=[hr_c])).json()["hits"] == []
    await client.post(f"/collections/{hr_c}/access", json={"user_id": uid}, headers=ha)
    hits = (await search(client, he, "confidential salary bands", mode="sparse")).json()["hits"]
    assert hits and hits[0]["collection_id"] == hr_c


async def test_versions_current_as_of_and_all_versions(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    before_any = datetime.now(timezone.utc)
    await asyncio.sleep(0.02)
    await upload(client, app, h, cid, "policy.txt", b"The legacy refund policy allows returns within thirty days of purchase. " * 6)
    await asyncio.sleep(0.02)
    mid = datetime.now(timezone.utc)
    await asyncio.sleep(0.02)
    await upload(client, app, h, cid, "policy.txt", b"The modern refund policy allows returns within sixty days of purchase. " * 6)

    assert (await search(client, h, "legacy thirty", mode="sparse")).json()["hits"] == []          # superseded: invisible by default
    cur = (await search(client, h, "modern sixty refund", mode="sparse")).json()["hits"]
    assert cur and all(x["version"] == 2 and x["is_current"] for x in cur)

    hist = (await search(client, h, "refund policy returns", mode="sparse", temporal="all_versions", top_k=50)).json()["hits"]
    assert {(x["version"], x["is_current"]) for x in hist} == {(1, False), (2, True)}

    at_mid = (await search(client, h, "refund policy returns", mode="sparse", temporal="as_of", as_of=mid.isoformat(), top_k=50)).json()["hits"]
    assert at_mid and {x["version"] for x in at_mid} == {1}
    at_start = await search(client, h, "refund policy", mode="sparse", temporal="as_of", as_of=before_any.isoformat())
    assert at_start.json()["hits"] == []
    assert (await search(client, h, "refund", temporal="as_of")).status_code == 422  # as_of required


async def test_metadata_filters(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    await upload(client, app, h, cid, "legal.txt", b"Contract termination clauses and liability caps " * 8, department="legal")
    await upload(client, app, h, cid, "eng.md", b"# Eng\nContract testing for services and liability of APIs " * 8, department="engineering")
    allh = (await search(client, h, "contract liability", mode="sparse")).json()["hits"]
    assert {x["filename"] for x in allh} == {"legal.txt", "eng.md"}
    assert {x["filename"] for x in (await search(client, h, "contract liability", mode="sparse", departments=["legal"])).json()["hits"]} == {"legal.txt"}
    assert {x["filename"] for x in (await search(client, h, "contract liability", mode="sparse", doc_types=[".md"])).json()["hits"]} == {"eng.md"}
    future = "2999-01-01T00:00:00Z"
    assert (await search(client, h, "contract liability", mode="sparse", date_from=future)).json()["hits"] == []


async def test_dense_ignores_chunks_from_a_different_embedding_model(client, app):
    h = await register(client, "Acme", "a@example.com")
    await seed(client, app, h, await make_collection(client, h))
    assert (await search(client, h, "kubernetes pods", mode="dense")).json()["hits"]
    app.state.retriever.embedder = HashingEmbedder(32)  # "re-configured" provider: old vectors are incomparable
    assert (await search(client, h, "kubernetes pods", mode="dense")).json()["hits"] == []
    assert (await search(client, h, "kubernetes pods", mode="sparse")).json()["hits"]  # lexical path unaffected


async def test_validation_auth_and_audit_hides_query_text(client, app):
    h = await register(client, "Acme", "a@example.com")
    assert (await search(client, h, "")).status_code == 422
    assert (await search(client, h, "x" * 2001)).status_code == 422
    assert (await client.post("/search", json={"query": "hi"})).status_code == 401
    await seed(client, app, h, await make_collection(client, h))
    await search(client, h, "super secret merger codename")
    async with app.state.session_factory() as db:
        rows = [a for a in (await db.execute(select(AuditLog).where(AuditLog.action == "search"))).scalars()]
    assert rows and "merger" not in json.dumps(rows[-1].detail) and len(rows[-1].detail["query_sha"]) == 16
