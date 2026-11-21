import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database.models import AuditLog, CollectionAccess, MemoryItem, User
from app.memory import service as memsvc
from tests.helpers import PW, login, make_collection, register, upload
from tests.test_workflow import QA, RouterLLM, chat, running, seeded

LONGFORMER_Q = "What does Longformer do with attention?"
LINFORMER_Q = "What does Linformer do with Transformer keys?"


async def mem(c, h, **params):
    r = await c.get("/memory", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def hours_until(iso):
    return (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds() / 3600


# ------------------------------------------------------------------ long-term preferences
async def test_preferences_reach_the_prompt_persist_and_are_validated(tmp_path, monkeypatch):
    async with running(tmp_path) as (app, c):
        h, cid, ids = await seeded(tmp_path, app, c)
        r = await c.put("/memory/preferences/style", json={"value": "concise bullet points"}, headers=h)
        assert r.status_code == 200 and r.json()["kind"] == "long_term" and r.json()["expires_at"] is None
        captured: list[str] = []

        def spy(user):
            captured.append(user)
            return {"claims": []}

        app.state.agent_deps.llm = RouterLLM(app.state.llm, answer=spy)
        await chat(c, h, QA)
        assert any("PREFERENCES:\n- style: concise bullet points" in u for u in captured)
        # a different user of the same org never receives them
        await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=h)
        hv = await login(c, "v@example.com")
        captured.clear()
        await chat(c, hv, QA)
        assert captured and not any("PREFERENCES" in u for u in captured)
        # validation
        assert (await c.put("/memory/preferences/bad key!", json={"value": "x"}, headers=h)).status_code == 422
        assert (await c.put("/memory/preferences/" + "k" * 101, json={"value": "x"}, headers=h)).status_code == 422
        assert (await c.put("/memory/preferences/big", json={"value": "x" * 9000}, headers=h)).status_code == 422
        # per-kind limit (updating an existing key is always allowed)
        monkeypatch.setitem(memsvc.LIMITS, "long_term", 2)
        assert (await c.put("/memory/preferences/k2", json={"value": 1}, headers=h)).status_code == 200
        assert (await c.put("/memory/preferences/k3", json={"value": 1}, headers=h)).status_code == 409
        assert (await c.put("/memory/preferences/style", json={"value": "detailed"}, headers=h)).status_code == 200
    async with running(tmp_path) as (app2, c2):                         # new process
        h2 = await login(c2, "a@example.com")
        assert {i["key"]: i["value"] for i in await mem(c2, h2, kind="long_term")} == {"style": "detailed", "k2": 1}


async def test_memory_is_private_per_user_and_org_even_from_admins(tmp_path):
    async with running(tmp_path) as (app, c):
        ha = await register(c, "OrgA", "a@example.com")
        await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=ha)
        hv, hb = await login(c, "v@example.com"), await register(c, "OrgB", "b@example.com")
        ids = {}
        for name, h in (("a", ha), ("v", hv), ("b", hb)):
            ids[name] = (await c.put("/memory/preferences/style", json={"value": f"{name}-pref"}, headers=h)).json()["id"]
        for name, h in (("a", ha), ("v", hv), ("b", hb)):
            assert [i["value"] for i in await mem(c, h)] == [f"{name}-pref"]       # even the org admin sees only her own memory
        assert (await c.delete(f"/memory/items/{ids['v']}", headers=ha)).status_code == 404     # admin cannot delete a member's item
        assert (await c.delete(f"/memory/items/{ids['a']}", headers=hb)).status_code == 404     # other org cannot either
        assert (await c.delete(f"/memory/items/{ids['v']}", headers=hv)).status_code == 204
        assert (await c.get("/memory")).status_code == 401
        assert (await c.get("/memory", params={"kind": "bogus"}, headers=ha)).status_code == 422


