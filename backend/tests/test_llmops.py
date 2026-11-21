"""Phase 14: routing, fallback, circuit breaker, cache, budgets, prompt versioning, metering endpoints."""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.core.context import set_usage_context
from app.core.exceptions import BudgetExceeded, ConfigurationError, LLMUnavailable, ValidationFailed
from app.database.models import LLMCall
from app.llm.base import LLMResponse
from app.llm.mock import MockLLM
from app.llmops.breaker import CircuitBreaker
from app.llmops.cache import MemoryCache, RedisCache
from app.llmops.config import ModelCfg, RoutingConfig, load_routing_config
from app.llmops.prompts import builtin_prompts, task_of
from app.llmops.router import CircuitBreaker as _CB  # noqa: F401  (re-exported name must exist)
from app.llmops.router import RoutedModel, RoutingLLM
from app.main import create_app
from tests.test_api_ingestion import login, register
from tests.test_phase10 import redis_url  # noqa: F401  (fixture)
from tests.test_workflow import QA, chat, running, seeded

SYS = "TASK: rewrite\nYou rewrite queries."


class Fake:
    def __init__(self, name, fail=0, text="ok", pt=1000, ct=500):
        self.name, self.fail, self.text, self.pt, self.ct, self.calls, self.last_system = name, fail, text, pt, ct, 0, None

    async def complete(self, system, user, *, max_tokens=800, temperature=0.0):
        self.calls += 1
        self.last_system = system
        if self.fail:
            self.fail -= 1 if self.fail > 0 else 0
            raise RuntimeError("provider down")
        return LLMResponse(self.text, self.name, self.pt, self.ct, 5.0)


class Rec:
    def __init__(self):
        self.rows, self.spent = [], {}

    def record(self, **row):
        self.rows.append(row)
        if row.get("org_id"):
            self.spent[row["org_id"]] = self.spent.get(row["org_id"], 0.0) + row["cost_usd"]

    async def spent_today(self, org):
        return self.spent.get(org, 0.0)


def cfg_two(**over):
    base = dict(models={"big": ModelCfg(provider="mock", model="big", price_in_per_mtok=10, price_out_per_mtok=30),
                        "small": ModelCfg(provider="mock", model="small", price_in_per_mtok=1, price_out_per_mtok=2)},
                default_chain=["big", "small"], task_chains={"verify": ["small"]})
    base.update(over)
    return RoutingConfig(**base)


def router(cfg, fakes, rec=None, cache=None, clock=None, prompts=None):
    rec = rec or Rec()
    cb = (lambda: CircuitBreaker(cfg.breaker_threshold, cfg.breaker_window_seconds, cfg.breaker_cooldown_seconds, **({"clock": clock} if clock else {})))
    models = {n: RoutedModel(n, fakes[n], m.price_in_per_mtok, m.price_out_per_mtok, m.timeout_seconds, cb()) for n, m in cfg.models.items()}
    return RoutingLLM(models, cfg, rec, prompts, cache), rec


@pytest.fixture(autouse=True)
def _ctx():
    set_usage_context("org-1", "u-1")
    yield
    set_usage_context(None, None)


# ===================================================================== routing + fallback
async def test_task_chain_routes_and_cost_is_computed():
    f = {"big": Fake("big"), "small": Fake("small")}
    r, rec = router(cfg_two(), f)
    await r.complete("TASK: verify\nx", "u")
    await r.complete(SYS, "u")
    assert f["small"].calls == 1 and f["big"].calls == 1
    big = [x for x in rec.rows if x["model"] == "big"][0]
    assert big["cost_usd"] == pytest.approx((1000 * 10 + 500 * 30) / 1e6) and big["org_id"] == "org-1" and big["task"] == "rewrite"


async def test_falls_back_on_provider_error_and_records_both():
    f = {"big": Fake("big", fail=-1), "small": Fake("small")}
    r, rec = router(cfg_two(), f)
    out = await r.complete(SYS, "u")
    assert out.text == "ok" and [x["status"] for x in rec.rows] == ["error", "ok"]
    assert rec.rows[1]["model"] == "small" and rec.rows[1]["fallback_from"] == "big"


async def test_all_models_failing_raises_llm_unavailable():
    f = {"big": Fake("big", fail=-1), "small": Fake("small", fail=-1)}
    r, _ = router(cfg_two(), f)
    with pytest.raises(LLMUnavailable):
        await r.complete(SYS, "u")


