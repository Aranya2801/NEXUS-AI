from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.auth.principal import Principal
from app.auth.service import audit
from app.retrieval.adaptive import AdaptivePolicy, FixedPolicy, GraphAdaptivePolicy, RetrievalPlan
from app.retrieval.compression import compress_hit
from app.retrieval.pipeline import RetrievalPipeline
from app.retrieval.service import HybridRetriever
from app.retrieval.types import Mode, SearchFilters, Temporal

router = APIRouter(tags=["search"])


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: Mode = "hybrid"
    top_k: int = Field(default=8, ge=1, le=50)
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    doc_types: list[str] | None = Field(default=None, max_length=20)
    departments: list[str] | None = Field(default=None, max_length=20)
    kinds: list[Literal["text", "table", "figure"]] | None = Field(default=None, max_length=3)
    date_from: datetime | None = None
    date_to: datetime | None = None
    temporal: Temporal = "current"
    as_of: datetime | None = None
    include_parent: bool = False


class HitOut(BaseModel):
    chunk_id: str
    document_id: str
    collection_id: str
    title: str
    filename: str
    version: int
    is_current: bool
    page_start: int | None
    page_end: int | None
    section: str | None
    text: str
    parent_text: str | None
    kind: str = "text"
    score: float
    scores: dict[str, float]
    ranks: dict[str, int]
    compressed_text: str | None = None
    evidence_sentences: list[int] = []


class SearchOut(BaseModel):
    mode: Mode
    hits: list[HitOut]
    timings_ms: dict[str, float]
    candidates: dict[str, int]
    collections_searched: int


@router.post("/search", response_model=SearchOut)
async def search(body: SearchIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    retriever: HybridRetriever = request.app.state.retriever
    f = SearchFilters(body.collection_ids, body.doc_types, body.departments, body.date_from, body.date_to, body.temporal, body.as_of, kinds=body.kinds)
    res = await retriever.search(db, p, body.query, mode=body.mode, top_k=body.top_k, filters=f, include_parent=body.include_parent)
    # audit stores a hash, not the query text (queries can themselves be sensitive)
    await audit(db, "search", org_id=p.org_id, user_id=p.user_id, mode=body.mode,
                query_sha=hashlib.sha256(body.query.encode()).hexdigest()[:16], hits=len(res.hits))
    return SearchOut(mode=res.mode, hits=[HitOut(**asdict(h)) for h in res.hits],
                     timings_ms=res.timings_ms, candidates=res.candidates, collections_searched=res.collections_searched)


class AdvancedSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    policy: Literal["adaptive", "fixed"] = "adaptive"
    # used when policy == "fixed" (or to override top_k for adaptive)
    mode: Mode = "hybrid"
    rerank: bool = True
    decompose: bool = False
    multi_query: int = Field(default=0, ge=0, le=5)
    hyde: bool = False
    compress: bool = False
    graph: bool = False          # fixed policy: graph expansion; adaptive policy: force it on at every level
    top_k: int | None = Field(default=None, ge=1, le=50)
    history: list[tuple[Literal["user", "assistant"], str]] = Field(default_factory=list, max_length=12)
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    doc_types: list[str] | None = Field(default=None, max_length=20)
    departments: list[str] | None = Field(default=None, max_length=20)
    temporal: Temporal = "current"
    as_of: datetime | None = None


class AdvancedSearchOut(BaseModel):
    hits: list[HitOut]
    plan: dict
    confidence: dict
    queries: list[str]
    attempts: list[dict]
    trace: list[dict]
    timings_ms: dict[str, float]
    llm_usage: dict
    graph: dict | None = None


@router.post("/search/advanced", response_model=AdvancedSearchOut)
async def search_advanced(body: AdvancedSearchIn, request: Request, p: Principal = Depends(get_principal),
                          db: AsyncSession = Depends(get_db)):
    pipeline: RetrievalPipeline = request.app.state.pipeline
    f = SearchFilters(body.collection_ids, body.doc_types, body.departments, None, None, body.temporal, body.as_of)
    if body.policy == "adaptive":
        policy = GraphAdaptivePolicy() if body.graph else AdaptivePolicy()
    else:
        policy = FixedPolicy(RetrievalPlan(name="fixed", mode=body.mode, rerank=body.rerank, decompose=body.decompose,
                                           multi_query=body.multi_query, hyde=body.hyde, compress=body.compress, graph=body.graph,
                                           top_k=body.top_k or 8, temporal=body.temporal))
    res = await pipeline.run_adaptive(db, p, body.query, policy, filters=f, history=list(body.history))
    if body.top_k:
        res.hits = res.hits[: body.top_k]
    if body.compress and not res.plan.compress:
        for h in res.hits:
            await compress_hit(pipeline.llm, body.query, h, res.usage)
    await audit(db, "search.advanced", org_id=p.org_id, user_id=p.user_id, policy=body.policy, plan=res.plan.name,
                query_sha=hashlib.sha256(body.query.encode()).hexdigest()[:16], hits=len(res.hits), llm_calls=res.usage.calls)
    return AdvancedSearchOut(
        hits=[HitOut(**asdict(h)) for h in res.hits], plan=asdict(res.plan), confidence=asdict(res.confidence), queries=res.queries,
        attempts=res.attempts, trace=res.trace, timings_ms=res.timings_ms,
        llm_usage={"calls": res.usage.calls, "prompt_tokens": res.usage.prompt_tokens,
                   "completion_tokens": res.usage.completion_tokens, "by_task": res.usage.by_task}, graph=res.graph)
