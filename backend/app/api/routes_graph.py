from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal, require_role
from app.api.routes_kb import _visible_document
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import NotFound
from app.retrieval.types import SearchFilters

router = APIRouter(prefix="/graph", tags=["knowledge-graph"])


class GraphQueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    depth: int = Field(default=2, ge=1, le=3)
    collection_ids: list[str] | None = Field(default=None, max_length=100)
    max_chunks: int = Field(default=20, ge=1, le=100)


@router.get("/entities")
async def list_entities(request: Request, q: str | None = Query(default=None, max_length=200), limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0), collection_id: str | None = None,
                        p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    f = SearchFilters(collection_ids=[collection_id] if collection_id else None)
    return await request.app.state.graph_retriever.list_entities(db, p, q, f, limit, offset)


@router.get("/entities/{entity_id}")
async def entity_detail(entity_id: str, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    out = await request.app.state.graph_retriever.entity_detail(db, p, entity_id)
    if out is None:
        raise NotFound("Entity not found")  # also returned for entities visible only through documents the caller cannot read
    return out


@router.post("/query")
async def graph_query(body: GraphQueryIn, request: Request, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    gr = request.app.state.graph_retriever
    res = await gr.search(db, p, body.query, SearchFilters(collection_ids=body.collection_ids), depth=body.depth, max_chunks=body.max_chunks)
    hits = await request.app.state.retriever.hydrate_ranked(db, p, res.ranked, "graph") if res.ranked else []
    return {"seeds": res.seeds, "nodes": res.nodes, "edges": res.edges, "documents": res.documents,
            "hits": [{"chunk_id": h.chunk_id, "document_id": h.document_id, "title": h.title, "page_start": h.page_start,
                      "section": h.section, "text": h.text, "score": h.score} for h in hits]}


@router.get("/stats")
async def graph_stats(request: Request, p: Principal = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    return await request.app.state.graph.stats(db, p.org_id)


@router.post("/reindex/{document_id}")
async def reindex(document_id: str, request: Request, p: Principal = Depends(require_role("editor")), db: AsyncSession = Depends(get_db)):
    """Re-extract the graph for a document's current version (recovery after an extractor failure or a model change)."""
    doc = await _visible_document(db, p, document_id)
    stats = await request.app.state.graph.reindex_document(db, doc)
    doc.meta = {**(doc.meta or {}), "graph": {"status": "ok", **stats}}
    await audit(db, "graph.reindex", org_id=p.org_id, user_id=p.user_id, resource=document_id, **stats)
    return stats
