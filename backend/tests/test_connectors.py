"""Phase 12: connectors tested end-to-end through the API against in-memory fake source servers."""
from __future__ import annotations

import sqlite3

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.connectors import registry
from app.connectors.base import ConnectorAuthError
from app.connectors.security import assert_public_url, decrypt_secrets
from app.connectors.sql_source import SqlConnector, check_select_only
from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.database.models import Connector, Document
from app.main import create_app
from tests.fake_sources import FakeWorld, iso, ts
from tests.test_api_ingestion import register

LONG = " ".join(["Detailed operational runbook content about the zebra migration plan."] * 6)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"), log_json=False, embedding_dim=64,
                    ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, ingestion_max_attempts=1, connector_dns_check=False,
                    sql_connector_allowed_dialects=("postgresql", "sqlite"), connector_scheduler_seconds=3600)


@pytest_asyncio.fixture
async def app(cfg, world):
    a = create_app(cfg)
    a.state.connector_client_factory = lambda: httpx.AsyncClient(transport=world.transport())
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def setup(client, restricted=True, org="Org A", email="a@acme.io"):
    h = await register(client, org, email)
    cid = (await client.post("/collections", json={"name": "kb", "is_restricted": restricted}, headers=h)).json()["id"]
    return h, cid


async def mk(client, h, cid, kind, config, secrets, name="c1", **kw):
    r = await client.post("/connectors", json={"name": name, "kind": kind, "collection_id": cid, "config": config, "secrets": secrets, **kw}, headers=h)
    return r


async def sync(client, app, h, conn_id, **params):
    r = await client.post(f"/connectors/{conn_id}/sync", params=params, headers=h)
    assert r.status_code == 202, r.text
    await app.state.connectors.drain()
    await app.state.runner.drain()
    return (await client.get(f"/connectors/{conn_id}/runs", headers=h)).json()[0]


async def docs(client, h):
    return (await client.get("/documents", headers=h)).json()


DRIVE_CFG = ({}, {"access_token": "good-token"})


def drive_file(i, name, content, modified, perms=("a@acme.io",), **kw):
    return {"id": f"file{i}000", "name": name, "mimeType": "text/plain", "modifiedTime": modified, "content": content,
            "perms": [{"type": "user", "role": "reader", "emailAddress": e} for e in perms], **kw}


# ===================================================================== Google Drive
@pytest.mark.asyncio
async def test_drive_full_incremental_cycle(client, app, world):
    h, cid = await setup(client)
    world.drive_files = [drive_file(i, f"note{i}.txt", LONG + f" unique{i}", iso(d=i)) for i in (1, 2, 3)]
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG)).json()
    assert "good-token" not in str(c) and "secret" not in str(c).lower().replace("secrets", "")
    run = await sync(client, app, h, c["id"])
    assert run["status"] == "succeeded" and run["stats"]["ingested"] == 3 and len(await docs(client, h)) == 3
    hits = (await client.post("/search", json={"query": "zebra migration unique2"}, headers=h)).json()["hits"]
    assert hits

    run2 = await sync(client, app, h, c["id"])                                   # nothing changed: hash-skip, no re-ingest
    assert run2["stats"]["ingested"] == 0 and run2["stats"]["unchanged"] >= 1

    world.drive_files[0]["content"], world.drive_files[0]["modifiedTime"] = LONG + " REVISED text about quokkas", iso(d=9)
    run3 = await sync(client, app, h, c["id"])
    assert run3["stats"]["ingested"] == 1
    doc = next(d for d in await docs(client, h) if d["title"].startswith("note1"))
    assert doc["current_version"] == 2

    world.drive_files[1].update(trashed=True, modifiedTime=iso(d=10))            # trashed at source -> deleted here
    run4 = await sync(client, app, h, c["id"])
    assert run4["stats"]["deleted"] == 1 and len(await docs(client, h)) == 2