# ------------------------------------------------------------------ session + document history, expiry
async def test_session_and_document_memory_written_only_for_delivered_answers(tmp_path):
    async with running(tmp_path) as (app, c):
        h, cid, ids = await seeded(tmp_path, app, c)
        first = (await chat(c, h, LONGFORMER_Q)).json()
        conv = first["conversation_id"]
        sess = await mem(c, h, kind="session")
        assert len(sess) == 1 and sess[0]["scope_id"] == conv and sess[0]["value"]["last_queries"] == [LONGFORMER_Q]
        assert ids["longformer.txt"] in sess[0]["value"]["cited_documents"] and 23.5 < hours_until(sess[0]["expires_at"]) < 24.5
        await chat(c, h, "and what about Linformer?", conversation_id=conv)
        sess = await mem(c, h, kind="session")
        assert len(sess) == 1 and len(sess[0]["value"]["last_queries"]) == 2
        docs = await mem(c, h, kind="doc_interaction")
        assert docs and all(89 < hours_until(d["expires_at"]) / 24 < 91 for d in docs)                  # 90-day TTL on document history
        # an answer waiting for human review has NOT been delivered, so nothing is remembered until approval
        pending = (await chat(c, h, LINFORMER_Q, review="always")).json()
        assert pending["status"] == "needs_review"
        assert await mem(c, h, kind="session", scope_id=pending["conversation_id"]) == []
        before = {d["key"]: d["value"]["count"] for d in await mem(c, h, kind="doc_interaction")}
        await c.post(f"/agents/runs/{pending['run_id']}/resume", json={"decision": "approve"}, headers=h)
        assert len(await mem(c, h, kind="session", scope_id=pending["conversation_id"])) == 1
        after = {d["key"]: d["value"]["count"] for d in await mem(c, h, kind="doc_interaction")}
        assert after != before and ids["linformer.txt"] in after


