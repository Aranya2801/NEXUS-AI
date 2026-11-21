import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import AuditLog, Chunk, Document, DocumentVersion
from app.main import create_app
from tests.fixtures import make_docx, make_pdf

PW = "correct-horse-battery"


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "s"),
                    log_json=False, embedding_dim=64, ingestion_retry_backoff_seconds=0.0,
                    login_rate_limit_per_minute=5, ingestion_max_attempts=3, bcrypt_rounds=4)


@pytest_asyncio.fixture
async def app(cfg):
    a = create_app(cfg)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def register(client, org, email) -> dict:
    r = await client.post("/auth/register", json={"organization_name": org, "email": email, "password": PW})
    assert r.status_code == 201, r.text
    return await login(client, email)


async def login(client, email) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def make_collection(client, h, name="kb", restricted=False) -> str:
    r = await client.post("/collections", json={"name": name, "is_restricted": restricted}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def upload(client, app, h, cid, name, data):
    r = await client.post(f"/collections/{cid}/documents", files={"file": (name, data)}, headers=h)
    await app.state.runner.drain()
    return r


TEXT = ("Transformers rely on attention. " * 40 + "\n\n" * 2 + "Retrieval augmented generation grounds answers. " * 40).encode()


async def test_auth_flow_and_errors(client):
    h = await register(client, "Acme", "a@example.com")
    me = await client.get("/auth/me", headers=h)
    assert me.json()["role"] == "admin"
    assert (await client.post("/auth/register", json={"organization_name": "Acme", "email": "b@example.com", "password": PW})).status_code == 409
    assert (await client.post("/auth/register", json={"organization_name": "X1", "email": "c@example.com", "password": "short"})).status_code == 422
    assert (await client.post("/auth/login", json={"email": "a@example.com", "password": "wrong-password-1"})).status_code == 401
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_login_rate_limit_and_security_headers(client):
    await register(client, "Acme", "a@example.com")  # consumes 1 login
    codes = [(await client.post("/auth/login", json={"email": "a@example.com", "password": "x" * 12})).status_code for _ in range(6)]
    assert 429 in codes
    r = await client.get("/health/live")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY" and r.headers["x-request-id"]


async def test_upload_process_and_provenance(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    pdf = make_pdf([["1. INTRODUCTION", "Attention is all you need."], ["2. RESULTS", "BLEU reached 27.3."]])
    r = await upload(client, app, h, cid, "paper.pdf", pdf)
    assert r.status_code == 202 and r.json()["outcome"] == "created"
    doc_id = r.json()["document"]["id"]
    d = (await client.get(f"/documents/{doc_id}", headers=h)).json()
    assert d["status"] == "ready" and d["current_version"] == 1
    job = (await client.get(f"/documents/{doc_id}/job", headers=h)).json()
    assert job["status"] == "succeeded" and job["attempts"] == 1
    chunks = (await client.get(f"/documents/{doc_id}/chunks", headers=h)).json()
    assert {(c["page_start"], c["section"]) for c in chunks} == {(1, "1. INTRODUCTION"), (2, "2. RESULTS")}
    async with app.state.session_factory() as db:
        kids = (await db.execute(select(Chunk).where(Chunk.document_id == doc_id, Chunk.level == "child"))).scalars().all()
        assert all(len(k.embedding) == 64 and k.parent_id and k.is_current for k in kids)
        actions = {a.action for a in (await db.execute(select(AuditLog))).scalars()}
        assert {"auth.register", "auth.login", "collection.create", "document.upload"} <= actions


async def test_duplicate_and_versioning_swap(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    first = (await upload(client, app, h, cid, "notes.txt", TEXT)).json()
    again = (await upload(client, app, h, cid, "copy.txt", TEXT)).json()  # same bytes, other filename
    assert again["outcome"] == "duplicate" and again["document"]["id"] == first["document"]["id"] and again["job"] is None
    v2 = (await upload(client, app, h, cid, "notes.txt", TEXT + b"\n\nA new appendix about knowledge graphs.")).json()
    assert v2["outcome"] == "new_version" and v2["document"]["id"] == first["document"]["id"]
    async with app.state.session_factory() as db:
        doc = await db.get(Document, first["document"]["id"])
        assert doc.current_version == 2
        vers = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id).order_by(DocumentVersion.version))).scalars().all()
        assert [(v.version, v.is_current) for v in vers] == [(1, False), (2, True)] and vers[0].superseded_at
        cur = (await db.execute(select(Chunk.version).where(Chunk.document_id == doc.id, Chunk.is_current.is_(True)))).scalars().all()
        assert set(cur) == {2}


