"""Agent 3 — Retrieval (+ Agent 4 — Knowledge Graph): parallel scoped retrieval branches, then fusion and reranking."""
from __future__ import annotations

from dataclasses import asdict

from langgraph.config import get_stream_writer

from app.agents.deps import AgentDeps, usage_dict
from app.agents.state import hit_dict, hit_from
from app.llm.base import UsageMeter
from app.retrieval.adaptive import RetrievalPlan
from app.retrieval.types import SearchFilters


def _filters(state: dict) -> SearchFilters:
    return SearchFilters(collection_ids=state.get("collection_ids"), temporal=state["plan"].get("temporal", "current"),
                         document_ids=state.get("document_ids"))


async def retrieve_branch(deps: AgentDeps, state: dict) -> dict:
    """One parallel branch (one sub-query). Opens its own DB session; access scope is rebuilt from the live user record."""
    plan = state["plan"]
    async with deps.session_factory() as db:
        p = await deps.principal(state, db)
        res = await deps.retriever.search(
            db, p, state["sub_query"], mode=plan["mode"], top_k=plan["candidates"], candidates=plan["candidates"],
            filters=_filters(state), dense_text=state.get("hyde_text") if state["branch"] == "q0" and plan["mode"] != "sparse" else None)
    return {"branches": [{"name": state["branch"], "loop": state["loop"], "query": state["sub_query"],
                          "hits": [hit_dict(h) for h in res.hits]}],
            "_trace": {"branch": state["branch"], "hits": len(res.hits), "timings_ms": res.timings_ms}}


async def graph_branch(deps: AgentDeps, state: dict) -> dict:
    async with deps.session_factory() as db:
        p = await deps.principal(state, db)
        gres = await deps.graph.search(db, p, state["standalone"], _filters(state))
        hits = await deps.retriever.hydrate_ranked(db, p, gres.ranked, "graph") if gres.ranked else []
    return {"branches": [{"name": "graph", "loop": state["loop"], "query": state["standalone"], "hits": [hit_dict(h) for h in hits],
                          "graph": {"seeds": gres.seeds, "nodes": gres.nodes, "edges": gres.edges[:100], "documents": gres.documents}}],
            "_trace": {"seeds": len(gres.seeds), "edges": len(gres.edges), "chunks": len(gres.ranked)}}


async def fuse_rerank(deps: AgentDeps, state: dict) -> dict:
    loop = state["loop"]
    cur = [b for b in state.get("branches", []) if b["loop"] == loop]  # earlier loops' branches are history, not evidence
    qb = sorted((b for b in cur if b["name"] != "graph"), key=lambda b: int(b["name"][1:]))
    gb = next((b for b in cur if b["name"] == "graph"), None)
    lists = [[hit_from(h) for h in b["hits"]] for b in qb]
    names = [b["name"] for b in qb]
    if gb and gb["hits"]:
        lists.append([hit_from(h) for h in gb["hits"]])
        names.append("graph")
    meter, trace, timings = UsageMeter(), [], {}
    plan = RetrievalPlan(**state["plan"])
    if not lists:
        hits = []
    elif len(lists) == 1 and not plan.rerank:
        hits = lists[0][: plan.top_k]
    else:
        queries = [b["query"] for b in qb] or [state["standalone"]]
        hits = await deps.pipeline.fuse_and_rerank(lists, names, state["standalone"], queries, plan, trace, timings, meter)
    evidence = [hit_dict(h) for h in hits]
    get_stream_writer()({"type": "evidence", "count": len(evidence), "documents": len({h["document_id"] for h in evidence})})
    return {"evidence": evidence, "graph": gb.get("graph") if gb else state.get("graph"), "llm_usage": usage_dict(meter),
            "_trace": {"loop": loop, "branches": names, "evidence": len(evidence), "steps": [t["step"] for t in trace]}}
