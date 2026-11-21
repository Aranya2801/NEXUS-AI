"""Graph-assisted retrieval: query → seed entities → bounded BFS over relations → evidence chunks.

Security: every query joins the SAME scope (`build_scope`) used by vector/keyword retrieval. An entity is only
visible if it has a mention in a chunk the caller may read, and an edge is only followed if the chunk that evidences it
is readable. A restricted document therefore contributes neither nodes, edges nor chunks to someone without access."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.database.models import Chunk, Document, DocumentVersion, Entity, EntityAlias, EntityMention, Relation
from app.knowledge_graph.resolution import canonicalize
from app.retrieval.scope import build_scope
from app.retrieval.sparse import STOPWORDS
from app.retrieval.types import SearchFilters

_WORD = re.compile(r"\w+", re.UNICODE)
MAX_GRAM = 4


@dataclass(slots=True)
class GraphResult:
    seeds: list[dict] = field(default_factory=list)
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)
    ranked: list[tuple[str, float]] = field(default_factory=list)   # (chunk_id, score) best-first
    documents: int = 0


def query_grams(query: str) -> dict[str, tuple[int, int]]:
    toks = _WORD.findall(query)
    grams: dict[str, tuple[int, int]] = {}
    for n in range(1, MAX_GRAM + 1):
        for i in range(len(toks) - n + 1):
            window = toks[i : i + n]
            if window[0].lower() in STOPWORDS or window[-1].lower() in STOPWORDS or (n == 1 and len(window[0]) < 3):
                continue
            c = canonicalize(" ".join(window))
            if c:
                grams.setdefault(c, (i, i + n))
    return grams


class GraphRetriever:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _visible(conds):  # mentions restricted to readable chunks
        return (select(EntityMention.entity_id, EntityMention.chunk_id)
                .join(Chunk, Chunk.id == EntityMention.chunk_id)
                .join(DocumentVersion, DocumentVersion.id == Chunk.version_id).where(*conds))

    async def search(self, db: AsyncSession, p: Principal, query: str, filters: SearchFilters | None = None, *,
                     depth: int | None = None, max_chunks: int = 30) -> GraphResult:
        res = GraphResult()
        depth = depth or self.settings.graph_depth
        conds, _ = await build_scope(db, p, filters or SearchFilters())
        grams = query_grams(query)
        if not grams:
            return res
        vis = self._visible(conds)
        rows = (await db.execute(
            select(Entity, EntityAlias.alias_canonical).join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(Entity.org_id == p.org_id, EntityAlias.alias_canonical.in_(list(grams)),
                   Entity.id.in_(select(vis.subquery().c.entity_id))))).all()
        matched: dict[str, tuple[Entity, tuple[int, int]]] = {}
        for ent, alias in rows:
            span = grams[alias]
            if ent.id not in matched or (span[1] - span[0]) > (matched[ent.id][1][1] - matched[ent.id][1][0]):
                matched[ent.id] = (ent, span)
        spans = [sp for _, sp in matched.values()]  # longest match wins: drop entities whose span nests inside another's
        seeds = {eid: e for eid, (e, sp) in matched.items()
                 if not any(o != sp and o[0] <= sp[0] and sp[1] <= o[1] for o in spans)}
        if not seeds:
            return res
        res.seeds = [{"id": e.id, "name": e.display_name, "type": e.type} for e in seeds.values()]
        res.nodes = {e.id: {"id": e.id, "name": e.display_name, "type": e.type, "depth": 0} for e in seeds.values()}

        scores: dict[str, float] = {}
        mention_rows = (await db.execute(vis.where(EntityMention.entity_id.in_(list(seeds))))).all()
        per_chunk: dict[str, set[str]] = {}
        for eid, cid in mention_rows:
            per_chunk.setdefault(cid, set()).add(eid)
        for cid, es in per_chunk.items():
            scores[cid] = 1.0 + 0.25 * (len(es) - 1)

        visited, frontier = set(seeds), set(seeds)
        for d in range(1, depth + 1):
            if not frontier:
                break
            q = (select(Relation, Chunk.page_start, Chunk.section, Document.title)
                 .join(Chunk, Chunk.id == Relation.chunk_id)
                 .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
                 .join(Document, Document.id == Chunk.document_id)
                 .where(Relation.org_id == p.org_id, *conds,
                        or_(Relation.source_id.in_(frontier), Relation.target_id.in_(frontier)))
                 .order_by(Relation.confidence.desc(), Relation.id).limit(self.settings.graph_max_edges_per_hop))
            nxt: set[str] = set()
            for rel, page, section, title in (await db.execute(q)).all():
                res.edges.append({"source_id": rel.source_id, "predicate": rel.predicate, "target_id": rel.target_id,
                                  "confidence": rel.confidence, "depth": d, "chunk_id": rel.chunk_id,
                                  "document_id": rel.document_id, "title": title, "page": page, "section": section,
                                  "evidence": rel.evidence})
                s = (0.5 + 0.5 * rel.confidence) / d
                scores[rel.chunk_id] = max(scores.get(rel.chunk_id, 0.0), s)
                for eid in (rel.source_id, rel.target_id):
                    if eid not in visited:
                        nxt.add(eid)
            visited |= nxt
            frontier = nxt
            for eid in nxt:
                res.nodes[eid] = {"id": eid, "depth": d}
        missing = [i for i, n in res.nodes.items() if "name" not in n]
        if missing:
            for e in (await db.execute(select(Entity).where(Entity.id.in_(missing), Entity.org_id == p.org_id))).scalars():
                res.nodes[e.id].update(name=e.display_name, type=e.type)
        res.ranked = sorted(scores.items(), key=lambda t: (-t[1], t[0]))[:max_chunks]
        if res.ranked:
            ids = [c for c, _ in res.ranked]
            res.documents = len((await db.execute(select(Chunk.document_id).where(Chunk.id.in_(ids)).distinct())).all())
        return res

    # ---------------------------------------------------------------- browsing
    async def list_entities(self, db: AsyncSession, p: Principal, q: str | None, filters: SearchFilters | None,
                            limit: int = 50, offset: int = 0) -> list[dict]:
        conds, _ = await build_scope(db, p, filters or SearchFilters())
        vis = self._visible(conds).subquery()
        from sqlalchemy import func

        stmt = (select(Entity, func.count(vis.c.chunk_id).label("mentions"))
                .join(vis, vis.c.entity_id == Entity.id).where(Entity.org_id == p.org_id)
                .group_by(Entity.id).order_by(func.count(vis.c.chunk_id).desc(), Entity.canonical_name).limit(limit).offset(offset))
        if q:
            stmt = stmt.where(Entity.canonical_name.like(f"%{canonicalize(q)}%"))
        return [{"id": e.id, "name": e.display_name, "type": e.type, "aliases": e.aliases or [], "mentions": m}
                for e, m in (await db.execute(stmt)).all()]

    async def entity_detail(self, db: AsyncSession, p: Principal, entity_id: str, filters: SearchFilters | None = None) -> dict | None:
        conds, _ = await build_scope(db, p, filters or SearchFilters())
        vis = self._visible(conds)
        ent = (await db.execute(select(Entity).where(Entity.id == entity_id, Entity.org_id == p.org_id,
                                                      Entity.id.in_(select(vis.subquery().c.entity_id))))).scalar_one_or_none()
        if ent is None:
            return None
        mentions = (await db.execute(
            select(Chunk.id, Chunk.document_id, Chunk.page_start, Chunk.section, Document.title)
            .join(EntityMention, EntityMention.chunk_id == Chunk.id).join(Document, Document.id == Chunk.document_id)
            .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
            .where(EntityMention.entity_id == ent.id, *conds).limit(50))).all()
        edges = (await db.execute(
            select(Relation, Document.title, Chunk.page_start).join(Chunk, Chunk.id == Relation.chunk_id)
            .join(DocumentVersion, DocumentVersion.id == Chunk.version_id).join(Document, Document.id == Chunk.document_id)
            .where(Relation.org_id == p.org_id, *conds, or_(Relation.source_id == ent.id, Relation.target_id == ent.id))
            .order_by(Relation.confidence.desc()).limit(100))).all()
        other_ids = {x for r, _, _ in edges for x in (r.source_id, r.target_id)} - {ent.id}
        names = {e.id: e.display_name for e in (await db.execute(select(Entity).where(Entity.id.in_(other_ids)))).scalars()} if other_ids else {}
        names[ent.id] = ent.display_name
        return {"id": ent.id, "name": ent.display_name, "type": ent.type, "aliases": ent.aliases or [],
                "mentions": [{"chunk_id": c, "document_id": d, "page": pg, "section": sec, "title": t} for c, d, pg, sec, t in mentions],
                "edges": [{"source": names.get(r.source_id), "predicate": r.predicate, "target": names.get(r.target_id),
                           "confidence": r.confidence, "document_id": r.document_id, "title": t, "page": pg, "evidence": r.evidence}
                          for r, t, pg in edges]}