async def test_tenant_isolation_and_roles(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    hb = await register(client, "OrgB", "b@example.com")
    cid = await make_collection(client, ha)
    doc = (await upload(client, app, ha, cid, "secret.txt", TEXT)).json()["document"]["id"]
    assert (await client.get(f"/documents/{doc}", headers=hb)).status_code == 404
    assert (await client.get(f"/documents/{doc}/chunks", headers=hb)).status_code == 404
    assert (await client.get("/documents", headers=hb)).json() == []
    assert (await client.post(f"/collections/{cid}/documents", files={"file": ("x.txt", b"hi")}, headers=hb)).status_code == 404
    assert (await client.delete(f"/documents/{doc}", headers=hb)).status_code == 404
    # viewer inside OrgA: can read, cannot upload or create
    await client.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=ha)
    hv = await login(client, "v@example.com")
    assert (await client.get(f"/documents/{doc}", headers=hv)).status_code == 200
    assert (await client.post(f"/collections/{cid}/documents", files={"file": ("x.txt", b"hi")}, headers=hv)).status_code == 403
    assert (await client.post("/auth/users", json={"email": "z@example.com", "password": PW}, headers=hv)).status_code == 403


async def test_restricted_collection_acl(client, app):
    ha = await register(client, "OrgA", "a@example.com")
    cid = await make_collection(client, ha, "hr", restricted=True)
    doc = (await upload(client, app, ha, cid, "salaries.txt", TEXT)).json()["document"]["id"]
    uid = (await client.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)).json()["id"]
    he = await login(client, "e@example.com")
    assert (await client.get(f"/documents/{doc}", headers=he)).status_code == 404
    assert [c["id"] for c in (await client.get("/collections", headers=he)).json()] == []
    assert (await client.post(f"/collections/{cid}/access", json={"user_id": uid}, headers=ha)).status_code == 204
    assert (await client.get(f"/documents/{doc}", headers=he)).status_code == 200


async def test_upload_validation_rejections(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    assert (await upload(client, app, h, cid, "evil.exe", b"MZ....")).status_code == 422
    assert (await upload(client, app, h, cid, "fake.pdf", b"not a pdf")).status_code == 422
    app.state.settings.max_upload_mb = 1
    assert (await upload(client, app, h, cid, "big.txt", b"a" * (1024 * 1024 + 10))).status_code == 422


async def test_retry_then_success_and_dead_letter(client, app, monkeypatch):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    emb, real, calls = app.state.ingestion.embedder, app.state.ingestion.embedder.embed, {"n": 0}

    async def flaky(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("embedding service down")
        return await real(texts)

    monkeypatch.setattr(emb, "embed", flaky)
    r = await upload(client, app, h, cid, "a.txt", TEXT)
    job = (await client.get(f"/documents/{r.json()['document']['id']}/job", headers=h)).json()
    assert job["status"] == "succeeded" and job["attempts"] == 2

    async def always_down(texts):
        raise ConnectionError("down")

    monkeypatch.setattr(emb, "embed", always_down)
    r = await upload(client, app, h, cid, "b.txt", TEXT + b" different")
    did = r.json()["document"]["id"]
    job = (await client.get(f"/documents/{did}/job", headers=h)).json()
    assert job["status"] == "dead" and job["attempts"] == 3 and "ConnectionError" in job["error"]
    assert (await client.get(f"/documents/{did}", headers=h)).json()["status"] == "failed"


async def test_corrupt_file_fails_fast_and_old_version_survives(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    ok = (await upload(client, app, h, cid, "r.docx", make_docx())).json()["document"]["id"]
    # same logical file, corrupt new version (valid zip container, broken XML)
    import io, zipfile
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>"); zf.writestr("word/document.xml", "<broken")
    r = await upload(client, app, h, cid, "r.docx", z.getvalue())
    assert r.json()["document"]["id"] == ok and r.json()["outcome"] == "new_version"
    job = (await client.get(f"/documents/{ok}/job", headers=h)).json()
    assert job["status"] == "dead" and job["attempts"] == 1  # permanent errors are not retried
    d = (await client.get(f"/documents/{ok}", headers=h)).json()
    assert d["status"] == "ready" and d["current_version"] == 1  # previous version still searchable
    assert len((await client.get(f"/documents/{ok}/chunks", headers=h)).json()) > 0


async def test_delete_document_removes_everything(client, app):
    h = await register(client, "Acme", "a@example.com")
    cid = await make_collection(client, h)
    did = (await upload(client, app, h, cid, "a.txt", TEXT)).json()["document"]["id"]
    assert (await client.delete(f"/documents/{did}", headers=h)).status_code == 204
    async with app.state.session_factory() as db:
        assert (await db.execute(select(Chunk).where(Chunk.document_id == did))).first() is None
    assert (await client.get(f"/documents/{did}", headers=h)).status_code == 404