async def test_breaker_opens_skips_model_then_half_open_recovers():
    t = [0.0]
    c = cfg_two(breaker_threshold=2, breaker_cooldown_seconds=30)
    f = {"big": Fake("big", fail=2), "small": Fake("small")}
    r, _ = router(c, f, clock=lambda: t[0])
    await r.complete(SYS, "a")
    await r.complete(SYS, "b")                       # 2 failures -> open
    assert r.models["big"].breaker.state == "open"
    calls = f["big"].calls
    await r.complete(SYS, "c")
    assert f["big"].calls == calls                   # skipped while open
    t[0] = 31
    assert r.models["big"].breaker.state == "half_open"
    await r.complete(SYS, "d")                       # probe succeeds (fail count exhausted)
    assert r.models["big"].breaker.state == "closed"


def test_breaker_failed_probe_reopens():
    t = [0.0]
    b = CircuitBreaker(1, 60, 10, clock=lambda: t[0])
    b.failure()
    t[0] = 11
    assert b.allow() and not b.allow()               # exactly one probe
    b.failure()
    assert b.state == "open"


# ===================================================================== budget
async def test_budget_degrades_then_blocks_per_org():
    c = cfg_two(budget={"daily_usd_per_org": 0.05, "degrade_at": 0.5, "degrade_chain": ["small"]})
    f = {"big": Fake("big"), "small": Fake("small")}
    r, rec = router(c, f)
    await r.complete(SYS, "1")                                      # big: 0.025 USD -> exactly at 50%
    assert f["big"].calls == 1
    await r.complete(SYS, "2")                                      # degraded -> small
    assert f["big"].calls == 1 and f["small"].calls == 1
    rec.spent["org-1"] = 0.06
    with pytest.raises(BudgetExceeded) as e:
        await r.complete(SYS, "3")
    assert e.value.status_code == 429 and e.value.code == "budget_exceeded"
    set_usage_context("org-2", None)                                # another tenant is unaffected
    await r.complete(SYS, "4")


# ===================================================================== cache
async def test_cache_hit_is_tenant_scoped_and_temperature_zero_only():
    f = {"big": Fake("big"), "small": Fake("small")}
    r, rec = router(cfg_two(), f, cache=MemoryCache())
    await r.complete(SYS, "same")
    await r.complete(SYS, "same")
    assert f["big"].calls == 1 and rec.rows[-1]["status"] == "cache_hit" and rec.rows[-1]["cost_usd"] == 0
    set_usage_context("org-2", None)
    await r.complete(SYS, "same")
    assert f["big"].calls == 2                                      # other tenant never sees org-1's entry
    await r.complete(SYS, "same", temperature=0.7)
    await r.complete(SYS, "same", temperature=0.7)
    assert f["big"].calls == 4                                      # non-deterministic calls are never cached


async def test_memory_cache_ttl_and_lru():
    t = [0.0]
    c = MemoryCache(2, ttl=10, clock=lambda: t[0])
    await c.set("a", {"v": 1})
    await c.set("b", {"v": 2})
    await c.get("a")
    await c.set("c", {"v": 3})                                      # evicts b (least recently used)
    assert await c.get("b") is None and (await c.get("a")) == {"v": 1}
    t[0] = 11
    assert await c.get("a") is None
    s = c.stats()
    assert s["evictions"] == 1 and s["hits"] == 2 and s["misses"] == 2


async def test_redis_cache_roundtrip_clear_and_failsoft(redis_url):
    c = RedisCache(redis_url, 60, prefix="t14:")
    await c.set("k", {"text": "x"})
    assert await c.get("k") == {"text": "x"} and await c.get("nope") is None
    assert await c.clear() >= 1 and await c.get("k") is None
    await c.close()
    dead = RedisCache("redis://127.0.0.1:1/0", 60)
    assert await dead.get("k") is None                              # an unreachable cache must never break LLM calls
    await dead.set("k", {"a": 1})
    await dead.close()


# ===================================================================== config
def test_routing_config_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        RoutingConfig(models={"a": ModelCfg(provider="mock", model="a")}, default_chain=["ghost"])
    with pytest.raises(ValueError):
        RoutingConfig(models={"a": ModelCfg(provider="mock", model="a")}, default_chain=[])
    with pytest.raises(ValueError):
        RoutingConfig(models={"a": ModelCfg(provider="mock", model="a")}, default_chain=["a"], task_chains={"x": ["zzz"]})
    s = lambda raw: Settings(env="test", llm_routing_config=raw)  # noqa: E731
    assert load_routing_config(s(None)) is None
    with pytest.raises(ConfigurationError):
        load_routing_config(s("{not json"))
    with pytest.raises(ConfigurationError):
        load_routing_config(s(json.dumps({"models": {"a": {"provider": "openai", "model": "m", "api_key_env": "NEXUS_TEST_MISSING_KEY"}}, "default_chain": ["a"]})))
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"models": {"a": {"provider": "mock", "model": "m"}}, "default_chain": ["a"]}))
    assert load_routing_config(s(f"@{p}")).default_chain == ["a"]
    with pytest.raises(ConfigurationError):
        load_routing_config(s("@/nonexistent/file.json"))
    with pytest.raises(ValueError):
        ModelCfg(provider="mock", model="m", surprise=1)             # unknown keys rejected (typos in price fields must not pass silently)


