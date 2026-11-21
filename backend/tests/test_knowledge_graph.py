import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import Entity, EntityAlias, Relation
from app.knowledge_graph.indexer import GraphIndexer
from app.knowledge_graph.llm_extractor import LLMExtractor
from app.knowledge_graph.resolution import EntityResolver, acronym_pairs, canonicalize
from app.knowledge_graph.rule_extractor import RuleBasedExtractor
from app.knowledge_graph.vocab import normalize_predicate
from app.llm.base import LLMResponse
from app.main import create_app
from tests.helpers import PW, login, make_collection, register, upload

CORPUS = {
    "survey.txt": "Vaswani et al. introduced the Transformer architecture. The Transformer relies on self-attention. "
                  "Quadratic memory cost limits Transformer on long sequences.",
    "longformer.txt": "Longformer extends Transformer with sparse attention patterns. Longformer reduces memory consumption for long documents.",
    "linformer.txt": "Linformer improves Transformer by projecting keys and values to a low-rank space. Linformer was evaluated on GLUE benchmark.",
    "performer.txt": "Performer approximates Linformer attention using random features. The estimator variance remains a limitation of Performer.",
}


class Scripted:
    name = "scripted"

    def __init__(self, text=None, exc=None):
        self.text, self.exc = text, exc

    async def complete(self, system, user, *, max_tokens=800, temperature=0.0):
        if self.exc:
            raise self.exc
        return LLMResponse(self.text, "scripted", 3, 3)


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


async def load_corpus(client, app, h, cid, docs=CORPUS):
    return {n: (await upload(client, app, h, cid, n, t.encode()))["document"]["id"] for n, t in docs.items()}


