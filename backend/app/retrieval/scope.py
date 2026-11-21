"""Builds the SQL scope for a retrieval query from a Principal. Every retriever (dense, sparse, graph)
selects FROM this scope, so authorisation, tenancy and version visibility are enforced *inside* the
query that ranks candidates; nothing unauthorised is ever scored, and nothing is filtered afterwards."""
from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.auth.access import accessible_collection_ids
from app.auth.principal import Principal
from app.core.exceptions import ValidationFailed
from app.database.models import Chunk, DocumentVersion
from app.retrieval.types import SearchFilters


async def build_scope(db: AsyncSession, p: Principal, f: SearchFilters) -> tuple[list[ColumnElement], int]:
    """Returns (conditions, n_collections). An empty authorised set yields a condition that matches nothing."""
    allowed = await accessible_collection_ids(db, p)
    if f.collection_ids is not None:
        allowed &= set(f.collection_ids)  # intersect: requesting a hidden collection never widens access
    if not allowed:
        return [Chunk.id.is_(None)], 0  # matches no rows

    conds: list[ColumnElement] = [
        Chunk.org_id == p.org_id, DocumentVersion.org_id == p.org_id,
        Chunk.collection_id.in_(allowed), Chunk.level == "child",
    ]
    ever_live = or_(DocumentVersion.is_current.is_(True), DocumentVersion.superseded_at.is_not(None))
    if f.temporal == "current":
        conds.append(Chunk.is_current.is_(True))
    elif f.temporal == "all_versions":
        conds.append(ever_live)
    elif f.temporal == "as_of":
        if f.as_of is None:
            raise ValidationFailed("as_of is required when temporal='as_of'")
        conds += [ever_live, DocumentVersion.created_at <= f.as_of,
                  or_(DocumentVersion.superseded_at.is_(None), DocumentVersion.superseded_at > f.as_of)]
    if f.document_ids is not None:
        conds.append(Chunk.document_id.in_(f.document_ids) if f.document_ids else Chunk.id.is_(None))
    if f.doc_types:
        conds.append(Chunk.doc_type.in_([d.lower().lstrip(".") for d in f.doc_types]))
    if f.kinds:
        conds.append(Chunk.kind.in_(f.kinds))
    if f.departments:
        conds.append(Chunk.department.in_(f.departments))
    if f.date_from:
        conds.append(DocumentVersion.created_at >= f.date_from)
    if f.date_to:
        conds.append(DocumentVersion.created_at <= f.date_to)
    return [and_(*conds)], len(allowed)