def test_builtin_prompts_all_have_task_line():
    b = builtin_prompts()
    assert {"rewrite", "answer", "verify", "extract_graph"} <= set(b)
    assert all(task_of(v) == k for k, v in b.items())


# ===================================================================== API: metering, prompts, isolation
def settings(tmp_path, **kw):
    return dict(llm_usage_flush_seconds=0.2, **kw)


ROUTING = {"models": {"big": {"provider": "mock", "model": "mock-llm", "price_in_per_mtok": 5, "price_out_per_mtok": 15},
                      "small": {"provider": "mock", "model": "mock-llm", "price_in_per_mtok": 1, "price_out_per_mtok": 2}},
           "default_chain": ["big", "small"]}


async def test_chat_is_metered_per_org_and_endpoints_are_isolated(tmp_path):
    async with running(tmp_path, llm_routing_config=json.dumps(ROUTING), llm_usage_flush_seconds=0.2) as (app, c):
        ha, _, _ = await seeded(tmp_path, app, c)
        hb = await register(c, "Other", "b@example.com")
        assert (await chat(c, ha, QA)).status_code == 200
        u = (await c.get("/admin/llm/usage", headers=ha)).json()
        assert u["totals"]["calls"] > 0 and {m["key"] for m in u["by_model"]} == {"big"}
        assert any(t["key"] == "answer" for t in u["by_task"])
        async with app.state.session_factory() as db:
            rows = (await db.execute(select(LLMCall))).scalars().all()
        assert rows and all(r.org_id for r in rows if r.task == "answer")
        ub = (await c.get("/admin/llm/usage", headers=hb)).json()
        assert ub["totals"]["calls"] == 0                              # other org sees nothing of org A
        m = await c.get("/admin/llm/models", headers=ha)
        body = m.text.lower()
        assert m.status_code == 200 and "api_key" not in body and "secret" not in body
        assert {x["name"] for x in m.json()["models"]} == {"big", "small"}
        reg = (await c.get("/admin/metrics", headers=ha)).text
        assert "nexus_llm_calls_total" in reg and "nexus_llm_tokens_total" in reg
        for path in ("/admin/llm/usage", "/admin/llm/models", "/admin/llm/prompts"):
            assert (await c.get(path)).status_code == 401
        hv = await _viewer(c, ha)
        assert (await c.get("/admin/llm/usage", headers=hv)).status_code == 403


async def _viewer(c, ha):
    await c.post("/auth/users", json={"email": "v@example.com", "password": "correct-horse-battery", "role": "viewer"}, headers=ha)
    return await login(c, "v@example.com")


async def test_prompt_versioning_validation_activate_rollback_and_isolation(tmp_path):
    async with running(tmp_path, llm_usage_flush_seconds=0.2) as (app, c):
        ha = await register(c, "Org A", "a@example.com")
        hb = await register(c, "Org B", "b@example.com")
        good = "TASK: rewrite\nRewrite the query tersely. Return JSON."
        bad = [{"task": "rewrite", "text": "no task line here, long enough"}, {"task": "rewrite", "text": "TASK: answer\nwrong task line"},
               {"task": "ghost", "text": "TASK: ghost\nunknown task here"}]
        for b in bad:
            assert (await c.post("/admin/llm/prompts", json=b, headers=ha)).status_code in (400, 422)
        r = await c.post("/admin/llm/prompts", json={"task": "rewrite", "text": good, "note": "v1", "activate": True}, headers=ha)
        assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["is_active"]
        r2 = await c.post("/admin/llm/prompts", json={"task": "rewrite", "text": good + " v2"}, headers=ha)
        assert r2.json()["version"] == 2 and not r2.json()["is_active"]

        # the override reaches the model, and the version + hash are metered
        seen = []
        orig = app.state.llm.models["default"].llm.complete

        async def spy(system, user, **kw):
            seen.append(system)
            return await orig(system, user, **kw)
        app.state.llm.models["default"].llm.complete = spy
        set_usage_context(await _org(app, "a@example.com"), None)
        await app.state.llm.complete("TASK: rewrite\nbuiltin text", "hello")
        assert seen[-1] == good
        set_usage_context(await _org(app, "b@example.com"), None)    # another org still gets the built-in prompt
        await app.state.llm.complete("TASK: rewrite\nbuiltin text", "hello")
        assert seen[-1] == "TASK: rewrite\nbuiltin text"
        await app.state.llm.complete("TASK: rewrite\nbuiltin text", "hello again")
        await app.state.llm.recorder.flush() if hasattr(app.state.llm.recorder, "flush") else None
        u = (await c.get("/admin/llm/usage", headers=ha)).json()
        assert any(v["key"] == "1" for v in u["by_prompt_version"])

        listing = (await c.get("/admin/llm/prompts", headers=ha)).json()
        rw = next(x for x in listing if x["task"] == "rewrite")
        assert rw["active"] == "1" and [v["version"] for v in rw["versions"]] == [1, 2]
        assert next(x for x in (await c.get("/admin/llm/prompts", headers=hb)).json() if x["task"] == "rewrite")["versions"] == []   # org isolation
        assert (await c.post("/admin/llm/prompts/rewrite/activate", json={"version": 2}, headers=ha)).json()["active"] == "2"
        assert (await c.post("/admin/llm/prompts/rewrite/activate", json={"version": 9}, headers=ha)).status_code == 404
        assert (await c.post("/admin/llm/prompts/rewrite/activate", json={"version": "builtin"}, headers=ha)).json()["active"] == "builtin"
        set_usage_context(await _org(app, "a@example.com"), None)
        await app.state.llm.complete("TASK: rewrite\nbuiltin text", "after rollback")
        assert seen[-1] == "TASK: rewrite\nbuiltin text"
        audit = (await c.get("/admin/audit", headers=ha)).json()
        assert {"llm.prompt_create", "llm.prompt_activate"} <= {a["action"] for a in audit}


