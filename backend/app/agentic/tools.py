"""Typed, permissioned tools. Every call: schema-validated (unknown arguments rejected), role-checked, flag-checked, time-boxed,
output-capped, and executed against the caller's own access scope (a fresh session; the owner is re-validated each call).
Tool OUTPUT IS UNTRUSTED DATA: it is returned to the planner/synthesiser as data, never executed as instructions."""
from __future__ import annotations

import ast
import asyncio
import json
import math
import operator
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.agentic.sandbox import SandboxViolation, run_python
from app.agents.deps import AgentDeps
from app.agents.state import hit_dict
from app.auth.access import accessible_collection_ids
from app.auth.principal import Principal
from app.database.models import Chunk, Document
from app.retrieval.adaptive import AdaptivePolicy
from app.retrieval.types import SearchFilters


@dataclass
class ToolContext:
    deps: AgentDeps
    user_id: str
    org_id: str
    collection_ids: list[str] | None = None
    allow_code: bool = False

    async def principal(self, db) -> Principal:
        return await self.deps.principal({"principal": {"user_id": self.user_id, "org_id": self.org_id}}, db)


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchParams(_P):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=6, ge=1, le=12)
    kinds: list[Literal["text", "table", "figure"]] | None = None


class GraphParams(_P):
    query: str = Field(min_length=2, max_length=500)


class ReadParams(_P):
    document_id: str = Field(min_length=8, max_length=40)
    offset: int = Field(default=0, ge=0, le=10_000)
    limit: int = Field(default=5, ge=1, le=10)


class CalcParams(_P):
    expression: str = Field(min_length=1, max_length=300)


class CodeParams(_P):
    code: str = Field(min_length=1, max_length=8000)


@dataclass
class Tool:
    name: str
    description: str
    params: type[_P]
    run: Callable[[ToolContext, Any], Awaitable[dict]]
    min_role: str = "viewer"
    needs_code_flag: bool = False

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.params.model_json_schema(), "min_role": self.min_role}


@dataclass
class ToolResult:
    ok: bool
    output: dict = field(default_factory=dict)
    error: str | None = None
    ms: float = 0.0
    evidence: list[dict] = field(default_factory=list)      # hit dicts for citation; never shown to the planner


# --------------------------------------------------------------------------------------------- tool bodies
def _hit_view(h: dict) -> dict:
    return {"chunk_id": h["chunk_id"], "document_id": h["document_id"], "title": h["title"], "filename": h["filename"], "page": h.get("page_start"),
            "section": h.get("section"), "kind": h.get("kind", "text"), "score": round(h["score"], 4), "text": h["text"][:600]}


async def _search(ctx: ToolContext, a: SearchParams) -> dict:
    async with ctx.deps.session_factory() as db:
        p = await ctx.principal(db)
        f = SearchFilters(collection_ids=ctx.collection_ids, kinds=a.kinds)
        res = await ctx.deps.pipeline.run_adaptive(db, p, a.query, AdaptivePolicy(), filters=f)
    hits = [hit_dict(h) for h in res.hits[: a.top_k]]
    return {"hits": [_hit_view(h) for h in hits], "confidence": round(res.confidence.score, 3), "plan": res.plan.name, "_evidence": hits}


async def _graph(ctx: ToolContext, a: GraphParams) -> dict:
    async with ctx.deps.session_factory() as db:
        p = await ctx.principal(db)
        g = await ctx.deps.graph.search(db, p, a.query, SearchFilters(collection_ids=ctx.collection_ids))
    names = {k: v.get("name") for k, v in g.nodes.items()}
    return {"entities": [{"id": k, **{x: v.get(x) for x in ("name", "type")}} for k, v in list(g.nodes.items())[:25]],
            "relations": [{"source": names.get(e.get("source")), "predicate": e.get("predicate"), "target": names.get(e.get("target"))} for e in g.edges[:30]],
            "documents": g.documents}


async def _read(ctx: ToolContext, a: ReadParams) -> dict:
    async with ctx.deps.session_factory() as db:
        p = await ctx.principal(db)
        doc = await db.get(Document, a.document_id)
        if doc is None or doc.org_id != p.org_id or doc.collection_id not in await accessible_collection_ids(db, p) or (
                ctx.collection_ids is not None and doc.collection_id not in ctx.collection_ids):
            return {"error": "document not found"}          # same answer for missing and hidden: no existence leak
        rows = (await db.execute(select(Chunk).where(Chunk.document_id == doc.id, Chunk.is_current.is_(True), Chunk.level == "child")
                                 .order_by(Chunk.ordinal).offset(a.offset).limit(a.limit))).scalars().all()
        ev = [{"chunk_id": c.id, "document_id": doc.id, "collection_id": doc.collection_id, "title": doc.title, "filename": doc.filename, "version": c.version,
               "is_current": True, "page_start": c.page_start, "page_end": c.page_end, "section": c.section, "text": c.text, "score": 1.0, "kind": c.kind} for c in rows]
    return {"title": doc.title, "chunks": [_hit_view(e) for e in ev], "_evidence": ev}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
          "sin": math.sin, "cos": math.cos, "floor": math.floor, "ceil": math.ceil}
