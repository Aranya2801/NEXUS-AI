"""Executes a RetrievalPlan: contextual rewrite → decompose / multi-query / HyDE → retrieval per query →
RRF across queries → rerank → (optional) compression → confidence. `run_adaptive` walks a policy's escalation
ladder with a hard cap, so retrieval can never loop indefinitely."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.llm.base import LLM, UsageMeter
from app.reranking.base import Reranker
from app.retrieval.adaptive import RetrievalPlan
from app.retrieval.compression import compress_hit
from app.retrieval.confidence import Confidence, retrieval_confidence
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.service import HybridRetriever
from app.retrieval.transform import QueryTransformer
from app.retrieval.types import Hit, SearchFilters

MAX_QUERIES = 8


@dataclass(slots=True)
class PipelineResult:
    hits: list[Hit]
    plan: RetrievalPlan
    confidence: Confidence
    queries: list[str]
    trace: list[dict]
    timings_ms: dict[str, float]
    usage: UsageMeter
    attempts: list[dict] = field(default_factory=list)
    graph: dict | None = None


class RetrievalPipeline:
    def __init__(self, retriever: HybridRetriever, llm: LLM, reranker: Reranker, settings: Settings, graph=None) -> None:
        self.retriever, self.llm, self.reranker, self.settings = retriever, llm, reranker, settings
        self.graph = graph  # GraphRetriever | None

    async def run(self, db: AsyncSession, p: Principal, query: str, plan: RetrievalPlan, *,
                  filters: SearchFilters | None = None, history: list[tuple[str, str]] | None = None) -> PipelineResult:
        meter, trace, timings = UsageMeter(), [], {}
        tr = QueryTransformer(self.llm, meter)
        f = filters or SearchFilters()
        if f.temporal == "current" and plan.temporal != "current":
            f = replace(f, temporal=plan.temporal)

        async def step(name: str, coro, **detail):
            t0 = time.perf_counter()
            out = await coro
            ms = (time.perf_counter() - t0) * 1000
            timings[name] = timings.get(name, 0.0) + ms
            trace.append({"step": name, "ms": round(ms, 2), **detail})
            return out

        q0 = await step("rewrite", tr.rewrite(query, history or []), used=bool(history)) if history else query
        queries = [q0]
        if plan.decompose:
            queries = await step("decompose", tr.decompose(q0), original=q0)
        if plan.multi_query:
            extra: list[str] = []
            for base in queries[:2]:
                extra += await step("multi_query", tr.multi_query(base, plan.multi_query), base=base)
            queries = list(dict.fromkeys([*queries, *extra]))[:MAX_QUERIES]
        hyde_text = await step("hyde", tr.hyde(q0)) if plan.hyde else None

        lists: list[list[Hit]] = []
        for i, q in enumerate(queries):
            res = await step("retrieve", self.retriever.search(
                db, p, q, mode=plan.mode, top_k=plan.candidates, filters=f, include_parent=False,
                candidates=plan.candidates, dense_text=hyde_text if (i == 0 and plan.mode != "sparse") else None),
                query=q, mode=plan.mode)
            lists.append(res.hits)

        names = [f"q{i}" for i in range(len(lists))]
        graph_info: dict | None = None
        if plan.graph and self.graph is not None:
            gres = await step("graph", self.graph.search(db, p, q0, f), query=q0)
            graph_info = {"seeds": gres.seeds, "nodes": gres.nodes, "edges": gres.edges[:100], "documents": gres.documents}
            trace.append({"step": "graph_result", "seeds": len(gres.seeds), "edges": len(gres.edges), "chunks": len(gres.ranked)})
            if gres.ranked:
                lists.append(await self.retriever.hydrate_ranked(db, p, gres.ranked, "graph"))
                names.append("graph")

        hits = await self.fuse_and_rerank(lists, names, q0, queries, plan, trace, timings, meter)

        conf = retrieval_confidence(q0, hits, self.settings.rrf_k)
        timings["total"] = sum(timings.values())
        return PipelineResult(hits, plan, conf, queries, trace, {k: round(v, 2) for k, v in timings.items()}, meter, graph=graph_info)

    async def fuse_and_rerank(self, lists: list[list[Hit]], names: list[str], q0: str, queries: list[str], plan: RetrievalPlan,
                              trace: list[dict], timings: dict[str, float], meter: UsageMeter) -> list[Hit]:
        """RRF across per-query/graph rankings → rerank (max over sub-queries) → top_k → optional compression.
        Shared by `run` and the LangGraph retrieval agent."""
        if len(lists) == 1:
            merged = lists[0]
        else:
            by_id = {h.chunk_id: h for hs in lists for h in reversed(hs)}  # earliest query's object wins
            fused = reciprocal_rank_fusion({n: [h.chunk_id for h in hs] for n, hs in zip(names, lists)}, k=self.settings.rrf_k)
            merged = []
            for cid, score, ranks in fused:
                h = by_id[cid]
                h.scores["fused"], h.score, h.ranks = score, score, {**h.ranks, **ranks}
                merged.append(h)
            trace.append({"step": "fuse_queries", "queries": len(lists), "merged": len(merged)})

        if plan.rerank and merged:
            pool = merged[: max(plan.top_k, min(self.settings.rerank_depth, plan.candidates))]
            texts = [h.text for h in pool]
            per_query = [await self.reranker.score(q, texts) for q in ([q0, *queries] if plan.decompose else [q0])]
            t0 = time.perf_counter()
            for i, h in enumerate(pool):  # max over (original + sub-)queries keeps evidence for every sub-question
                h.scores["rerank"] = max(s[i] for s in per_query)
                # graph evidence may not share query words; give it a bounded bonus so it is not reranked away
                h.score = h.scores["rerank"] + 0.25 * min(1.0, h.scores.get("graph", 0.0))
            pool.sort(key=lambda h: (-h.score, h.chunk_id))
            timings["rerank"] = (time.perf_counter() - t0) * 1000
            trace.append({"step": "rerank", "reranker": self.reranker.name, "pool": len(pool)})
            merged = pool
        hits = merged[: plan.top_k]

        if plan.compress:
            for h in hits:
                await compress_hit(self.llm, q0, h, meter)
            trace.append({"step": "compress", "hits": len(hits)})
        return hits

    async def run_adaptive(self, db: AsyncSession, p: Principal, query: str, policy, *, filters: SearchFilters | None = None,
                           history: list[tuple[str, str]] | None = None, max_loops: int | None = None) -> PipelineResult:
        ladder = policy.ladder(query)[: 1 + (self.settings.max_retrieval_loops if max_loops is None else max_loops)]
        best: PipelineResult | None = None
        attempts: list[dict] = []
        total = UsageMeter()
        for plan in ladder:
            res = await self.run(db, p, query, plan, filters=filters, history=history)
            attempts.append({"plan": plan.name, "level": plan.level, "confidence": res.confidence.score,
                             "hits": len(res.hits), "cost_hint": plan.cost_hint()})
            total.calls += res.usage.calls
            total.prompt_tokens += res.usage.prompt_tokens
            total.completion_tokens += res.usage.completion_tokens
            for k, v in res.usage.by_task.items():
                total.by_task[k] = total.by_task.get(k, 0) + v
            if best is None or res.confidence.score > best.confidence.score:
                best = res
            if res.confidence.score >= self.settings.evidence_sufficiency_threshold:
                break
        assert best is not None
        best.attempts, best.usage = attempts, total
        return best