async def _org(app, email):
    from app.database.models import User
    async with app.state.session_factory() as db:
        return (await db.execute(select(User.org_id).where(User.email == email))).scalar_one()


def _mk(p):
    q = p / "x"
    q.mkdir()
    return q


async def test_cache_endpoints_and_cost_free_hits(tmp_path):
    async with running(tmp_path, llm_cache_enabled=True, llm_usage_flush_seconds=0.2) as (app, c):
        ha, _, _ = await seeded(tmp_path, app, c)
        await chat(c, ha, QA)
        await chat(c, ha, QA)
        st = (await c.get("/admin/llm/models", headers=ha)).json()["cache"]
        assert st["hits"] > 0
        u = (await c.get("/admin/llm/usage", headers=ha)).json()
        assert u["status"].get("cache_hit", 0) > 0 and u["cache_hit_rate"] > 0
        assert (await c.delete("/admin/llm/cache", headers=ha)).json()["cleared"] > 0
    async with running(_mk(tmp_path), llm_usage_flush_seconds=0.2) as (app, c):
        h = await register(c, "Org A", "a@example.com")
        assert (await c.delete("/admin/llm/cache", headers=h)).status_code == 404       # disabled -> explicit, not silent


async def test_budget_exhaustion_returns_429_over_http(tmp_path):
    r = dict(ROUTING, budget={"daily_usd_per_org": 0.000001})
    async with running(tmp_path, llm_routing_config=json.dumps(r), llm_usage_flush_seconds=0.2) as (app, c):
        ha, _, _ = await seeded(tmp_path, app, c)
        codes = [(await chat(c, ha, QA)).status_code for _ in range(3)]
        assert 429 in codes
        j = (await chat(c, ha, QA)).json()
        assert j["error"]["code"] == "budget_exceeded"
        u = (await c.get("/admin/llm/usage", headers=ha)).json()
        assert u["budget"]["state"] == "exhausted"


async def test_fallback_through_the_app_when_primary_is_down(tmp_path):
    class Down:
        name = "down"

        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    def builder(m):
        return Down() if m.model == "down" else MockLLM()

    r = {"models": {"bad": {"provider": "mock", "model": "down"}, "good": {"provider": "mock", "model": "mock-llm"}}, "default_chain": ["bad", "good"]}
    from tests.test_workflow import Settings as _S  # noqa: F401
    cfg = Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"), checkpoint_path=str(tmp_path / "c.sqlite"),
                   log_json=False, embedding_dim=64, ingestion_retry_backoff_seconds=0.0, bcrypt_rounds=4, llm_routing_config=json.dumps(r), llm_usage_flush_seconds=0.2)
    app = create_app(cfg)
    app.state.llm_builder = builder
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60) as c:
            ha, _, _ = await seeded(tmp_path, app, c)
            assert (await chat(c, ha, QA)).status_code == 200
            h = {x["name"]: x for x in (await c.get("/admin/llm/models", headers=ha)).json()["models"]}
            assert h["bad"]["failures"] > 0 and h["good"]["calls"] > 0
