"""Phase 17: feedback intelligence, quality monitoring, retrieval regression evaluation, prompt A/B experiments."""
from __future__ import annotations

import math
import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.context import set_prompt_pins, set_usage_context
from app.database.base import utcnow
from app.database.models import AgentRun, AuditLog, Experiment, Feedback, User
from app.learning import quality
from app.learning.experiments import pick_variant, pins_of
from app.learning.stats import two_proportion_z, welch_z, wilson
from tests.helpers import login, make_collection, register, upload
from tests.test_workflow import CORPUS, QA, chat, running, seeded


# ===================================================================== statistics
def test_wilson_and_two_proportion_known_values():
    lo, hi = wilson(50, 100)
    assert (lo, hi) == pytest.approx((0.4038, 0.5962), abs=1e-3)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)
    z, p = two_proportion_z(40, 100, 60, 100)
    assert z == pytest.approx(-2.828, abs=1e-3) and p == pytest.approx(0.00468, abs=1e-4)
    assert two_proportion_z(0, 50, 0, 50) == (0.0, 1.0) and two_proportion_z(1, 0, 1, 5) == (0.0, 1.0)
    z, p = welch_z([1, 2, 3, 4, 5] * 10, [3, 4, 5, 6, 7] * 10)
    assert z < -5 and p < 1e-6 and welch_z([1], [2]) == (0.0, 1.0)
    assert welch_z([1.0] * 5, [1.0] * 5) == (0.0, 1.0) and welch_z([0.2] * 5, [0.8] * 5)[1] == 0.0


def test_variant_assignment_is_deterministic_weighted_and_independent_per_experiment():
    vs = [{"name": "A", "prompt_version": "builtin", "weight": 1}, {"name": "B", "prompt_version": 1, "weight": 3}]
    assert [pick_variant("e1", f"run{i}", vs)["name"] for i in range(50)] == [pick_variant("e1", f"run{i}", vs)["name"] for i in range(50)]
    share_b = sum(pick_variant("e1", f"r{i}", vs)["name"] == "B" for i in range(8000)) / 8000
    assert share_b == pytest.approx(0.75, abs=0.03)
    assert sum(pick_variant("e1", f"r{i}", vs)["name"] != pick_variant("e2", f"r{i}", vs)["name"] for i in range(2000)) > 100    # not correlated across experiments
    assert pick_variant("e", "x", [{"name": "only", "prompt_version": 1, "weight": 1}])["name"] == "only"
    assert pins_of({"e1": {"task": "answer", "version": 2}}) == {"answer": "2"} and pins_of({}) == {}


# ===================================================================== helpers
async def org_of(app, email):
    async with app.state.session_factory() as db:
        return (await db.execute(select(User.org_id).where(User.email == email))).scalar_one()


async def user_of(app, email):
    async with app.state.session_factory() as db:
        return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


def run_row(org, user, *, status="completed", answer_status="grounded", conf=0.8, unsupported=0, age_days=1.0, variants=None, review=None):
    return AgentRun(org_id=org, user_id=user, query="q", status=status, review=review, variants=variants or {},
                    result={"answer_status": answer_status, "confidence": {"score": conf}, "verification": {"unsupported": unsupported}} if status != "failed" else None,
                    created_at=utcnow() - timedelta(days=age_days))


# ===================================================================== feedback
async def test_feedback_reasons_consent_and_admin_review_privacy(tmp_path):
    async with running(tmp_path) as (app, c):
        ha, _, _ = await seeded(tmp_path, app, c)
        await c.post("/auth/users", json={"email": "u@example.com", "password": "correct-horse-battery", "role": "editor"}, headers=ha)
        hu = await login(c, "u@example.com")
        hb = await register(c, "Org B", "b@example.com")
        r1 = (await chat(c, hu, QA)).json()["run_id"]
        r2 = (await chat(c, hu, "What is Longformer?")).json()["run_id"]
        r3 = (await chat(c, ha, QA)).json()["run_id"]
        assert (await c.post("/feedback", json={"run_id": r1, "rating": -1, "reasons": ["bogus"]}, headers=hu)).status_code == 422
        ok = (await c.post("/feedback", json={"run_id": r1, "rating": -1, "reasons": ["wrong_answer", "bad_citation", "wrong_answer"], "comment": "nope", "allow_review": True}, headers=hu)).json()
        assert ok["reasons"] == ["bad_citation", "wrong_answer"] and ok["allow_review"] is True
        await c.post("/feedback", json={"run_id": r2, "rating": -1, "reasons": ["too_long"]}, headers=hu)         # no consent
        await c.post("/feedback", json={"run_id": r3, "rating": 1}, headers=ha)
        lst = (await c.get("/admin/feedback", headers=ha)).json()
        assert [x["run_id"] for x in lst] == [r1] and lst[0]["reasons"] == ["bad_citation", "wrong_answer"] and lst[0]["query"] == QA     # r2 stays private
        assert (await c.get("/admin/feedback?rating=1", headers=ha)).json() == []
        assert (await c.get("/admin/feedback", headers=hb)).json() == []                          # other org: nothing
        assert (await c.get("/admin/feedback", headers=hu)).status_code == 403
        s = (await c.get("/admin/feedback/summary", headers=ha)).json()
        assert s["down"] == 2 and s["up"] == 1 and s["reasons"]["too_long"] == 1 and len(s["down_rate_ci95"]) == 2
        await c.post("/feedback", json={"run_id": r1, "rating": -1, "allow_review": False}, headers=hu)       # consent can be withdrawn
        assert (await c.get("/admin/feedback", headers=ha)).json() == []


