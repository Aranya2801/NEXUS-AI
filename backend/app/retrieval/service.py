from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.database.models import Chunk, Document, DocumentVersion
from app.embeddings.base import Embedder
from app.retrieval.dense import dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.scope import build_scope
from app.retrieval.sparse import sparse_search
from app.retrieval.types import Hit, Mode, RetrievalResult, SearchFilters


class HybridRetriever:
    def __init__(self, embedder: Embedder, settings: Settings) -> None:
        self.embedder, self.settings = embedder, settings

    async def search(self, db: AsyncSession, p: Principal, query: str, *, mode: Mode = "hybrid",
                     top_k: int | None = None, filters: SearchFilters | None = None,
                     include_parent: bool = False, candidates: int | None = None,
                     dense_text: str | None = None) -> RetrievalResult:
        query = query.strip()
        if not query or len(query) > 2000:
            raise ValidationFailed("Query must be 1-2000 characters")
        f = filters or SearchFilters()
        k = top_k or self.settings.retrieval_top_k
        n = candidates or self.settings.retrieval_candidates
        t: dict[str, float] = {}

        t0 = time.perf_counter()
        conds, n_cols = await build_scope(db, p, f)
        t["scope"] = (time.perf_counter() - t0) * 1000
        rankings: dict[str, list[str]] = {}
        raw: dict[str, dict[str, float]] = {}
        counts: dict[str, int] = {}

        if mode in ("dense", "hybrid"):
            t0 = time.perf_counter()
            qvec = (await self.embedder.embed([dense_text or query]))[0]  # dense_text: HyDE passage
            t["embed"] = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            res = await dense_search(db, qvec, self.embedder.name, conds, n)
            t["dense"] = (time.perf_counter() - t0) * 1000
            rankings["dense"], counts["dense"] = [i for i, _ in res], len(res)
            raw["dense"] = dict(res)
        if mode in ("sparse", "hybrid"):
            t0 = time.perf_counter()
            res = await sparse_search(db, query, conds, n)
            t["sparse"] = (time.perf_counter() - t0) * 1000
            rankings["sparse"], counts["sparse"] = [i for i, _ in res], len(res)
            raw["sparse"] = dict(res)

        t0 = time.perf_counter()
        if mode == "hybrid":
            fused = reciprocal_rank_fusion(rankings, k=self.settings.rrf_k)
        else:
            (name, ids), = rankings.items()
            fused = [(i, raw[name][i], {name: r}) for r, i in enumerate(ids, start=1)]
        fused = fused[:k]
        t["fuse"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        hits = await self._hydrate(db, p, fused, raw, include_parent)
        t["hydrate"] = (time.perf_counter() - t0) * 1000
        t["total"] = sum(t.values())
        return RetrievalResult(hits, mode, {k_: round(v, 2) for k_, v in t.items()}, counts, n_cols)

    async def hydrate_ranked(self, db: AsyncSession, p: Principal, ranked: list[tuple[str, float]], name: str,
                             include_parent: bool = False) -> list[Hit]:
        """Turns an externally produced, already-scoped ranking (e.g. graph retrieval) into Hits."""
        fused = [(cid, score, {name: r}) for r, (cid, score) in enumerate(ranked, start=1)]
        return await self._hydrate(db, p, fused, {name: dict(ranked)}, include_parent)

    async def _hydrate(self, db: AsyncSession, p: Principal, fused, raw, include_parent: bool) -> list[Hit]:
        if not fused:
            return []
        ids = [i for i, _, _ in fused]
        # org_id re-asserted here as defence in depth; ids already came from the scoped query
        rows = (await db.execute(
            select(Chunk, Document.title, Document.filename, DocumentVersion.is_current.label("vcur"))
            .join(Document, Document.id == Chunk.document_id)
            .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
            .where(Chunk.id.in_(ids), Chunk.org_id == p.org_id))).all()
        by_id = {r.Chunk.id: r for r in rows}
        parents: dict[str, str] = {}
        if include_parent:
            pids = {r.Chunk.parent_id for r in rows if r.Chunk.parent_id}
            if pids:
                parents = {c.id: c.text for c in (await db.execute(
                    select(Chunk).where(Chunk.id.in_(pids), Chunk.org_id == p.org_id))).scalars()}
        hits = []
        for cid, score, ranks in fused:
            r = by_id.get(cid)
            if r is None:
                continue
            c = r.Chunk
            hits.append(Hit(
                chunk_id=c.id, document_id=c.document_id, collection_id=c.collection_id, title=r.title, filename=r.filename,
                version=c.version, is_current=bool(r.vcur), page_start=c.page_start, page_end=c.page_end, section=c.section,
                text=c.text, kind=c.kind, parent_text=parents.get(c.parent_id) if c.parent_id else None, score=score,
                scores={n: raw[n][cid] for n in raw if cid in raw[n]}, ranks=ranks))
        return hits