async def test_expired_memory_is_hidden_immediately_and_purged(tmp_path):
    async with running(tmp_path, memory_purge_interval_seconds=0.05) as (app, c):
        h = await register(c, "Acme", "a@example.com")
        async with app.state.session_factory() as db:
            u = (await db.execute(select(User))).scalar_one()
            db.add_all([MemoryItem(org_id=u.org_id, user_id=u.id, kind="session", scope_id="c1", key="working_set", value={"x": 1},
                                   expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
                        MemoryItem(org_id=u.org_id, user_id=u.id, kind="long_term", scope_id="", key="keep", value="me", expires_at=None)])
            await db.commit()
        assert [i["key"] for i in await mem(c, h)] == ["keep"]                 # hidden at read time even before the purge runs
        await asyncio.sleep(0.5)                                                # the background purge loop physically deletes it
        async with app.state.session_factory() as db:
            assert {i.key for i in (await db.execute(select(MemoryItem))).scalars()} == {"keep"}
            db.add(MemoryItem(org_id="o", user_id="u", kind="session", scope_id="s", key="old", value={}, expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
            await db.flush()
            assert await app.state.memory.purge_expired(db) == 1


async def test_document_history_tracks_views_respects_access_and_rolls_over(tmp_path, monkeypatch):
    async with running(tmp_path) as (app, c):
        h, cid, ids = await seeded(tmp_path, app, c)
        for name in ("survey.txt", "longformer.txt", "linformer.txt"):
            assert (await c.get(f"/documents/{ids[name]}/chunks", headers=h)).status_code == 200
        recent = (await c.get("/memory/documents/recent", headers=h)).json()
        assert [d["filename"] for d in recent] == ["linformer.txt", "longformer.txt", "survey.txt"]        # most recent first
        assert all(d["action"] == "viewed" and d["count"] == 1 and d["title"] for d in recent)
        await c.get(f"/documents/{ids['survey.txt']}/chunks", headers=h)
        assert (await c.get("/memory/documents/recent", headers=h)).json()[0]["filename"] == "survey.txt"
        assert (await c.get("/memory/documents/recent", headers=h)).json()[0]["count"] == 2
        # deleted documents vanish from history
        await c.delete(f"/documents/{ids['linformer.txt']}", headers=h)
        assert "linformer.txt" not in [d["filename"] for d in (await c.get("/memory/documents/recent", headers=h)).json()]
        # rolling cap: the oldest entry is evicted instead of refusing
        monkeypatch.setitem(memsvc.LIMITS, "doc_interaction", 2)
        await c.get(f"/documents/{ids['performer.txt']}/chunks", headers=h)
        keys = {i["key"] for i in await mem(c, h, kind="doc_interaction")}
        assert len(keys) == 2 and ids["performer.txt"] in keys


async def test_document_history_hides_documents_after_access_is_revoked(tmp_path):
    async with running(tmp_path) as (app, c):
        ha = await register(c, "Acme", "a@example.com")
        col = await make_collection(c, ha, "secret", restricted=True)
        did = (await upload(c, app, ha, col, "s.txt", b"Confidential roadmap for the Zorblax project is described here."))["document"]["id"]
        await c.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
        he = await login(c, "e@example.com")
        uid = (await c.get("/auth/me", headers=he)).json()["id"]
        await c.post(f"/collections/{col}/access", json={"user_id": uid}, headers=ha)
        await c.get(f"/documents/{did}/chunks", headers=he)
        assert [d["filename"] for d in (await c.get("/memory/documents/recent", headers=he)).json()] == ["s.txt"]
        async with app.state.session_factory() as db:
            await db.execute(delete(CollectionAccess).where(CollectionAccess.user_id == uid))
            await db.commit()
        assert (await c.get("/memory/documents/recent", headers=he)).json() == []


# ------------------------------------------------------------------ research contexts
async def test_research_context_scopes_chat_and_revalidates_findings(tmp_path):
    async with running(tmp_path) as (app, c):
        h, cid, ids = await seeded(tmp_path, app, c)
        hb = await register(c, "OrgB", "b@example.com")
        hit = (await c.post("/search", json={"query": "Longformer sparse attention", "mode": "sparse"}, headers=h)).json()["hits"][0]
        assert hit["document_id"] == ids["longformer.txt"]
        ctx = (await c.post("/memory/research-contexts", json={"name": "long-context models", "goal": "compare attention variants"}, headers=h)).json()
        f = await c.post(f"/memory/research-contexts/{ctx['id']}/findings", json={"chunk_id": hit["chunk_id"], "note": "key paper"}, headers=h)
        assert f.status_code == 201 and "Longformer" in f.json()["quote"]
        got = (await c.get(f"/memory/research-contexts/{ctx['id']}", headers=h)).json()
        assert got["pinned_documents"] == [ids["longformer.txt"]] and got["findings"][0]["hidden"] is False and got["findings"][0]["note"] == "key paper"
        assert [x["id"] for x in (await c.get("/memory/research-contexts", headers=h)).json()] == [ctx["id"]]

        # chat inside the context only sees pinned documents
        inside = (await chat(c, h, LONGFORMER_Q, research_context_id=ctx["id"])).json()
        assert inside["answer_status"] == "grounded" and {e["document_id"] for e in inside["evidence"]} == {ids["longformer.txt"]}
        outside = (await chat(c, h, LINFORMER_Q, research_context_id=ctx["id"])).json()
        assert {e["document_id"] for e in outside["evidence"]} <= {ids["longformer.txt"]}          # nothing outside the pinned set is retrieved
        assert all(x["document_id"] == ids["longformer.txt"] for x in outside["citations"]) and "projecting keys" not in outside["answer"]
        assert (await c.post(f"/memory/research-contexts/{ctx['id']}/pin", json={"document_id": ids["linformer.txt"]}, headers=h)).status_code == 204
        both = (await chat(c, h, LINFORMER_Q, research_context_id=ctx["id"])).json()
        assert ids["linformer.txt"] in {x["document_id"] for x in both["citations"]}                    # now Linformer's content is in scope
        empty = (await c.post("/memory/research-contexts", json={"name": "empty"}, headers=h)).json()
        assert (await chat(c, h, LINFORMER_Q, research_context_id=empty["id"])).json()["answer_status"] == "grounded"   # no pins -> no restriction

        # isolation: unknown / foreign ids all look like "not found"
        bctx = (await c.post("/memory/research-contexts", json={"name": "mine"}, headers=hb)).json()
        assert (await c.post(f"/memory/research-contexts/{bctx['id']}/findings", json={"chunk_id": hit["chunk_id"]}, headers=hb)).status_code == 404
        assert (await c.post(f"/memory/research-contexts/{bctx['id']}/pin", json={"document_id": ids["survey.txt"]}, headers=hb)).status_code == 404
        assert (await c.get(f"/memory/research-contexts/{ctx['id']}", headers=hb)).status_code == 404
        assert (await chat(c, hb, QA, research_context_id=ctx["id"])).status_code == 404
        assert (await chat(c, h, QA, research_context_id="nope")).status_code == 404
        # deletion
        assert (await c.delete(f"/memory/research-contexts/{ctx['id']}", headers=h)).status_code == 204
        assert (await c.get(f"/memory/research-contexts/{ctx['id']}", headers=h)).status_code == 404
        assert await mem(c, h, kind="research_context", scope_id=ctx["id"]) == []


async def test_saved_findings_are_hidden_when_access_is_revoked(tmp_path):
    async with running(tmp_path) as (app, c):
        ha = await register(c, "Acme", "a@example.com")
        col = await make_collection(c, ha, "secret", restricted=True)
        await upload(c, app, ha, col, "s.txt", b"Confidential roadmap for the Zorblax project is described here in detail.")
        await c.post("/auth/users", json={"email": "e@example.com", "password": PW, "role": "editor"}, headers=ha)
        he = await login(c, "e@example.com")
        uid = (await c.get("/auth/me", headers=he)).json()["id"]
        await c.post(f"/collections/{col}/access", json={"user_id": uid}, headers=ha)
        hit = (await c.post("/search", json={"query": "Zorblax roadmap", "mode": "sparse"}, headers=he)).json()["hits"][0]
        ctx = (await c.post("/memory/research-contexts", json={"name": "r"}, headers=he)).json()
        assert (await c.post(f"/memory/research-contexts/{ctx['id']}/findings", json={"chunk_id": hit["chunk_id"]}, headers=he)).status_code == 201
        async with app.state.session_factory() as db:
            await db.execute(delete(CollectionAccess).where(CollectionAccess.user_id == uid))
            await db.commit()
        got = (await c.get(f"/memory/research-contexts/{ctx['id']}", headers=he)).json()
        assert got["findings"][0]["hidden"] is True and "quote" not in got["findings"][0] and "Zorblax" not in repr(got["findings"])
        # saving a passage you cannot read is refused with the same answer as a missing one
        assert (await c.post(f"/memory/research-contexts/{ctx['id']}/findings", json={"chunk_id": hit["chunk_id"]}, headers=he)).status_code == 404


# ------------------------------------------------------------------ user-controlled deletion
async def test_users_can_forget_by_kind_or_everything_including_conversations(tmp_path):
    async with running(tmp_path) as (app, c):
        h, cid, ids = await seeded(tmp_path, app, c)
        await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=h)
        hv = await login(c, "v@example.com")
        await c.put("/memory/preferences/style", json={"value": "x"}, headers=h)
        await c.put("/memory/preferences/style", json={"value": "other-user"}, headers=hv)
        j = (await chat(c, h, LONGFORMER_Q)).json()
        n_docs = len(await mem(c, h, kind="doc_interaction"))
        assert n_docs >= 1 and len(await mem(c, h, kind="session")) == 1

        r = (await c.delete("/memory", params={"kind": "doc_interaction"}, headers=h)).json()
        assert r == {"deleted_items": n_docs, "deleted_conversations": 0}
        assert {i["kind"] for i in await mem(c, h)} == {"long_term", "session"}

        graph = app.state.runner_agents.graph
        assert (await graph.aget_state({"configurable": {"thread_id": j["run_id"]}})).values
        r = (await c.delete("/memory", params={"include_conversations": "true"}, headers=h)).json()
        assert r["deleted_items"] == 2 and r["deleted_conversations"] == 1
        assert await mem(c, h) == [] and (await c.get("/conversations", headers=h)).json() == []
        assert (await c.get(f"/agents/runs/{j['run_id']}", headers=h)).status_code == 404
        assert (await graph.aget_state({"configurable": {"thread_id": j["run_id"]}})).values == {}      # checkpoint erased too
        assert [i["value"] for i in await mem(c, hv)] == ["other-user"]                                # other users untouched
        async with app.state.session_factory() as db:
            forget = (await db.execute(select(AuditLog).where(AuditLog.action == "memory.forget"))).scalars().all()
            assert len(forget) == 2 and forget[-1].detail["conversations"] == 1