# ===================================================================== evaluation cases / regression runs
async def test_eval_cases_runs_compare_and_tenant_isolation(tmp_path):
    async with running(tmp_path) as (app, c):
        ha, cid, ids = await seeded(tmp_path, app, c)
        hb = await register(c, "Org B", "b@example.com")
        other = (await upload(c, app, hb, await make_collection(c, hb), "x.txt", b"Totally unrelated bees and honey text. " * 20))["document"]["id"]
        docs = list(ids.values())
        assert (await c.post("/admin/eval/run", json={}, headers=ha)).status_code == 422                                   # no cases yet
        assert (await c.post("/admin/eval/cases", json={"query": "attention", "expected_document_ids": [other]}, headers=ha)).status_code == 404   # other org's doc
        assert (await c.post("/admin/eval/cases", json={"query": "attention", "expected_document_ids": []}, headers=ha)).status_code == 422
        qs = {"Self-attention transformer": docs[0], "Longformer sparse attention": docs[1] if len(docs) > 1 else docs[0], "Linformer low-rank": docs[2] if len(docs) > 2 else docs[0]}
        for q, d in qs.items():
            assert (await c.post("/admin/eval/cases", json={"query": q, "expected_document_ids": [d]}, headers=ha)).status_code == 201
        assert len((await c.get("/admin/eval/cases", headers=ha)).json()) == 3 and (await c.get("/admin/eval/cases", headers=hb)).json() == []
        a = (await c.post("/admin/eval/run", json={"mode": "hybrid", "top_k": 10, "label": "hybrid"}, headers=ha)).json()
        b = (await c.post("/admin/eval/run", json={"mode": "dense", "top_k": 10, "label": "dense"}, headers=ha)).json()
        assert a["metrics"]["cases"] == 3 and 0 <= a["metrics"]["mrr"] <= 1 and "noisy" in a["warning"]
        cmp_ = (await c.get(f"/admin/eval/compare?a={a['id']}&b={b['id']}", headers=ha)).json()
        assert cmp_["shared_cases"] == 3 and set(cmp_["metrics"]) == {"mrr", "ndcg_at_10", "recall_at_10", "precision_at_5"} and "anecdotes" in cmp_["warning"]
        m = cmp_["metrics"]["mrr"]
        assert m["ci_low"] <= m["mean_diff"] <= m["ci_high"]
        assert (await c.get(f"/admin/eval/compare?a={a['id']}&b={b['id']}", headers=hb)).status_code == 404                # runs are org-private
        assert len((await c.get("/admin/eval/runs", headers=ha)).json()) == 2 and (await c.get("/admin/eval/runs", headers=hb)).json() == []
        case_id = (await c.get("/admin/eval/cases", headers=ha)).json()[0]["id"]
        assert (await c.delete(f"/admin/eval/cases/{case_id}", headers=hb)).status_code == 404
        assert (await c.delete(f"/admin/eval/cases/{case_id}", headers=ha)).status_code == 200