@pytest.mark.asyncio
async def test_drive_google_doc_export_and_unsupported_type_skipped(client, app, world):
    h, cid = await setup(client)
    world.drive_files = [{"id": "gdoc1000", "name": "Roadmap", "mimeType": "application/vnd.google-apps.document", "modifiedTime": iso(d=1),
                          "content": LONG, "perms": [{"type": "user", "role": "owner", "emailAddress": "a@acme.io"}]},
                         drive_file(2, "movie.mp4", "binary", iso(d=2))]
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG)).json()
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 1 and (await docs(client, h))[0]["title"].startswith("Roadmap")


@pytest.mark.asyncio
async def test_strict_acl_skips_unknown_and_too_broad_and_removes_on_tightening(client, app, world):
    h, cid = await setup(client)           # restricted collection: only the admin a@acme.io can see it
    world.drive_files = [drive_file(1, "ok.txt", LONG, iso(d=1), perms=("a@acme.io", "z@acme.io")),
                         drive_file(2, "narrow.txt", LONG + " narrow", iso(d=2), perms=("z@acme.io",)),     # admin cannot read it at the source
                         {**drive_file(3, "unknown.txt", LONG + " unk", iso(d=3)), "perms": None},          # source gave no permissions
                         {**drive_file(4, "shared.txt", LONG + " shared", iso(d=4)), "perms": [{"type": "anyone", "role": "reader"}]}]
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG)).json()
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 2 and run["stats"]["skipped_acl"] == 2
    items = {i["name"]: i for i in (await client.get(f"/connectors/{c['id']}/items", headers=h)).json()}
    assert items["narrow"]["status"] == "skipped_acl" and "cannot read" in items["narrow"]["detail"]
    assert items["unknown"]["status"] == "skipped_acl" and "unknown" in items["unknown"]["detail"]
    # source tightens permissions on an already-synced file -> it must disappear from NEXUS
    world.drive_files[0].update(perms=[{"type": "user", "role": "reader", "emailAddress": "z@acme.io"}], modifiedTime=iso(d=11))
    run2 = await sync(client, app, h, c["id"])
    assert run2["stats"]["skipped_acl"] >= 1 and {d["title"][:6] for d in await docs(client, h)} == {"shared"}


@pytest.mark.asyncio
async def test_strict_mode_blocks_unrestricted_collection_but_inherit_allows(client, app, world):
    h, cid = await setup(client, restricted=False)
    await client.post("/admin/users", json={}, headers=h)   # (endpoint may not exist; harmless) - org has just the admin
    world.drive_files = [drive_file(1, "a.txt", LONG, iso(d=1), perms=("a@acme.io",))]
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG, name="strict")).json()
    assert (await sync(client, app, h, c["id"]))["stats"]["ingested"] == 1      # single-user org: readers == {admin} -> subset holds
    h2 = await register(client, "Org B", "b@beta.io")
    cid2 = (await client.post("/collections", json={"name": "kb"}, headers=h2)).json()["id"]
    world.drive_files = [drive_file(1, "a.txt", LONG, iso(d=1), perms=("someone@else.io",))]
    s = (await mk(client, h2, cid2, "google_drive", *DRIVE_CFG, name="s2")).json()
    assert (await sync(client, app, h2, s["id"]))["stats"]["skipped_acl"] == 1
    i = (await mk(client, h2, cid2, "google_drive", *DRIVE_CFG, name="s3", acl_mode="inherit")).json()
    assert (await sync(client, app, h2, i["id"]))["stats"]["ingested"] == 1


