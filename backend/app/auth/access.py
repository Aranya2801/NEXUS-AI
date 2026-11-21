"""Collection-level authorisation. These helpers are the single source of truth for what a
Principal may see; retrieval (Phase 3) reuses `accessible_collection_ids` inside its queries."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.exceptions import NotFound, PermissionDenied
from app.database.models import Collection, CollectionAccess


async def accessible_collection_ids(db: AsyncSession, p: Principal) -> set[str]:
    rows = (await db.execute(select(Collection.id, Collection.is_restricted).where(Collection.org_id == p.org_id))).all()
    if p.role == "admin":
        return {r.id for r in rows}
    granted = set((await db.execute(
        select(CollectionAccess.collection_id).where(CollectionAccess.user_id == p.user_id)
    )).scalars())
    return {r.id for r in rows if not r.is_restricted or r.id in granted}


async def require_collection(db: AsyncSession, p: Principal, collection_id: str, *, write: bool = False) -> Collection:
    """Returns the collection or raises NotFound (never 403 for foreign/hidden ones: no existence leak)."""
    col = await db.get(Collection, collection_id)
    if col is None or col.org_id != p.org_id or collection_id not in await accessible_collection_ids(db, p):
        raise NotFound("Collection not found")
    if write and not p.at_least("editor"):
        raise PermissionDenied("Editor role required")
    return col
