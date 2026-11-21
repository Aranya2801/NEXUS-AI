"""Builds and maintains the persistent knowledge graph.

Update policy: the graph always reflects the CURRENT version of each document. When a new version is indexed (or a
document is deleted/reindexed) all mentions and relations of that document are removed first, then rebuilt from the new
chunks; entities left without any mention are garbage-collected. Older versions stay searchable through the vector /
keyword indexes (temporal retrieval) but do not contribute graph edges."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models import Chunk, Document, Entity, EntityAlias, EntityMention, Relation
from app.knowledge_graph.resolution import EntityResolver, acronym_pairs, canonicalize
from app.knowledge_graph.types import ChunkExtraction

log = get_logger("nexus.graph")
_GENERIC = {"concept", "other"}


class GraphIndexer:
    def __init__(self, extractor, settings: Settings) -> None:
        self.extractor, self.settings = extractor, settings

    async def extract(self, chunks: list[Chunk]) -> dict[str, ChunkExtraction]:
        """Pure computation (no DB writes) so an LLM failure can be handled without touching the transaction."""
        return await self.extractor.extract([(c.id, c.text) for c in chunks])

    async def clear_document(self, db: AsyncSession, org_id: str, document_id: str) -> None:
        ent_ids = set((await db.execute(select(EntityMention.entity_id).where(
            EntityMention.org_id == org_id, EntityMention.document_id == document_id))).scalars())
        await db.execute(delete(Relation).where(Relation.org_id == org_id, Relation.document_id == document_id))
        await db.execute(delete(EntityMention).where(EntityMention.org_id == org_id, EntityMention.document_id == document_id))
        if ent_ids:
            alive = set((await db.execute(select(EntityMention.entity_id).where(EntityMention.entity_id.in_(ent_ids)))).scalars())
            orphans = ent_ids - alive
            if orphans:  # explicit deletes: SQLite does not enforce ON DELETE CASCADE
                await db.execute(delete(Relation).where(Relation.source_id.in_(orphans) | Relation.target_id.in_(orphans)))
                await db.execute(delete(EntityAlias).where(EntityAlias.entity_id.in_(orphans)))
                await db.execute(delete(Entity).where(Entity.id.in_(orphans)))

    async def persist(self, db: AsyncSession, doc: Document, chunks: list[Chunk],
                      extracted: dict[str, ChunkExtraction]) -> dict:
        await self.clear_document(db, doc.org_id, doc.id)
        await db.flush()
        all_text = " ".join(c.text for c in chunks)
        local: dict[str, str] = {}
        surfaces: dict[str, str] = {}
        for expansion, acr in acronym_pairs(all_text):
            local[canonicalize(acr)] = canonicalize(expansion)
            surfaces.setdefault(expansion, "concept")

        def want(surface: str, typ: str) -> None:
            if surface not in surfaces or surfaces[surface] in _GENERIC:
                surfaces[surface] = typ

        for ex in extracted.values():
            for e in ex.entities:
                want(e.surface, e.type)
            for r in ex.relations:
                want(r.source, "concept")
                want(r.target, "concept")
        resolver = EntityResolver(db, doc.org_id, local)
        ents = await resolver.resolve_many(surfaces)
        for surf, ent in ents.items():  # upgrade generic types when a later document is more specific
            if ent.type in _GENERIC and surfaces[surf] not in _GENERIC:
                ent.type = surfaces[surf]

        n_mentions = n_rel = rejected = 0
        for ch in chunks:
            ex = extracted.get(ch.id)
            if ex is None:
                continue
            rejected += ex.rejected
            ids: set[str] = {ents[e.surface].id for e in ex.entities if e.surface in ents}
            seen_rel: set[tuple[str, str, str]] = set()
            for r in ex.relations:
                s, t = ents.get(r.source), ents.get(r.target)
                if not s or not t or s.id == t.id or (s.id, t.id, r.predicate) in seen_rel:
                    continue
                seen_rel.add((s.id, t.id, r.predicate))
                ids |= {s.id, t.id}
                db.add(Relation(org_id=doc.org_id, source_id=s.id, target_id=t.id, predicate=r.predicate, chunk_id=ch.id,
                                document_id=doc.id, confidence=r.confidence, evidence=r.evidence))
                n_rel += 1
            for eid in ids:
                db.add(EntityMention(org_id=doc.org_id, entity_id=eid, chunk_id=ch.id, document_id=doc.id))
                n_mentions += 1
        await db.flush()
        return {"entities": len({e.id for e in ents.values()}), "mentions": n_mentions, "relations": n_rel, "rejected": rejected,
                "extractor": self.extractor.name}

    async def reindex_document(self, db: AsyncSession, doc: Document) -> dict:
        chunks = list((await db.execute(select(Chunk).where(
            Chunk.document_id == doc.id, Chunk.org_id == doc.org_id, Chunk.is_current.is_(True), Chunk.level == "child")
            .order_by(Chunk.ordinal))).scalars())
        extracted = await self.extract(chunks)
        return await self.persist(db, doc, chunks, extracted)

    async def stats(self, db: AsyncSession, org_id: str) -> dict:
        n = lambda m: select(func.count()).select_from(m).where(m.org_id == org_id)  # noqa: E731
        return {"entities": (await db.execute(n(Entity))).scalar_one(), "relations": (await db.execute(n(Relation))).scalar_one(),
                "mentions": (await db.execute(n(EntityMention))).scalar_one()}