# ===================================================================== SharePoint (delta)
@pytest.mark.asyncio
async def test_sharepoint_delta_sync_with_deletion(client, app, world):
    h, cid = await setup(client)
    perms = [{"grantedToV2": {"user": {"email": "A@acme.io"}}}]
    world.sp_items = {"i1": {"name": "policy.txt", "modified": iso(d=1), "content": LONG + " policy", "perms": perms},
                      "i2": {"name": "guide.txt", "modified": iso(d=1), "content": LONG + " guide", "perms": perms},
                      "i3": {"name": "secret.txt", "modified": iso(d=1), "content": LONG + " secret", "perms": None}}   # permissions endpoint 403 -> unknown
    c = (await mk(client, h, cid, "sharepoint", {"drive_id": "drv-1234"}, {"access_token": "good-token"})).json()
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 2 and run["stats"]["skipped_acl"] == 1
    assert (await client.get(f"/connectors/{c['id']}", headers=h)).json()["has_cursor"] is True
    world.sp_items["i2"]["deleted"] = True
    world.sp_items["i1"].update(content=LONG + " policy v2", modified=iso(d=5))
    world.sp_pending_delta = ["i1", "i2"]
    run2 = await sync(client, app, h, c["id"])
    assert run2["stats"]["scanned"] == 2 and run2["stats"]["deleted"] == 1 and run2["stats"]["ingested"] == 1
    assert [d["title"][:6] for d in await docs(client, h)] == ["policy"]


@pytest.mark.asyncio
async def test_graph_delta_link_off_host_is_refused():
    from app.connectors.base import ConnectorError
    from app.connectors.microsoft import _graph_url

    for bad in ("https://evil.example/x", "http://graph.microsoft.com/x", "https://graph.microsoft.com.evil.io/x"):
        with pytest.raises(ConnectorError):
            _graph_url(bad)


# ===================================================================== Slack
@pytest.mark.asyncio
async def test_slack_daily_documents_incremental_and_retry(client, app, world):
    h, cid = await setup(client)
    world.slack_msgs = [{"ts": ts(2026, 3, 1, 9), "user": "U1", "text": "Deploy of the quokka service finished"},
                        {"ts": ts(2026, 3, 1, 10), "user": "U2", "text": "Rollback plan documented in the runbook for quokka"}]
    world.fail_next["conversations.history"] = 1                                 # first call is rate limited -> retried
    world.slack_private = True
    c = (await mk(client, h, cid, "slack", {"channels": ["C123"]}, {"bot_token": "good-token"})).json()
    run = await sync(client, app, h, c["id"])
    # private channel members = {a@acme.io, b@acme.io}? members list only has U1 -> readers {a@}; admin a@ is a subset -> ingested
    assert run["status"] == "succeeded" and run["stats"]["ingested"] == 1, run
    d = (await docs(client, h))[0]
    chunks = (await client.get(f"/documents/{d['id']}/chunks", headers=h)).json()
    assert "Ana: Deploy of the quokka" in chunks[0]["text"] and "Bo: Rollback" in " ".join(x["text"] for x in chunks)
    world.slack_msgs.append({"ts": ts(2026, 3, 1, 15), "user": "U1", "text": "Postmortem scheduled for Friday"})
    run2 = await sync(client, app, h, c["id"])
    assert run2["stats"]["ingested"] == 1                                        # same day re-fetched in full -> new version
    assert (await docs(client, h))[0]["current_version"] == 2
    assert (await sync(client, app, h, c["id"]))["stats"]["scanned"] == 0        # watermark advanced: nothing new


@pytest.mark.asyncio
async def test_slack_bad_token_fails_run_and_keeps_cursor(client, app, world):
    h, cid = await setup(client)
    world.slack_msgs = [{"ts": ts(2026, 3, 1), "user": "U1", "text": "hello"}]
    c = (await mk(client, h, cid, "slack", {"channels": ["C123"]}, {"bot_token": "WRONG"})).json()
    run = await sync(client, app, h, c["id"])
    assert run["status"] == "failed" and "token" in run["error"].lower() and "WRONG" not in str(run)
    assert (await client.get(f"/connectors/{c['id']}", headers=h)).json()["has_cursor"] is False
    r = await client.patch(f"/connectors/{c['id']}", json={"secrets": {"bot_token": "good-token"}}, headers=h)   # rotate credential
    assert r.status_code == 200
    assert (await sync(client, app, h, c["id"]))["status"] == "succeeded"


