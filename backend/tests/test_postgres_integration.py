"""Runs the retrieval stack against a REAL PostgreSQL + pgvector (via the `pgserver` dev dependency).
Skipped automatically when pgserver is unavailable. Exercises: pgvector `<=>` + HNSW, FTS (`ts_rank_cd`), DDL events,
asyncpg vector codec, and the same tenant/ACL/version guarantees as the SQLite tests."""
import re
import tempfile
import uuid

import httpx
import pytest
import pytest_asyncio

try:
    import pgserver
except ImportError:   # the maintained fork ships the same API under another module name
    pgserver = pytest.importorskip("pixeltable_pgserver")

from sqlalchemy import text  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.models.documents import EMBEDDING_DIM  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.helpers import PW, login, make_collection, register, search, seed, upload  # noqa: E402


@pytest.fixture(scope="module")
def pg():
    d = tempfile.mkdtemp(prefix="nexus_pg_")
    try:
        srv = pgserver.get_server(d, cleanup_mode="stop")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"cannot start pgserver: {exc}")
    yield srv
    srv.cleanup()


@pytest_asyncio.fixture
async def app(pg, tmp_path):
    name = "t_" + uuid.uuid4().hex[:10]
    pg.psql(f"CREATE DATABASE {name}")
    host = re.search(r"host=([^&]+)", pg.get_uri()).group(1)
    cfg = Settings(env="test", database_url=f"postgresql+asyncpg://postgres@/{name}?host={host}", storage_dir=str(tmp_path / "s"),
                   log_json=False, embedding_dim=EMBEDDING_DIM, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4)
    a = create_app(cfg)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_schema_has_vector_fts_and_hnsw_indexes(app):
    async with app.state.session_factory() as db:
        idx = {r[0] for r in (await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='chunks'"))).all()}
        ext = (await db.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar_one()
    assert {"ix_chunks_fts", "ix_chunks_embedding_hnsw"} <= idx and ext


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
async def test_modes_rank_correctly_on_postgres(client, app, mode):
    h = await register(client, "Acme", "a@example.com")
    ids = await seed(client, app, h, await make_collection(client, h))
    for q, exp in [("self-attention transformer tokens", "attention.txt"), ("kubernetes container orchestration pods", "kubernetes.txt"),
                   ("invoice ledger audit reconcile", "billing.txt")]:
        r = await search(client, h, q, mode=mode, top_k=3)
        assert r.status_code == 200, r.text
        assert r.json()["hits"][0]["document_id"] == ids[exp], (mode, q)
    # stored vectors really are pgvector rows of the configured dimension
    async with app.state.session_factory() as db:
        dim = (await db.execute(text("SELECT vector_dims(embedding) FROM chunks WHERE embedding IS NOT NULL LIMIT 1"))).scalar_one()
    assert dim == EMBEDDING_DIM


async def test_isolation_acl_and_versions_on_postgres(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    open_c = await make_collection(client, ha, "open")
    hr_c = await make_collection(client, ha, "hr", restricted=True)
    await seed(client, app, ha, open_c)
    await upload(client, app, ha, hr_c, "salaries.txt", b"Confidential salary bands for engineering staff " * 8)
    for mode in ("dense", "sparse", "hybrid"):
        assert (await search(client, hb, "salary invoice kubernetes", mode=mode, collection_ids=[open_c, hr_c])).json()["hits"] == []
    await client.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
    he = await login(client, "e@example.com")
    for mode in ("dense", "sparse", "hybrid"):
        got = (await search(client, he, "confidential salary bands engineering", mode=mode)).json()["hits"]
        assert all(x["collection_id"] != hr_c for x in got)
    # version swap
    await upload(client, app, ha, open_c, "policy.txt", b"The legacy refund policy allows returns within thirty days " * 6)
    await upload(client, app, ha, open_c, "policy.txt", b"The modern refund policy allows returns within sixty days " * 6)
    for mode in ("sparse", "dense"):
        hits = (await search(client, ha, "refund policy returns", mode=mode, top_k=50)).json()["hits"]
        pol = [x for x in hits if x["filename"] == "policy.txt"]
        assert pol and all(x["version"] == 2 for x in pol)
    hist = (await search(client, ha, "refund policy returns", mode="sparse", temporal="all_versions", top_k=50)).json()["hits"]
    assert {x["version"] for x in hist if x["filename"] == "policy.txt"} == {1, 2}


async def test_advanced_pipeline_on_postgres(client, app):
    h = await register(client, "Acme", "a@example.com")
    ids = await seed(client, app, h, await make_collection(client, h))
    r = await client.post("/search/advanced", headers=h, json={"query": "Compare kubernetes container orchestration and self-attention transformer tokens"})
    assert r.status_code == 200, r.text
    b = r.json()
    docs = [x["document_id"] for x in b["hits"][:6]]
    assert b["plan"]["decompose"] and ids["kubernetes.txt"] in docs and ids["attention.txt"] in docs
    r = await client.post("/search/advanced", headers=h, json={"query": "quantum flux capacitor"})
    assert len(r.json()["attempts"]) == 3  # escalation ladder is bounded
    hb = await register(client, "OrgB", "b@example.com")
    r = await client.post("/search/advanced", headers=hb, json={"query": "kubernetes", "policy": "fixed", "hyde": True, "multi_query": 3})
    assert r.json()["hits"] == []


async def test_knowledge_graph_on_postgres(client, app):
    from tests.test_knowledge_graph import CORPUS, gq, load_corpus

    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    open_c = await make_collection(client, ha, "open")
    hr_c = await make_collection(client, ha, "hr", restricted=True)
    ids = await load_corpus(client, app, ha, open_c)
    q = "What improvements were proposed to the Transformer?"
    # multi-hop reaches Performer (no shared words with the query) only through Linformer
    d1 = await gq(client, ha, q, depth=1)
    d2 = await gq(client, ha, q, depth=2)
    assert ids["performer.txt"] not in {x["document_id"] for x in d1["hits"]}
    assert ids["performer.txt"] in {x["document_id"] for x in d2["hits"]}
    # advanced pipeline with graph + rerank
    r = await client.post("/search/advanced", headers=ha, json={"query": q, "policy": "fixed", "mode": "sparse", "graph": True, "top_k": 8})
    assert {ids["longformer.txt"], ids["linformer.txt"], ids["performer.txt"]} <= {x["document_id"] for x in r.json()["hits"]}
    # tenant + ACL isolation
    assert (await gq(client, hb, "Transformer Longformer"))["seeds"] == []
    await upload(client, app, ha, hr_c, "secret.txt", b"Transformer improves Zorblax according to the confidential memo.")
    await client.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
    he = await login(client, "e@example.com")
    res = await gq(client, he, "Transformer", depth=2)
    assert "Zorblax" not in {n.get("name") for n in res["nodes"].values()}
    assert "Zorblax" in {n.get("name") for n in (await gq(client, ha, "Transformer", depth=2))["nodes"].values()}
    # cleanup on delete
    sid = (await client.get("/documents", params={"collection_id": hr_c}, headers=ha)).json()[0]["id"]
    assert (await client.delete(f"/documents/{sid}", headers=ha)).status_code == 204
    assert "Zorblax" not in {e["name"] for e in (await client.get("/graph/entities", headers=ha)).json()}


async def test_research_workflow_on_postgres(client, app):
    from tests.test_knowledge_graph import CORPUS, load_corpus

    # Auto-review thresholds are backend-sensitive: this query scores evidence confidence ~0.23 on PostgreSQL FTS (below the 0.25
    # review threshold) but higher on SQLite BM25. Pin the mode so this test checks workflow plumbing, not threshold calibration.
    app.state.settings.human_review_mode = "never"
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    await load_corpus(client, app, ha, await make_collection(client, ha))
    r = await client.post("/chat", json={"message": "Which papers discuss the Transformer and what limitations did they report?"}, headers=ha)
    assert r.status_code == 200, r.text
    j = r.json()
    nodes = [t["node"] for t in j["trace"]]
    assert j["status"] == "completed" and j["answer_status"] == "grounded" and j["citations"]
    assert "graph_branch" in nodes and nodes.count("retrieve_branch") >= 1 and j["verification"]["unsupported"] == 0
    # human review round-trip
    rv = (await client.post("/chat", json={"message": "What improvements were proposed to the Transformer?", "review": "always"}, headers=ha)).json()
    assert rv["status"] == "needs_review"
    done = (await client.post(f"/agents/runs/{rv['run_id']}/resume", json={"decision": "approve"}, headers=ha)).json()
    assert done["status"] == "completed" and done["citations"]
    # tenant isolation end to end
    jb = (await client.post("/chat", json={"message": "What improvements were proposed to the Transformer?"}, headers=hb)).json()
    assert jb["answer_status"] == "insufficient_evidence" and jb["evidence"] == []


async def test_reports_and_memory_on_postgres(client, app):
    import io

    from pypdf import PdfReader

    from tests.fixtures import make_pdf
    from tests.test_reports import PAPERS, TOPIC

    app.state.settings.human_review_mode = "never"
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    cid = await make_collection(client, ha)
    for n, pages in PAPERS.items():
        await upload(client, app, ha, cid, n, make_pdf(pages))
    r = await client.post("/reports", json={"topic": TOPIC}, headers=ha)
    await app.state.reports.drain()
    rep = (await client.get(f"/reports/{r.json()['id']}", headers=ha)).json()
    assert rep["status"] == "ready", rep["error"]
    nums = {row["cells"][2] for s in rep["content"]["sections"] for b in s["blocks"] if b["type"] == "table" for row in b["rows"]}
    assert nums == {"18.3", "21.9", "71.2"} and len(rep["content"]["documents"]) == 3
    pdf = "".join(p.extract_text() for p in PdfReader(io.BytesIO((await client.get(f"/reports/{rep['id']}/export", headers=ha)).content)).pages)
    assert "21.9" in pdf and "p.2" in pdf
    assert (await client.get(f"/reports/{rep['id']}", headers=hb)).status_code == 404
    assert (await make_report_b(client, app, hb))["content"]["documents"] == []
    # memory on PostgreSQL: upsert + unique key, JSON values, research-context scoping, expiry filter
    assert (await client.put("/memory/preferences/style", json={"value": {"tone": "concise"}}, headers=ha)).status_code == 200
    assert (await client.put("/memory/preferences/style", json={"value": {"tone": "detailed"}}, headers=ha)).status_code == 200
    items = (await client.get("/memory", params={"kind": "long_term"}, headers=ha)).json()
    assert len(items) == 1 and items[0]["value"] == {"tone": "detailed"}
    await client.get(f"/documents/{(await client.get('/documents', headers=ha)).json()[0]['id']}/chunks", headers=ha)
    assert (await client.get("/memory/documents/recent", headers=ha)).json()
    assert (await client.get("/memory", headers=hb)).json() == []


async def make_report_b(client, app, h):
    r = await client.post("/reports", json={"topic": "transformer architectures"}, headers=h)
    await app.state.reports.drain()
    return (await client.get(f"/reports/{r.json()['id']}", headers=h)).json()


@pytest_asyncio.fixture
async def pg_cp_app(pg, tmp_path):
    name = "cp_" + uuid.uuid4().hex[:10]
    pg.psql(f"CREATE DATABASE {name}")
    host = re.search(r"host=([^&]+)", pg.get_uri()).group(1)
    cfg = Settings(env="test", database_url=f"postgresql+asyncpg://postgres@/{name}?host={host}", storage_dir=str(tmp_path / "s"),
                   log_json=False, embedding_dim=EMBEDDING_DIM, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4,
                   checkpoint_backend="postgres", human_review_mode="always")
    a = create_app(cfg)
    async with a.router.lifespan_context(a):
        yield a


async def test_postgres_checkpointer_persists_interrupted_run_and_resumes(pg_cp_app):
    app = pg_cp_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        h = await register(c, "Acme", "a@example.com")
        await seed(c, app, h, await make_collection(c, h))
        r = await c.post("/chat", json={"message": "kubernetes container orchestration pods", "review": "always"}, headers=h)
        j = r.json()
        assert r.status_code == 200 and j["status"] == "needs_review"
        async with app.state.session_factory() as db:   # the paused graph state really lives in Postgres, not memory
            n = (await db.execute(text("SELECT count(*) FROM checkpoints"))).scalar_one()
        assert n > 0
        done = (await c.post(f"/agents/runs/{j['run_id']}/resume", json={"decision": "approve"}, headers=h)).json()
        assert done["status"] == "completed" and done["answer"]


def test_alembic_migrations_upgrade_match_models_and_downgrade(pg):
    """`alembic upgrade head` on an empty database creates the full schema (incl. pgvector + HNSW + FTS); `alembic check` proves the
    migration has not drifted from the SQLAlchemy models; downgrade removes everything."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    name = "mig_" + uuid.uuid4().hex[:10]
    pg.psql(f"CREATE DATABASE {name}")
    host = re.search(r"host=([^&]+)", pg.get_uri()).group(1)
    env = dict(os.environ, NEXUS_DATABASE_URL=f"postgresql+asyncpg://postgres@/{name}?host={host}")
    backend = Path(__file__).resolve().parents[1]

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend, env=env, capture_output=True, text=True)

    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr[-1500:]
    chk = alembic("check")
    assert chk.returncode == 0, chk.stderr[-1500:]
    import asyncio

    import asyncpg

    async def inspect():
        conn = await asyncpg.connect(user="postgres", host=host, database=name)
        try:
            tables = {r[0] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            idx = {r[0] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename='chunks'")}
            return tables, idx
        finally:
            await conn.close()
    tables, idx = asyncio.run(inspect())
    assert {"organizations", "users", "documents", "chunks", "agent_runs", "reports", "feedback", "entities", "relations"} <= tables
    assert {"ix_chunks_fts", "ix_chunks_embedding_hnsw"} <= idx
    down = alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr[-1500:]
    assert "chunks" not in asyncio.run(inspect())[0]


async def test_sql_connector_against_real_postgres_is_read_only(pg, app):
    """The SQL connector's database-level guard (default_transaction_read_only) on a real server, not just the lexical guard."""
    import asyncio

    from sqlalchemy import create_engine

    from app.connectors.sql_source import SqlConnector

    name = "src_" + uuid.uuid4().hex[:8]
    pg.psql(f"CREATE DATABASE {name}")
    host = re.search(r"host=([^&]+)", pg.get_uri()).group(1)
    url = f"postgresql://postgres@/{name}?host={host}"
    eng = create_engine(url.replace("postgresql://", "postgresql+psycopg://"), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("CREATE TABLE tickets(id int primary key, title text, body text)"))
        c.execute(text("INSERT INTO tickets VALUES (1,'VPN','VPN drops hourly for the hippo office'),(2,'Printer','Printer jams with wombat error')"))
    eng.dispose()
    s = app.state.settings.model_copy(update={"sql_connector_allowed_dialects": ("postgresql",)})
    conn = SqlConnector(url, "SELECT id, title, body FROM tickets", "id", None, "title", None, None, True, s)
    res = await conn.scan(None, 10)
    assert sorted(i.name for i in res.items) == ["Printer", "VPN"] and "hippo" in (await conn.fetch(res.items[0]))[0].decode() + (await conn.fetch(res.items[1]))[0].decode()
    cu, args = conn._connect_args()
    with create_engine(cu, connect_args=args).connect() as c:
        with pytest.raises(Exception, match="read-only"):
            c.execute(text("INSERT INTO tickets VALUES (3,'x','y')"))      # lexical guard bypassed: the server still refuses
