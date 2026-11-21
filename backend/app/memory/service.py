"""User memory. Five kinds, all strictly per (org, user): 
  short_term      = recent Message rows of the conversation (already persisted; used for follow-up rewriting)
  long_term       = explicit user preferences (key/value), no expiry
  session         = per-conversation working set (queries, cited documents, entities), TTL
  research_context= named workspaces with pinned documents and saved findings (page-cited snapshots)
  doc_interaction = recently cited/viewed documents, TTL, capped
Everything is user-readable and user-deletable. Content that points at documents is re-validated against the caller's CURRENT
access on every read, so revoked access hides saved snippets. Do not store secrets: values are plain JSON in the database."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.access import accessible_collection_ids
from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.database.base import new_id, utcnow
from app.database.models import Chunk, Document, MemoryItem

KINDS = ("long_term", "session", "research_context", "doc_interaction")
KEY_RX = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
MAX_VALUE_BYTES = 8000
LIMITS = {"long_term": 200, "session": 50, "research_context": 2000, "doc_interaction": 500}
META_KEY = "_meta"


def item_out(i: MemoryItem) -> dict:
    return {"id": i.id, "kind": i.kind, "scope_id": i.scope_id, "key": i.key, "value": i.value,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None, "updated_at": i.updated_at.isoformat()}


class MemoryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ basics
    @staticmethod
    def _check(key: str, value) -> None:
        if not KEY_RX.match(key):
            raise ValidationFailed("Key must be 1-100 characters of letters, digits, _ . : -")
        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Value must be JSON-serialisable") from exc
        if size > MAX_VALUE_BYTES:
            raise ValidationFailed(f"Value exceeds {MAX_VALUE_BYTES} bytes")

    @staticmethod
    def _live(q):
        return q.where((MemoryItem.expires_at.is_(None)) | (MemoryItem.expires_at > utcnow()))

    async def _upsert(self, db: AsyncSession, p: Principal, kind: str, scope: str, key: str, value, ttl: timedelta | None) -> MemoryItem:
        self._check(key, value)
        item = (await db.execute(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == kind,
            MemoryItem.scope_id == scope, MemoryItem.key == key))).scalar_one_or_none()
        exp = utcnow() + ttl if ttl else None
        if item is None:
            n = len((await db.execute(select(MemoryItem.id).where(MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id,
                                                                  MemoryItem.kind == kind))).all())
            if n >= LIMITS[kind]:
                if kind == "doc_interaction":  # rolling history: drop the oldest instead of refusing
                    oldest = (await db.execute(select(MemoryItem).where(
                        MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == kind)
                        .order_by(MemoryItem.updated_at).limit(max(1, n - LIMITS[kind] + 1)))).scalars().all()
                    for o in oldest:
                        await db.delete(o)
                else:
                    raise Conflict(f"Limit of {LIMITS[kind]} {kind} items reached")
            item = MemoryItem(org_id=p.org_id, user_id=p.user_id, kind=kind, scope_id=scope, key=key, value=value, expires_at=exp)
            db.add(item)
        else:
            item.value, item.expires_at = value, exp
        await db.flush()
        return item

    async def list_items(self, db: AsyncSession, p: Principal, kind: str | None = None, scope_id: str | None = None) -> list[dict]:
        if kind is not None and kind not in KINDS:
            raise ValidationFailed(f"kind must be one of {KINDS}")
        q = self._live(select(MemoryItem).where(MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id))
        if kind:
            q = q.where(MemoryItem.kind == kind)
        if scope_id is not None:
            q = q.where(MemoryItem.scope_id == scope_id)
        return [item_out(i) for i in (await db.execute(q.order_by(MemoryItem.kind, MemoryItem.updated_at.desc()))).scalars()]

    async def delete_item(self, db: AsyncSession, p: Principal, item_id: str) -> None:
        item = await db.get(MemoryItem, item_id)
        if item is None or item.org_id != p.org_id or item.user_id != p.user_id:
            raise NotFound("Memory item not found")
        await db.delete(item)

    async def delete_all(self, db: AsyncSession, p: Principal, kind: str | None = None) -> int:
        q = delete(MemoryItem).where(MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id)
        if kind:
            q = q.where(MemoryItem.kind == kind)
        return (await db.execute(q)).rowcount or 0

    async def purge_expired(self, db: AsyncSession) -> int:
        return (await db.execute(delete(MemoryItem).where(MemoryItem.expires_at.is_not(None), MemoryItem.expires_at <= utcnow()))).rowcount or 0

    # --------------------------------------------------------------- long-term
    async def set_preference(self, db: AsyncSession, p: Principal, key: str, value) -> dict:
        return item_out(await self._upsert(db, p, "long_term", "", key, value, None))

    async def preferences_context(self, db: AsyncSession, p: Principal, limit: int = 10, max_chars: int = 1000) -> list[str]:
        items = (await db.execute(self._live(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "long_term"))
            .order_by(MemoryItem.updated_at.desc()).limit(limit))).scalars().all()
        out, used = [], 0
        for i in items:
            line = f"{i.key}: {str(i.value)[:200]}"
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
        return out

    # ----------------------------------------------------------------- session
    async def record_session(self, db: AsyncSession, p: Principal, conversation_id: str, query: str, cited_documents: list[str],
                             entities: list[str], query_class: str | None) -> None:
        cur = (await db.execute(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "session",
            MemoryItem.scope_id == conversation_id, MemoryItem.key == "working_set"))).scalar_one_or_none()
        prev = (cur.value if cur else {}) or {}
        value = {"last_queries": [*prev.get("last_queries", []), query[:300]][-5:],
                 "cited_documents": list(dict.fromkeys([*cited_documents, *prev.get("cited_documents", [])]))[:20],
                 "entities": list(dict.fromkeys([*entities, *prev.get("entities", [])]))[:20], "query_class": query_class}
        await self._upsert(db, p, "session", conversation_id, "working_set", value, timedelta(hours=self.settings.memory_ttl_session_hours))

    # ------------------------------------------------------- document history
    async def record_document_interactions(self, db: AsyncSession, p: Principal, document_ids: list[str], action: str = "cited") -> None:
        ttl = timedelta(days=self.settings.memory_ttl_doc_interaction_days)
        for did in dict.fromkeys(document_ids):
            cur = (await db.execute(select(MemoryItem).where(
                MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "doc_interaction",
                MemoryItem.scope_id == "", MemoryItem.key == did))).scalar_one_or_none()
            count = ((cur.value or {}).get("count", 0) if cur else 0) + 1
            await self._upsert(db, p, "doc_interaction", "", did, {"action": action, "count": count, "last_at": utcnow().isoformat()}, ttl)

    async def recent_documents(self, db: AsyncSession, p: Principal, limit: int = 20) -> list[dict]:
        items = (await db.execute(self._live(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "doc_interaction"))
            .order_by(MemoryItem.updated_at.desc()).limit(limit * 2))).scalars().all()
        if not items:
            return []
        allowed = await accessible_collection_ids(db, p)
        docs = {d.id: d for d in (await db.execute(select(Document).where(
            Document.id.in_([i.key for i in items]), Document.org_id == p.org_id))).scalars() if d.collection_id in allowed}
        return [{"document_id": i.key, "title": docs[i.key].title, "filename": docs[i.key].filename, **i.value}
                for i in items if i.key in docs][:limit]  # deleted or no-longer-accessible documents silently vanish

    # -------------------------------------------------------- research contexts
    async def _visible_chunk(self, db: AsyncSession, p: Principal, chunk_id: str) -> tuple[Chunk, str] | None:
        ch = await db.get(Chunk, chunk_id)
        if ch is None or ch.org_id != p.org_id or ch.collection_id not in await accessible_collection_ids(db, p):
            return None
        doc = await db.get(Document, ch.document_id)
        return (ch, doc.title) if doc else None

    async def create_context(self, db: AsyncSession, p: Principal, name: str, goal: str = "") -> dict:
        cid = new_id()
        await self._upsert(db, p, "research_context", cid, META_KEY, {"name": name[:200], "goal": goal[:1000], "pinned_documents": []}, None)
        return {"id": cid, "name": name[:200], "goal": goal[:1000]}

    async def _meta(self, db: AsyncSession, p: Principal, context_id: str) -> MemoryItem:
        meta = (await db.execute(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "research_context",
            MemoryItem.scope_id == context_id, MemoryItem.key == META_KEY))).scalar_one_or_none()
        if meta is None:
            raise NotFound("Research context not found")
        return meta

    async def list_contexts(self, db: AsyncSession, p: Principal) -> list[dict]:
        rows = (await db.execute(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "research_context",
            MemoryItem.key == META_KEY).order_by(MemoryItem.updated_at.desc()))).scalars().all()
        return [{"id": r.scope_id, "name": r.value["name"], "goal": r.value.get("goal", ""),
                 "pinned_documents": len(r.value.get("pinned_documents", []))} for r in rows]

    async def pin_document(self, db: AsyncSession, p: Principal, context_id: str, document_id: str) -> None:
        meta = await self._meta(db, p, context_id)
        doc = await db.get(Document, document_id)
        if doc is None or doc.org_id != p.org_id or doc.collection_id not in await accessible_collection_ids(db, p):
            raise NotFound("Document not found")
        pins = list(dict.fromkeys([*meta.value.get("pinned_documents", []), document_id]))
        await self._upsert(db, p, "research_context", context_id, META_KEY, {**meta.value, "pinned_documents": pins}, None)

    async def add_finding(self, db: AsyncSession, p: Principal, context_id: str, chunk_id: str, note: str = "") -> dict:
        await self._meta(db, p, context_id)
        vis = await self._visible_chunk(db, p, chunk_id)
        if vis is None:
            raise NotFound("Passage not found")  # same answer for missing and for inaccessible
        ch, title = vis
        fid = f"finding:{new_id()[:10]}"
        value = {"chunk_id": ch.id, "document_id": ch.document_id, "title": title, "page_start": ch.page_start, "section": ch.section,
                 "version": ch.version, "quote": ch.text[:500], "note": note[:500]}
        await self._upsert(db, p, "research_context", context_id, fid, value, None)
        await self.pin_document(db, p, context_id, ch.document_id)
        return {"key": fid, **value}

    async def get_context(self, db: AsyncSession, p: Principal, context_id: str) -> dict:
        meta = await self._meta(db, p, context_id)
        rows = (await db.execute(select(MemoryItem).where(
            MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id, MemoryItem.kind == "research_context",
            MemoryItem.scope_id == context_id, MemoryItem.key != META_KEY).order_by(MemoryItem.created_at))).scalars().all()
        findings = []
        for r in rows:
            vis = await self._visible_chunk(db, p, r.value["chunk_id"])
            if vis is None:  # access revoked or document deleted: never show the stored snapshot
                findings.append({"id": r.id, "hidden": True, "reason": "source no longer accessible"})
            else:
                findings.append({"id": r.id, "hidden": False, **r.value})
        return {"id": context_id, **{k: meta.value[k] for k in ("name", "goal")}, "pinned_documents": meta.value.get("pinned_documents", []),
                "findings": findings}

    async def context_document_ids(self, db: AsyncSession, p: Principal, context_id: str) -> list[str]:
        meta = await self._meta(db, p, context_id)
        return list(meta.value.get("pinned_documents", []))

    async def delete_context(self, db: AsyncSession, p: Principal, context_id: str) -> None:
        await self._meta(db, p, context_id)
        await db.execute(delete(MemoryItem).where(MemoryItem.org_id == p.org_id, MemoryItem.user_id == p.user_id,
                                                  MemoryItem.kind == "research_context", MemoryItem.scope_id == context_id))