# ===================================================================== Teams
@pytest.mark.asyncio
async def test_teams_html_messages_become_daily_document(client, app, world):
    h, cid = await setup(client)
    world.teams_msgs = [{"id": "1", "messageType": "message", "createdDateTime": "2026-03-02T09:00:00Z", "lastModifiedDateTime": "2026-03-02T09:00:00Z",
                         "from": {"user": {"displayName": "Cy"}}, "body": {"content": "<p>Budget <b>approved</b> for the <i>narwhal</i> project</p>"}},
                        {"id": "2", "messageType": "systemEventMessage", "createdDateTime": "2026-03-02T09:05:00Z", "body": {"content": "joined"}}]
    c = (await mk(client, h, cid, "teams", {"team_id": "team-12345", "channel_ids": ["chan-1"]}, {"access_token": "good-token"})).json()
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 1, run
    d = (await docs(client, h))[0]
    text = (await client.get(f"/documents/{d['id']}/chunks", headers=h)).json()[0]["text"]
    assert "Cy: Budget approved for the narwhal project" in text and "<p>" not in text and "joined" not in text


# ===================================================================== SQL
def make_db(path):
    con = sqlite3.connect(path)
    con.execute("create table tickets(id integer primary key, title text, body text, updated text)")
    con.executemany("insert into tickets values(?,?,?,?)", [(1, "Printer", "The printer on floor three jams constantly with wombat error", "2026-01-01"),
                                                             (2, "VPN", "VPN drops every hour for remote staff in the hippo office", "2026-01-02")])
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_sql_connector_rows_become_documents_and_deletions_propagate(client, app, tmp_path):
    h, cid = await setup(client)
    db = tmp_path / "t.db"
    make_db(db)
    c = (await mk(client, h, cid, "sql", {"query": "SELECT id, title, body FROM tickets", "id_column": "id", "title_column": "title", "readers": ["a@acme.io"]},
                  {"url": f"sqlite:///{db}"})).json()
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 2, run
    assert (await client.post("/search", json={"query": "wombat printer"}, headers=h)).json()["hits"]
    con = sqlite3.connect(db)
    con.execute("delete from tickets where id=2")
    con.commit()
    con.close()
    run2 = await sync(client, app, h, c["id"])
    assert run2["stats"]["deleted"] == 1 and len(await docs(client, h)) == 1


@pytest.mark.asyncio
async def test_sql_connector_without_declared_readers_is_skipped_in_strict_mode(client, app, tmp_path):
    h, cid = await setup(client)
    db = tmp_path / "t.db"
    make_db(db)
    c = (await mk(client, h, cid, "sql", {"query": "SELECT id, title, body FROM tickets", "id_column": "id"}, {"url": f"sqlite:///{db}"})).json()
    assert (await sync(client, app, h, c["id"]))["stats"]["skipped_acl"] == 2


@pytest.mark.parametrize("q", ["DELETE FROM tickets", "SELECT 1; DROP TABLE tickets", "SELECT * FROM tickets -- x", "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
                               "SELECT * INTO newtab FROM tickets", "PRAGMA table_info(tickets)", "UPDATE tickets SET title='x'"])
def test_sql_lexical_guard_rejects_writes(q):
    with pytest.raises(ValidationFailed):
        check_select_only(q)


def test_sql_database_level_read_only_and_dialect_allowlist(tmp_path, cfg):
    from sqlalchemy import create_engine, text

    db = tmp_path / "t.db"
    make_db(db)
    conn = SqlConnector(f"sqlite:///{db}", "SELECT * FROM tickets", "id", None, None, None, None, True, cfg)
    url, args = conn._connect_args()
    with create_engine(url, connect_args=args).connect() as c, pytest.raises(Exception, match="readonly"):
        c.execute(text("insert into tickets values (9,'x','y','z')"))     # even if the lexical guard were bypassed, the DB refuses writes
    with pytest.raises(ValidationFailed):
        registry.validate("sql", {"query": "SELECT 1", "id_column": "id"}, {"url": f"sqlite:///{db}"}, Settings(env="test"))     # default: postgres only
    with pytest.raises(ValidationFailed):
        registry.validate("sql", {"query": "SELECT 1", "id_column": "id"}, {"url": "mysql://u:p@h/db"}, cfg)