async def test_case_from_feedback_needs_consent_and_uses_the_run_query(tmp_path):
    async with running(tmp_path) as (app, c):
        ha, cid, ids = await seeded(tmp_path, app, c)
        await c.post("/auth/users", json={"email": "u@example.com", "password": "correct-horse-battery", "role": "editor"}, headers=ha)
        hu = await login(c, "u@example.com")
        r1 = (await chat(c, hu, QA)).json()["run_id"]
        doc = next(iter(ids.values()))
        body = {"run_id": r1, "expected_document_ids": [doc]}
        assert (await c.post("/admin/eval/cases/from-feedback", json=body, headers=ha)).status_code == 404       # no feedback / no consent
        await c.post("/feedback", json={"run_id": r1, "rating": -1}, headers=hu)
        assert (await c.post("/admin/eval/cases/from-feedback", json=body, headers=ha)).status_code == 404       # feedback without consent
        await c.post("/feedback", json={"run_id": r1, "rating": -1, "allow_review": True}, headers=hu)
        r = await c.post("/admin/eval/cases/from-feedback", json=body, headers=ha)
        assert r.status_code == 201 and r.json()["query"] == QA and r.json()["source_run_id"] == r1


# ===================================================================== quality monitoring
async def seed_runs(app, org, user, n, **kw):
    async with app.state.session_factory() as db:
        for i in range(n):
            db.add(run_row(org, user, **{**kw, "age_days": kw.get("age_days", 1) + (i % 3) * 0.1}))
        await db.commit()