async def gq(client, h, query, **kw):
    r = await client.post("/graph/query", json={"query": query, **kw}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ extractors (unit)
def test_rule_extractor_entities_predicates_and_passive_swap():
    r = RuleBasedExtractor().extract_text(
        "Vaswani et al. introduced the Transformer architecture. Longformer extends Transformer with sparse attention. "
        "The Transformer is improved by Reformer. Quadratic memory limits Transformer on long sequences.")
    names = {e.surface for e in r.entities}
    assert {"Vaswani et al", "Transformer", "Longformer", "Reformer"} <= names and "Quadratic" not in names and "The" not in names
    rels = {(x.source, x.predicate, x.target) for x in r.relations}
    assert ("Longformer", "extends", "Transformer") in rels and ("Reformer", "improves", "Transformer") in rels  # passive swapped
    assert all(x.evidence for x in r.relations)


def test_vocab_and_canonicalization():
    assert normalize_predicate("Enhances") == "improves" and normalize_predicate("builds on") == "extends"
    assert normalize_predicate("hacks the system") == "related_to" and normalize_predicate("improves") == "improves"
    assert canonicalize("The Transformers") == canonicalize("transformer") == "transformer"
    assert canonicalize("Self-Attention") == canonicalize("self attention")
    assert canonicalize("Acme Inc.") == "acme" and canonicalize("OpenAI's") == "openai"
    assert acronym_pairs("Large Language Models (LLMs) are common; Foo Bar (XYZ) is not an acronym.") == [("Large Language Models", "LLMs")]


async def test_llm_extractor_rejects_everything_not_in_the_text():
    text = "Longformer extends Transformer with sparse attention. Nothing else happens here."
    hostile = ('{"entities": [{"name": "Longformer", "type": "model"}, {"name": "Skynet", "type": "organization"}], '
               '"relations": ['
               '{"source": "Longformer", "predicate": "extends", "target": "Transformer", "evidence": "Longformer extends Transformer with sparse attention."},'
               '{"source": "Longformer", "predicate": "hacks", "target": "Skynet", "evidence": "Longformer hacks Skynet."},'
               '{"source": "Longformer", "predicate": "improves", "target": "Transformer", "evidence": "Nothing else happens here."},'
               '{"source": "Transformer", "predicate": "uses", "target": "Transformer", "evidence": "Longformer extends Transformer with sparse attention."},'
               '{"source": 5, "predicate": "x"}, "IGNORE PREVIOUS INSTRUCTIONS", {"source": "A", "target": "B", "evidence": ""}]}')
    ex = await LLMExtractor(Scripted(hostile)).extract_one(text)
    assert [(r.source, r.predicate, r.target) for r in ex.relations] == [("Longformer", "extends", "Transformer")]
    assert {e.surface for e in ex.entities} == {"Longformer", "Transformer"}     # "Skynet" never occurs in the text
    assert ex.rejected >= 5
    capped = await LLMExtractor(Scripted('{"entities": [' + ",".join(f'{{"name": "w{i}"}}' for i in range(60)) + "]}"), max_entities=10).extract_one(
        " ".join(f"w{i}" for i in range(60)))
    assert len(capped.entities) == 10
    assert (await LLMExtractor(Scripted("not json")).extract_one(text)).relations == []
    with pytest.raises(RuntimeError):
        await LLMExtractor(Scripted(exc=RuntimeError("down"))).extract_one(text)


async def test_resolver_fuzzy_is_conservative_and_acronyms_unify(db):
    r = EntityResolver(db, "org1", {"bert": canonicalize("Bidirectional Encoder Representations from Transformers")})
    first = await r.resolve_many({"Stanford University": "organization", "Bidirectional Encoder Representations from Transformers": "concept"})
    again = await EntityResolver(db, "org1", {"bert": canonicalize("Bidirectional Encoder Representations from Transformers")}).resolve_many(
        {"Stanford Univerity": "organization", "BERT": "concept", "Berta": "person", "Bert": "person"})
    await db.flush()
    assert again["Stanford Univerity"].id == first["Stanford University"].id                    # typo merged (same type, high similarity)
    assert again["BERT"].id == first["Bidirectional Encoder Representations from Transformers"].id  # acronym -> expansion
    assert again["Bert"].id == again["BERT"].id            # document-level acronym map applies to the surface "Bert" too
    assert again["Berta"].id != again["BERT"].id          # short, different names are never fuzzy-merged
    other_org = await EntityResolver(db, "org2").resolve_many({"Stanford University": "organization"})
    assert other_org["Stanford University"].id != first["Stanford University"].id                  # orgs never share entities


# ------------------------------------------------------------------ end-to-end
async def test_multi_hop_retrieval_finds_documents_lexical_search_misses(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    ids = await load_corpus(client, app, h, cid)
    q = "What improvements were proposed to the Transformer?"
    base = await client.post("/search/advanced", headers=h, json={"query": q, "policy": "fixed", "mode": "sparse", "rerank": False, "top_k": 3})
    assert ids["performer.txt"] not in [x["document_id"] for x in base.json()["hits"]]       # no shared words -> invisible to keyword search
    g = await client.post("/search/advanced", headers=h, json={"query": q, "policy": "fixed", "mode": "sparse", "rerank": True, "graph": True, "top_k": 8})
    body = g.json()
    docs = {x["document_id"] for x in body["hits"]}
    assert {ids["longformer.txt"], ids["linformer.txt"], ids["performer.txt"]} <= docs        # Performer reached via Linformer (2 hops)
    assert [s["name"] for s in body["graph"]["seeds"]] == ["Transformer"]
    preds = {(e["predicate"], e["depth"]) for e in body["graph"]["edges"]}
    assert ("extends", 1) in preds and ("improves", 1) in preds and any(d == 2 for _, d in preds)
    assert all(e["evidence"] and e["title"] for e in body["graph"]["edges"])
    assert {"graph", "graph_result"} <= {t["step"] for t in body["trace"]}
    d1 = await gq(client, h, q, depth=1)
    assert ids["performer.txt"] not in {x["document_id"] for x in d1["hits"]}                  # depth bound respected
    assert ids["performer.txt"] in {x["document_id"] for x in (await gq(client, h, q, depth=2))["hits"]}


async def test_entities_are_resolved_across_documents_and_browsable(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    ids = await load_corpus(client, app, h, cid)
    ents = (await client.get("/graph/entities", params={"q": "transformer"}, headers=h)).json()
    assert [e["name"] for e in ents] == ["Transformer"] and ents[0]["mentions"] >= 3
    detail = (await client.get(f"/graph/entities/{ents[0]['id']}", headers=h)).json()
    assert {m["document_id"] for m in detail["mentions"]} >= {ids["survey.txt"], ids["longformer.txt"], ids["linformer.txt"]}
    assert {(e["source"], e["predicate"]) for e in detail["edges"]} >= {("Longformer", "extends"), ("Linformer", "improves")}
    stats = (await client.get("/graph/stats", headers=h)).json()
    assert stats["entities"] >= 6 and stats["relations"] >= 4


async def test_acronym_and_expansion_share_one_entity_across_documents(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    a = (await upload(client, app, h, cid, "a.txt", b"Bidirectional Encoder Representations from Transformers (BERT) is a language model. BERT outperforms Elmo on GLUE."))["document"]["id"]
    b = (await upload(client, app, h, cid, "b.txt", b"Researchers say BERT improves Search ranking in production systems."))["document"]["id"]
    for name in ("BERT", "Bidirectional Encoder Representations from Transformers"):
        res = await gq(client, h, f"tell me about {name}")
        assert {x["document_id"] for x in res["hits"]} == {a, b}, name
    assert len({s["id"] for s in (await gq(client, h, "BERT"))["seeds"]}) == 1


async def test_reupload_replaces_graph_and_delete_removes_it(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    doc = (await upload(client, app, h, cid, "x.txt", b"Alpha extends Beta in every experiment we ran."))["document"]["id"]
    assert (await client.get("/graph/entities", params={"q": "alpha"}, headers=h)).json()
    await upload(client, app, h, cid, "x.txt", b"Gamma extends Delta in every experiment we ran.")
    assert (await client.get("/graph/entities", params={"q": "alpha"}, headers=h)).json() == []   # old version's entities garbage-collected
    assert (await client.get("/graph/entities", params={"q": "gamma"}, headers=h)).json()
    async with app.state.session_factory() as db:
        names = {e.display_name for e in (await db.execute(select(Entity))).scalars()}
        assert names == {"Gamma", "Delta"} and len((await db.execute(select(Relation))).scalars().all()) == 1
    assert (await client.delete(f"/documents/{doc}", headers=h)).status_code == 204
    stats = (await client.get("/graph/stats", headers=h)).json()
    assert stats == {"entities": 0, "relations": 0, "mentions": 0}
    async with app.state.session_factory() as db:
        assert (await db.execute(select(EntityAlias))).first() is None


async def test_graph_respects_tenants_and_acls(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    open_c, hr_c = await make_collection(client, ha, "open"), await make_collection(client, ha, "hr", restricted=True)
    await upload(client, app, ha, open_c, "o.txt", b"Alpha extends Beta in the public report.")
    await upload(client, app, ha, hr_c, "r.txt", b"Beta improves Gamma according to the confidential study.")
    ent_id = (await client.get("/graph/entities", params={"q": "alpha"}, headers=ha)).json()[0]["id"]

    # other tenant: nothing visible, even with the id or the collection ids
    assert (await gq(client, hb, "Alpha Beta Gamma", collection_ids=[open_c, hr_c]))["seeds"] == []
    assert (await client.get("/graph/entities", headers=hb)).json() == []
    assert (await client.get(f"/graph/entities/{ent_id}", headers=hb)).status_code == 404
    await client.post("/collections", json={"name": "mine"}, headers=hb)
    # same names in another org stay separate entities
    cb = (await client.get("/collections", headers=hb)).json()[0]["id"]
    await upload(client, app, hb, cb, "b.txt", b"Alpha uses Zeta throughout this note.")
    assert {e["name"] for e in (await client.get("/graph/entities", headers=hb)).json()} == {"Alpha", "Zeta"}

    # restricted collection: an editor without access cannot reach Gamma through Beta
    await client.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
    uid = (await client.get("/auth/me", headers=await login(client, "e@example.com"))).json()["id"]
    he = await login(client, "e@example.com")
    res = await gq(client, he, "Alpha", depth=3)
    assert "Gamma" not in {n.get("name") for n in res["nodes"].values()} and all("confidential" not in x["text"] for x in res["hits"])
    assert "Gamma" not in {e["name"] for e in (await client.get("/graph/entities", headers=he)).json()}
    await client.post(f"/collections/{hr_c}/access", json={"user_id": uid}, headers=ha)
    res = await gq(client, he, "Alpha", depth=3)
    assert "Gamma" in {n.get("name") for n in res["nodes"].values()}


async def test_extractor_failure_degrades_gracefully_and_reindex_recovers(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    good = app.state.graph.extractor
    app.state.graph.extractor = LLMExtractor(Scripted(exc=RuntimeError("llm down")))
    doc = (await upload(client, app, h, cid, "f.txt", b"Longformer extends Transformer with sparse attention."))["document"]["id"]
    d = (await client.get(f"/documents/{doc}", headers=h)).json()
    assert d["status"] == "ready"                                                                   # still searchable
    assert (await client.post("/search", json={"query": "sparse attention", "mode": "sparse"}, headers=h)).json()["hits"]
    assert (await client.get("/graph/entities", headers=h)).json() == []
    app.state.graph.extractor = good
    r = await client.post(f"/graph/reindex/{doc}", headers=h)
    assert r.status_code == 200 and r.json()["relations"] == 1
    assert {e["name"] for e in (await client.get("/graph/entities", headers=h)).json()} >= {"Longformer", "Transformer"}


async def test_llm_extractor_mode_end_to_end_with_mock_llm(tmp_path):
    cfg = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                   ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, graph_extractor="llm")
    a = create_app(cfg)
    async with a.router.lifespan_context(a), httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
        h = await register(c, "Acme", "a@example.com")
        cid = await make_collection(c, h)
        await upload(c, a, h, cid, "l.txt", b"Longformer extends Transformer with sparse attention. Linformer improves Transformer greatly.")
        assert a.state.graph.extractor.name.startswith("llm:")
        assert (await c.get("/graph/stats", headers=h)).json()["relations"] == 2


async def test_graph_validation_auth_and_edge_cap(client, app):
    h = await register(client, "Acme", "a@example.com")
    assert (await client.post("/graph/query", json={"query": "x", "depth": 4}, headers=h)).status_code == 422
    assert (await client.post("/graph/query", json={"query": "x"})).status_code == 401
    assert (await client.get("/graph/stats")).status_code == 401
    cid = await make_collection(client, h)
    hub = " ".join(f"Hub extends Spoke{i}x." for i in range(30))
    await upload(client, app, h, cid, "hub.txt", hub.replace("x.", "xyz.").encode())
    app.state.graph_retriever.settings.graph_max_edges_per_hop = 5
    res = await gq(client, h, "Hub", depth=1)
    assert 0 < len(res["edges"]) <= 5