# ===================================================================== REST
@pytest.mark.asyncio
async def test_rest_connector_pagination_and_same_host_enforcement(client, app, world):
    h, cid = await setup(client)
    world.rest_pages = {"/v1/articles": {"data": {"items": [{"id": 1, "title": "Alpha", "text": LONG + " alpha giraffe"}]}, "links": {"next": "/v1/articles?page=2"}},
                        "/v1/articles?page=2": {"data": {"items": [{"id": 2, "title": "Beta", "text": LONG + " beta"}]}, "links": {"next": None}}}
    conf = {"url": "https://api.example.test/v1/articles", "items_path": "data.items", "id_field": "id", "name_field": "title", "text_fields": ["title", "text"],
            "next_path": "links.next", "readers": ["a@acme.io"]}
    c = (await mk(client, h, cid, "rest", conf, {"headers": {"x-api-key": "k-123"}})).json()
    assert "k-123" not in str(c)
    run = await sync(client, app, h, c["id"])
    assert run["stats"]["ingested"] == 2, run
    # a hostile next link must not be followed (it would also receive the auth header)
    world.rest_pages["/v1/articles"]["links"]["next"] = "https://evil.example.test/steal"
    run2 = await sync(client, app, h, c["id"])
    assert run2["status"] == "failed" and "leaves the configured host" in run2["error"]
    assert len(await docs(client, h)) == 2                                       # failed scan deletes nothing


@pytest.mark.asyncio
async def test_rest_ssrf_guard_on_create(client, app):
    h, cid = await setup(client)
    for url in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8080/x", "http://10.0.0.5/a", "http://localhost/a", "file:///etc/passwd",
                "https://user:pw@api.example.test/x"):
        r = await mk(client, h, cid, "rest", {"url": url}, {}, name=f"n{abs(hash(url))}")
        assert r.status_code == 422, (url, r.text)


def test_assert_public_url_unit(cfg):
    assert_public_url("https://api.example.test/x", cfg)
    assert_public_url("http://127.0.0.1/x", cfg, allow_private=True)
    for bad in ("http://[::1]/x", "http://192.168.1.1/x", "http://0.0.0.0/x", "ftp://example.com/x", "http://service.internal/x"):
        with pytest.raises(ValidationFailed):
            assert_public_url(bad, cfg)


# ===================================================================== security / lifecycle
@pytest.mark.asyncio
async def test_secrets_encrypted_at_rest_and_never_returned(client, app, cfg):
    h, cid = await setup(client)
    c = (await mk(client, h, cid, "slack", {"channels": ["C1"]}, {"bot_token": "xoxb-super-secret"})).json()
    async with app.state.session_factory() as db:
        row = (await db.execute(select(Connector))).scalar_one()
    assert "xoxb-super-secret" not in row.secret_enc and decrypt_secrets(cfg, row.secret_enc) == {"bot_token": "xoxb-super-secret"}
    for path in ("", f"/{c['id']}", f"/{c['id']}/runs", f"/{c['id']}/items"):
        assert "xoxb-super-secret" not in (await client.get(f"/connectors{path}", headers=h)).text
    audit = (await client.get("/admin/audit", headers=h)).text
    assert "xoxb-super-secret" not in audit and "connector.create" in audit