_CONST = {"pi": math.pi, "e": math.e}


def safe_eval(expr: str) -> float | int:
    def ev(n: ast.AST, depth: int = 0):
        if depth > 30:
            raise ValueError("expression too deep")
        if isinstance(n, ast.Expression):
            return ev(n.body, depth + 1)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            l, r = ev(n.left, depth + 1), ev(n.right, depth + 1)
            if isinstance(n.op, ast.Pow) and (abs(r) > 64 or abs(l) > 1e9):
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](l, r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand, depth + 1))
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(x, depth + 1) for x in n.args])
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x, depth + 1) for x in n.elts]
        raise ValueError("unsupported expression")
    return ev(ast.parse(expr.strip(), mode="eval"))


async def _calc(ctx: ToolContext, a: CalcParams) -> dict:
    try:
        v = safe_eval(a.expression)
    except (ValueError, ZeroDivisionError, OverflowError, SyntaxError, TypeError) as exc:
        return {"error": f"cannot evaluate: {exc}"}
    return {"expression": a.expression, "result": v}


async def _python(ctx: ToolContext, a: CodeParams) -> dict:
    s = ctx.deps.settings
    try:
        r = await run_python(a.code, timeout=s.agentic_sandbox_timeout_seconds, memory_mb=s.agentic_sandbox_memory_mb)
    except SandboxViolation as exc:
        return {"error": f"rejected by sandbox: {exc}"}
    return {"ok": r.ok, "stdout": r.stdout, "error": r.error, "killed": r.killed, "duration_ms": r.duration_ms}


def default_tools() -> dict[str, Tool]:
    ts = [
        Tool("search_documents", "Hybrid search over the user's accessible documents. Returns passages with provenance.", SearchParams, _search),
        Tool("graph_lookup", "Look up entities and relations in the knowledge graph mentioned in the query.", GraphParams, _graph),
        Tool("read_document", "Read consecutive passages of one document by id (use ids from search results).", ReadParams, _read),
        Tool("calculator", "Evaluate an arithmetic expression exactly (+ - * / ** % // and sqrt/log/exp/min/max/sum).", CalcParams, _calc),
        Tool("python", "Run short pure-compute Python (math/statistics/json/re/datetime/decimal...) in a sandbox; print() results.", CodeParams, _python,
             min_role="editor", needs_code_flag=True),
    ]
    return {t.name: t for t in ts}


class ToolRegistry:
    def __init__(self, tools: dict[str, Tool] | None = None, metrics=None) -> None:
        self.tools, self.metrics = tools or default_tools(), metrics

    def visible(self, role: str, settings, allow_code: bool) -> list[Tool]:
        from app.auth.principal import ROLE_RANK

        return [t for t in self.tools.values() if ROLE_RANK.get(role, 0) >= ROLE_RANK[t.min_role] and (not t.needs_code_flag or (
            settings.agentic_code_execution_enabled and allow_code))]

    def validate(self, name: str, args: dict) -> _P:
        t = self.tools.get(name)
        if t is None:
            raise ValueError(f"unknown tool '{name}'")
        try:
            return t.params(**args)
        except ValidationError as exc:
            raise ValueError("invalid arguments: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc

    async def execute(self, ctx: ToolContext, role: str, name: str, args: dict) -> ToolResult:
        t0, s = time.perf_counter(), ctx.deps.settings
        t = self.tools.get(name)
        status, res = "ok", None
        try:
            if t is None:
                raise ValueError(f"unknown tool '{name}'")
            if name not in {x.name for x in self.visible(role, s, ctx.allow_code)}:
                raise PermissionError(f"tool '{name}' is not available for this run")
            params = self.validate(name, args)
            out = await asyncio.wait_for(t.run(ctx, params), s.agentic_tool_timeout_seconds)
            evidence = out.pop("_evidence", [])
            if "error" in out and len(out) == 1:
                res = ToolResult(False, {}, out["error"])
            else:
                text = json.dumps(out, default=str)
                if len(text) > s.agentic_max_tool_output_chars:
                    out = {"truncated": True, "preview": text[: s.agentic_max_tool_output_chars]}
                res = ToolResult(True, out, None, evidence=evidence)
        except asyncio.TimeoutError:
            res, status = ToolResult(False, {}, "tool timed out"), "timeout"
        except PermissionError as exc:
            res, status = ToolResult(False, {}, str(exc)), "denied"
        except ValueError as exc:
            res, status = ToolResult(False, {}, str(exc)), "invalid"
        except Exception as exc:  # noqa: BLE001 - a failing tool degrades the run, it does not crash it
            res, status = ToolResult(False, {}, f"{type(exc).__name__}: {str(exc)[:200]}"), "error"
        if not res.ok and status == "ok":
            status = "error"
        res.ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.metrics is not None:
            self.metrics.inc("nexus_agentic_tool_calls_total", name if t else "unknown", status)
            self.metrics.observe("nexus_agentic_tool_duration_seconds", res.ms / 1000, name if t else "unknown")
        return res
