"""Dense retrieval: cosine similarity over chunk embeddings. pgvector (`<=>`, HNSW) on PostgreSQL,
exact numpy cosine on SQLite. Only chunks embedded with the *same model* as the query are compared."""
from __future__ import annotations

import numpy as np
from sqlalchemy import Float, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.database.base import EmbeddingType
from app.database.models import Chunk, DocumentVersion

HNSW_EF_SEARCH = 200  # filtered ANN queries lose recall at the default (40); raise it


async def dense_search(db: AsyncSession, qvec: list[float], embed_model: str, conds: list[ColumnElement], n: int) -> list[tuple[str, float]]:
    scoped = [*conds, Chunk.embed_model == embed_model, Chunk.embedding.is_not(None)]
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(max(HNSW_EF_SEARCH, n))}"))
        qparam = bindparam("qv", value=[float(x) for x in qvec], type_=EmbeddingType(len(qvec)))
        dist = Chunk.embedding.op("<=>", return_type=Float)(qparam)  # pgvector cosine distance
        stmt = (
            select(Chunk.id, (1 - dist).label("s"))
            .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
            .where(*scoped)
            .order_by(dist)
            .limit(n)
        )
        return [(r.id, float(r.s)) for r in (await db.execute(stmt)).all()]
    rows = (await db.execute(select(Chunk.id, Chunk.embedding).join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
                             .where(*scoped))).all()
    if not rows:
        return []
    mat = np.asarray([r.embedding for r in rows], dtype=np.float32)
    q = np.asarray(qvec, dtype=np.float32)
    sims = (mat @ q) / ((np.linalg.norm(mat, axis=1) * np.linalg.norm(q)) + 1e-12)
    order = np.argsort(-sims)[:n]
    return [(rows[i].id, float(sims[i])) for i in order]