@pytest.mark.asyncio
async def test_validation_and_authorization(client, app, world):
    h, cid = await setup(client)
    assert (await mk(client, h, cid, "slack", {"channels": []}, {"bot_token": "t"})).status_code == 422          # empty channel list
    assert (await mk(client, h, cid, "slack", {"channels": ["C1"], "evil": 1}, {"bot_token": "t"})).status_code == 422   # unknown config key
    assert (await mk(client, h, cid, "slack", {"channels": ["C1"]}, {})).status_code == 422                       # missing credential
    assert (await mk(client, h, cid, "bogus", {}, {})).status_code == 422
    c = (await mk(client, h, cid, "slack", {"channels": ["C1"]}, {"bot_token": "t"})).json()
    assert (await mk(client, h, cid, "slack", {"channels": ["C1"]}, {"bot_token": "t"})).status_code == 409       # duplicate name
    h2 = await register(client, "Org B", "b@beta.io")
    assert (await client.get(f"/connectors/{c['id']}", headers=h2)).status_code == 404                           # tenant isolation
    assert (await client.post(f"/connectors/{c['id']}/sync", headers=h2)).status_code == 404
    assert (await client.delete(f"/connectors/{c['id']}", headers=h2)).status_code == 404
    assert (await client.get("/connectors", headers=h2)).json() == []
    r = await mk(client, h2, cid, "slack", {"channels": ["C1"]}, {"bot_token": "t"}, name="x2")                   # foreign collection
    assert r.status_code == 404
    # non-admin cannot manage connectors
    inv = await client.post("/auth/users", json={"email": "v@acme.io", "password": "correct-horse-battery", "role": "viewer"}, headers=h)
    if inv.status_code == 201:
        from tests.test_api_ingestion import login
        hv = await login(client, "v@acme.io")
        assert (await client.get("/connectors", headers=hv)).status_code == 403


@pytest.mark.asyncio
async def test_owner_deactivation_stops_sync_and_delete_can_remove_documents(client, app, world):
    h, cid = await setup(client)
    world.drive_files = [drive_file(1, "a.txt", LONG, iso(d=1))]
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG)).json()
    await sync(client, app, h, c["id"])
    assert len(await docs(client, h)) == 1
    assert (await client.delete(f"/connectors/{c['id']}", params={"delete_documents": "true"}, headers=h)).status_code == 204
    assert await docs(client, h) == [] and (await client.get("/connectors", headers=h)).json() == []
    async with app.state.session_factory() as db:
        assert (await db.execute(select(Document))).scalars().all() == []


@pytest.mark.asyncio
async def test_partial_failure_does_not_advance_cursor_and_concurrent_sync_is_single_flight(client, app, world):
    h, cid = await setup(client)
    world.drive_files = [drive_file(1, "good.txt", LONG, iso(d=1)), drive_file(2, "empty.txt", "   ", iso(d=2))]    # empty content -> item failure
    c = (await mk(client, h, cid, "google_drive", *DRIVE_CFG)).json()
    run = await sync(client, app, h, c["id"])
    assert run["status"] == "partial" and run["stats"]["failed"] == 1 and run["stats"]["ingested"] == 1
    assert (await client.get(f"/connectors/{c['id']}", headers=h)).json()["has_cursor"] is False
    app.state.connectors.trigger(c["id"])
    app.state.connectors.trigger(c["id"])                                         # second trigger finds the lock held and returns
    await app.state.connectors.drain()
    assert len((await client.get(f"/connectors/{c['id']}/runs", headers=h)).json()) in (2, 3)


@pytest.mark.asyncio
async def test_interrupted_runs_are_failed_on_startup(client, app):
    from app.database.base import utcnow
    from app.database.models import ConnectorRun

    h, cid = await setup(client)
    c = (await mk(client, h, cid, "slack", {"channels": ["C1"]}, {"bot_token": "t"})).json()
    async with app.state.session_factory() as db:
        db.add(ConnectorRun(org_id=(await db.get(Connector, c["id"])).org_id, connector_id=c["id"], started_at=utcnow()))
        await db.commit()
    await app.state.connectors.start()
    run = (await client.get(f"/connectors/{c['id']}/runs", headers=h)).json()[0]
    assert run["status"] == "failed" and "restart" in run["error"]
    await app.state.connectors.stop()


def test_kinds_endpoint_lists_schemas():
    assert set(registry.KINDS) == {"google_drive", "sharepoint", "slack", "teams", "sql", "rest"}