async def test_quality_alerts_need_enough_data_and_a_real_change(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org, user = await org_of(app, "a@example.com"), await user_of(app, "a@example.com")
        await seed_runs(app, org, user, 40, answer_status="grounded", age_days=20)                 # baseline window: healthy
        await seed_runs(app, org, user, 10, answer_status="insufficient_evidence", age_days=1)      # recent: bad, but only 10 runs
        q = (await c.get("/admin/quality?days=14&baseline_days=14", headers=h)).json()
        assert q["alerts"] == [] and q["recent"]["runs"] == 10 and q["baseline"]["runs"] == 40        # too little data to alert
        await seed_runs(app, org, user, 30, answer_status="insufficient_evidence", conf=0.2, age_days=2)
        q = (await c.get("/admin/quality?days=14&baseline_days=14", headers=h)).json()
        kinds = {a["metric"]: a for a in q["alerts"]}
        assert kinds["insufficient_evidence_rate"]["direction"] == "regression" and kinds["insufficient_evidence_rate"]["recent"] > 0.9 and kinds["insufficient_evidence_rate"]["baseline"] == 0.0
        assert kinds["mean_confidence"]["direction"] == "regression" and kinds["mean_confidence"]["delta"] < -0.3
        assert q["daily"] and all("runs" in d for d in q["daily"]) and q["notes"]


async def test_no_alert_for_noise_and_improvements_are_labelled(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org, user = await org_of(app, "a@example.com"), await user_of(app, "a@example.com")
        rnd = random.Random(1)
        async with app.state.session_factory() as db:
            for age in (20, 2):                                                                    # same 20% abstain rate in both windows
                for i in range(100):
                    db.add(run_row(org, user, answer_status="insufficient_evidence" if rnd.random() < 0.2 else "grounded", age_days=age + (i % 4) * 0.1, conf=0.7 + rnd.random() * 0.01))
            await db.commit()
        assert (await c.get("/admin/quality", headers=h)).json()["alerts"] == []
        await seed_runs(app, org, user, 120, status="failed", age_days=20)                           # baseline had many failures; recent has none -> improvement
        al = (await c.get("/admin/quality", headers=h)).json()["alerts"]
        assert any(a["metric"] == "failed_rate" and a["direction"] == "improvement" for a in al)


async def test_background_monitor_audits_new_regressions_once_and_sets_gauge(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org, user = await org_of(app, "a@example.com"), await user_of(app, "a@example.com")
        await seed_runs(app, org, user, 50, answer_status="grounded", age_days=20)
        await seed_runs(app, org, user, 50, answer_status="insufficient_evidence", conf=0.2, age_days=2)
        mon = app.state.quality_monitor
        first = await mon.run_once()
        assert set(first[org]) >= {"insufficient_evidence_rate", "mean_confidence"}
        assert await mon.run_once() == {}                                                            # same alert is not re-raised within 24 h
        alerts = (await c.get("/admin/audit?action=quality.alert", headers=h)).json()
        assert {a["resource"] for a in alerts} == set(first[org]) and "p_value" in alerts[0]["detail"]
        assert "nexus_quality_regressions_active 2" in (await c.get("/admin/metrics", headers=h)).text


# ===================================================================== experiments
async def make_prompt(c, h, text, activate=False):
    r = await c.post("/admin/llm/prompts", json={"task": "answer", "text": text, "activate": activate}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["version"]


async def test_experiment_creation_rules_and_isolation(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        hb = await register(c, "Org B", "b@example.com")
        v1 = await make_prompt(c, h, "TASK: answer\nAnswer concisely using only the passages.")
        good = {"name": "concise", "task": "answer", "variants": [{"name": "control", "prompt_version": "builtin"}, {"name": "concise", "prompt_version": v1, "weight": 2}]}
        for bad, code in ((dict(good, task="ghost"), 422), (dict(good, variants=good["variants"][:1]), 422),
                          (dict(good, variants=[good["variants"][0], dict(good["variants"][1], prompt_version=99)]), 422),
                          (dict(good, variants=[good["variants"][0], dict(good["variants"][0], name="x")]), 422),
                          (dict(good, variants=[good["variants"][0], dict(good["variants"][1], name="control")]), 422),
                          (dict(good, variants=[good["variants"][0], dict(good["variants"][1], weight=0)]), 422)):
            assert (await c.post("/admin/experiments", json=bad, headers=h)).status_code == code, bad
        r = await c.post("/admin/experiments", json=good, headers=h)
        assert r.status_code == 201 and r.json()["status"] == "running"
        assert (await c.post("/admin/experiments", json=dict(good, name="another"), headers=h)).status_code == 409     # one running experiment per task
        assert (await c.get("/admin/experiments", headers=hb)).json() == []
        eid = r.json()["id"]
        assert (await c.get(f"/admin/experiments/{eid}", headers=hb)).status_code == 404
        assert (await c.post(f"/admin/experiments/{eid}/stop", headers=hb)).status_code == 404
        # org B cannot reference org A's prompt versions
        assert (await c.post("/admin/experiments", json=good, headers=hb)).status_code == 422
        assert (await c.post(f"/admin/experiments/{eid}/stop", headers=h)).json()["status"] == "stopped"
        assert (await c.post(f"/admin/experiments/{eid}/stop", headers=h)).status_code == 409
        assert (await c.post("/admin/experiments", json=dict(good, name="again"), headers=h)).status_code == 201       # stopped one frees the task


async def test_runs_are_assigned_pinned_and_prompts_reach_the_model(tmp_path):
    async with running(tmp_path) as (app, c):
        h, _, _ = await seeded(tmp_path, app, c)
        override = "TASK: answer\nVARIANT-B-PROMPT answer strictly from the passages."
        v = await make_prompt(c, h, override)
        e = (await c.post("/admin/experiments", json={"name": "ab", "task": "answer", "variants": [{"name": "A", "prompt_version": "builtin"}, {"name": "B", "prompt_version": v}]}, headers=h)).json()
        seen = []
        m = app.state.llm.models["default"]
        orig = m.llm.complete

        async def spy(system, user, **kw):
            if system.startswith("TASK: answer"):
                seen.append(system)
            return await orig(system, user, **kw)
        m.llm.complete = spy
        run_ids = [(await chat(c, h, QA)).json()["run_id"] for _ in range(14)]
        async with app.state.session_factory() as db:
            runs = [await db.get(AgentRun, r) for r in run_ids]
        assign = [r.variants[e["id"]]["variant"] for r in runs]
        assert set(assign) == {"A", "B"} and all(r.variants[e["id"]]["task"] == "answer" for r in runs)
        assert sum("VARIANT-B-PROMPT" in s for s in seen) == assign.count("B") > 0          # exactly the B-assigned runs used the override
        assert all(r.variants[e["id"]]["version"] == ("builtin" if r.variants[e["id"]]["variant"] == "A" else str(v)) for r in runs)
        await app.state.llm.recorder.flush()
        u = (await c.get("/admin/llm/usage", headers=h)).json()
        assert any(x["key"] == str(v) for x in u["by_prompt_version"])                          # metering records which version served the call
        # a stopped experiment no longer assigns
        await c.post(f"/admin/experiments/{e['id']}/stop", headers=h)
        before = len(seen)
        rid = (await chat(c, h, QA)).json()["run_id"]
        async with app.state.session_factory() as db:
            assert (await db.get(AgentRun, rid)).variants == {} and not any("VARIANT-B" in s for s in seen[before:])


async def seed_experiment_runs(app, org, user, exp, spec):
    """spec: {variant: (n_rated, n_down)}"""
    async with app.state.session_factory() as db:
        for name, (n, down) in spec.items():
            ver = next(v["prompt_version"] for v in exp["variants"] if v["name"] == name)
            for i in range(n):
                r = run_row(org, user, variants={exp["id"]: {"name": exp["name"], "variant": name, "task": exp["task"], "version": str(ver)}}, age_days=0)
                db.add(r)
                await db.flush()
                db.add(Feedback(org_id=org, user_id=f"u{i}-{name}", run_id=r.id, rating=-1 if i < down else 1))
        await db.commit()


async def test_experiment_analysis_verdicts_and_human_conclusion(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org, user = await org_of(app, "a@example.com"), await user_of(app, "a@example.com")
        v = await make_prompt(c, h, "TASK: answer\nBetter prompt. Use only the passages provided.")
        e = (await c.post("/admin/experiments", json={"name": "ab", "task": "answer", "variants": [{"name": "A", "prompt_version": "builtin"}, {"name": "B", "prompt_version": v}]}, headers=h)).json()
        r = (await c.get(f"/admin/experiments/{e['id']}", headers=h)).json()
        assert r["verdict"]["status"] == "insufficient_data" and all(x["runs"] == 0 for x in r["results"]) and len(r["notes"]) == 3
        await seed_experiment_runs(app, org, user, e, {"A": (10, 5), "B": (10, 1)})
        assert (await c.get(f"/admin/experiments/{e['id']}", headers=h)).json()["verdict"]["status"] == "insufficient_data"       # n < 30 per arm: no verdict however big the gap
        await seed_experiment_runs(app, org, user, e, {"A": (60, 30), "B": (60, 6)})
        r = (await c.get(f"/admin/experiments/{e['id']}", headers=h)).json()
        a, b = r["results"]
        assert a["rated"] == 70 and b["rated"] == 70 and a["thumbs_down_rate"] > b["thumbs_down_rate"] and b["thumbs_down_ci95"][1] < a["thumbs_down_ci95"][0]
        assert r["verdict"]["status"] == "difference_detected" and r["comparisons"][0]["thumbs_down"]["p_holm"] < 0.001
        assert r["comparisons"][0]["thumbs_down"]["p_holm"] >= r["comparisons"][0]["thumbs_down"]["p_raw"]                         # Holm never lowers p
        # human conclusion: validated winner, activates its prompt for the org, ends assignment
        assert (await c.post(f"/admin/experiments/{e['id']}/conclude", json={"winner": "nope"}, headers=h)).status_code == 422
        done = (await c.post(f"/admin/experiments/{e['id']}/conclude", json={"winner": "B"}, headers=h)).json()
        assert done["status"] == "concluded" and done["winner"] == "B" and done["active_prompt"] == str(v)
        listing = (await c.get("/admin/llm/prompts", headers=h)).json()
        assert next(x for x in listing if x["task"] == "answer")["active"] == str(v)
        assert (await c.post(f"/admin/experiments/{e['id']}/conclude", json={"winner": "A"}, headers=h)).status_code == 409
        assert {"experiment.create", "experiment.conclude"} <= {x["action"] for x in (await c.get("/admin/audit?limit=100", headers=h)).json()}


async def test_equal_variants_are_not_declared_different(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org, user = await org_of(app, "a@example.com"), await user_of(app, "a@example.com")
        v = await make_prompt(c, h, "TASK: answer\nAnother prompt that is merely different.")
        e = (await c.post("/admin/experiments", json={"name": "ab", "task": "answer", "variants": [{"name": "A", "prompt_version": "builtin"}, {"name": "B", "prompt_version": v}]}, headers=h)).json()
        await seed_experiment_runs(app, org, user, e, {"A": (80, 24), "B": (80, 26)})
        r = (await c.get(f"/admin/experiments/{e['id']}", headers=h)).json()
        assert r["verdict"]["status"] == "no_significant_difference" and r["comparisons"][0]["thumbs_down"]["p_holm"] > 0.5


async def test_prompt_store_pin_unit(tmp_path):
    async with running(tmp_path) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        org = await org_of(app, "a@example.com")
        v = await make_prompt(c, h, "TASK: answer\nPinned version text for the unit test.")
        store = app.state.llmops["prompts"]
        set_usage_context(org, None)
        assert await store.resolve(org, "answer", "TASK: answer\nbuiltin") == ("TASK: answer\nbuiltin", "builtin")
        set_prompt_pins({"answer": str(v)})
        assert await store.resolve(org, "answer", "TASK: answer\nbuiltin") == ("TASK: answer\nPinned version text for the unit test.", str(v))
        set_prompt_pins({"answer": "builtin"})
        assert (await store.resolve(org, "answer", "TASK: answer\nbuiltin"))[1] == "builtin"
        set_prompt_pins({"answer": "999"})                                                          # unknown version: fall back safely, never fail the call
        assert (await store.resolve(org, "answer", "TASK: answer\nbuiltin"))[1] == "builtin"
        set_usage_context(None, None)
