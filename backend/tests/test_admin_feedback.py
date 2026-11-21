import httpx
import pytest
import pytest_asyncio

from app.core.config import Settings
from app.main import create_app
from tests.helpers import PW, login, make_collection, register, upload
from tests.test_workflow import QA, chat, running, seeded


async def test_admin_overview_users_audit_jobs_and_isolation(tmp_path):
    async with running(tmp_path) as (app, c):
        ha, cid, ids = await seeded(tmp_path, app, c)
        hb = await register(c, "OrgB", "b@example.com")
        await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=ha)
        hv = await login(c, "v@example.com")
        j = (await chat(c, ha, QA)).json()
        await c.post("/feedback", json={"run_id": j["run_id"], "rating": 1}, headers=ha)

        ov = (await c.get("/admin/overview", headers=ha)).json()
        assert ov["counts"]["users"] == 2 and ov["counts"]["collections"] == 1 and ov["counts"]["chunks"] > 0 and ov["counts"]["conversations"] == 1
        assert ov["documents"] == {"ready": 4} and ov["ingestion_jobs"] == {"succeeded": 4} and ov["agent_runs"] == {"completed": 1}
        assert ov["graph"]["entities"] > 0 and ov["feedback"] == {"up": 1, "down": 0}
        assert ov["runtime"]["llm"] == "mock-llm" and ov["runtime"]["embedder"].startswith("hashing")
        ob = (await c.get("/admin/overview", headers=hb)).json()                      # other org sees only its own numbers
        assert ob["counts"]["users"] == 1 and ob["counts"]["chunks"] == 0 and ob["documents"] == {} and ob["feedback"] == {"up": 0, "down": 0}
        for path in ("/admin/overview", "/admin/users", "/admin/audit", "/admin/jobs"):
            assert (await c.get(path, headers=hv)).status_code == 403                     # members are not admins
            assert (await c.get(path)).status_code == 401
        assert {u["email"] for u in (await c.get("/admin/users", headers=ha)).json()} == {"a@example.com", "v@example.com"}
        audit = (await c.get("/admin/audit", params={"limit": 200}, headers=ha)).json()
        assert {"auth.register", "document.upload", "chat.run", "feedback.give"} <= {a["action"] for a in audit}
        assert all(a["detail"] is not None for a in audit) and not any("b@example.com" in repr(a) for a in audit)
        assert {a["action"] for a in (await c.get("/admin/audit", params={"action": "chat.run"}, headers=ha)).json()} == {"chat.run"}
        jobs = (await c.get("/admin/jobs", headers=ha)).json()
        assert len(jobs) == 4 and {x["status"] for x in jobs} == {"succeeded"} and (await c.get("/admin/jobs", params={"status": "dead"}, headers=ha)).json() == []
        assert (await c.get("/admin/jobs", headers=hb)).json() == []


async def test_admin_can_change_roles_and_deactivate_users_immediately(tmp_path):
    async with running(tmp_path) as (app, c):
        ha = await register(c, "OrgA", "a@example.com")
        hb = await register(c, "OrgB", "b@example.com")
        v = (await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=ha)).json()
        hv = await login(c, "v@example.com")
        me = (await c.get("/auth/me", headers=ha)).json()
        assert (await c.post("/collections", json={"name": "x"}, headers=hv)).status_code == 403
        r = await c.patch(f"/admin/users/{v['id']}", json={"role": "editor"}, headers=ha)
        assert r.status_code == 200 and r.json()["role"] == "editor"
        assert (await c.post("/collections", json={"name": "x"}, headers=hv)).status_code == 201       # old token, new role, effective at once
        assert (await c.patch(f"/admin/users/{v['id']}", json={"is_active": False}, headers=ha)).status_code == 200
        assert (await c.get("/auth/me", headers=hv)).status_code == 401                                   # deactivated: token stops working at once
        assert (await c.post("/auth/login", json={"email": "v@example.com", "password": PW})).status_code == 401
        # guard rails
        assert (await c.patch(f"/admin/users/{me['id']}", json={"role": "viewer"}, headers=ha)).status_code == 409   # last admin
        assert (await c.patch(f"/admin/users/{me['id']}", json={"is_active": False}, headers=ha)).status_code == 409
        assert (await c.patch(f"/admin/users/{v['id']}", json={"role": "admin"}, headers=hb)).status_code == 404    # other org
        assert (await c.patch(f"/admin/users/{v['id']}", json={"role": "boss"}, headers=ha)).status_code == 422
        await c.patch(f"/admin/users/{v['id']}", json={"role": "admin", "is_active": True}, headers=ha)
        assert (await c.patch(f"/admin/users/{me['id']}", json={"role": "viewer"}, headers=ha)).status_code == 200  # now there is another admin
        upd = [a for a in (await c.get("/admin/audit", params={"action": "user.update"}, headers=await login(c, "v@example.com"))).json()]
        assert len(upd) >= 4 and any(a["detail"].get("role") == "editor" for a in upd)


async def test_feedback_is_owner_only_upserted_and_validated(tmp_path):
    async with running(tmp_path) as (app, c):
        ha, cid, ids = await seeded(tmp_path, app, c)
        hb = await register(c, "OrgB", "b@example.com")
        await c.post("/auth/users", json={"email": "v@example.com", "password": PW, "role": "viewer"}, headers=ha)
        hv = await login(c, "v@example.com")
        j = (await chat(c, ha, QA)).json()
        rid = j["run_id"]
        assert (await c.get(f"/feedback/{rid}", headers=ha)).json() == {"run_id": rid, "rating": None, "comment": "", "reasons": [], "allow_review": False}
        assert (await c.post("/feedback", json={"run_id": rid, "rating": -1, "comment": "missed the point"}, headers=ha)).status_code == 200
        assert (await c.post("/feedback", json={"run_id": rid, "rating": 1}, headers=ha)).json()["rating"] == 1            # re-rating updates
        assert (await c.get(f"/feedback/{rid}", headers=ha)).json()["rating"] == 1
        assert (await c.get("/admin/overview", headers=ha)).json()["feedback"] == {"up": 1, "down": 0}
        for h in (hb, hv):                                                                                                    # other org / other user
            assert (await c.post("/feedback", json={"run_id": rid, "rating": 1}, headers=h)).status_code == 404
        assert (await c.get(f"/feedback/{rid}", headers=hv)).json()["rating"] is None
        assert (await c.post("/feedback", json={"run_id": rid, "rating": 5}, headers=ha)).status_code == 422
        assert (await c.post("/feedback", json={"run_id": rid, "rating": 1, "comment": "x" * 1001}, headers=ha)).status_code == 422
        assert (await c.post("/feedback", json={"run_id": "nope", "rating": 1}, headers=ha)).status_code == 404


async def test_cors_is_off_by_default_and_opt_in(tmp_path):
    async with running(tmp_path) as (app, c):
        r = await c.options("/auth/login", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in r.headers
    (tmp_path / "x").mkdir()
    async with running(tmp_path / "x", cors_origins=["http://localhost:5173"]) as (app, c):
        ok = await c.options("/auth/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                                     "Access-Control-Request-Headers": "authorization,content-type"})
        assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
        bad = await c.options("/auth/login", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in bad.headers
