"""Sparse (lexical) retrieval: BM25 computed over the *scoped* corpus on SQLite/in-memory, and
PostgreSQL full-text search (`ts_rank_cd`) on PostgreSQL."""
from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.database.models import Chunk, DocumentVersion

_TOKEN = re.compile(r"\w+", re.UNICODE)
STOPWORDS = frozenset("a an the of and or to in on for with is are was were be by as at it this that from".split())


def tokenize(text_: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text_)) if t not in STOPWORDS]


class BM25:
    """Okapi BM25 with the non-negative (Lucene) IDF, so common terms never yield negative scores."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avgdl = (sum(self.len) / len(docs)) if docs else 0.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - v + 0.5) / (v + 0.5)) for t, v in df.items()}

    def score(self, query: list[str]) -> list[float]:
        out = []
        for tf, dl in zip(self.tf, self.len):
            s = 0.0
            for t in set(query):
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out.append(s)
        return out


async def sparse_search(db: AsyncSession, query: str, conds: list[ColumnElement], n: int) -> list[tuple[str, float]]:
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    base = select(Chunk.id).join(DocumentVersion, DocumentVersion.id == Chunk.version_id).where(*conds)
    if db.get_bind().dialect.name == "postgresql":
        tsq = func.to_tsquery("simple", " | ".join(dict.fromkeys(q_tokens)))
        tsv = func.to_tsvector("simple", Chunk.text)
        rank = func.ts_rank_cd(tsv, tsq).label("r")
        rows = (await db.execute(base.add_columns(rank).where(tsv.op("@@")(tsq)).order_by(rank.desc()).limit(n))).all()
        return [(r.id, float(r.r)) for r in rows]
    rows = (await db.execute(base.add_columns(Chunk.text))).all()
    if not rows:
        return []
    scores = BM25([tokenize(r.text) for r in rows]).score(q_tokens)
    ranked = sorted(((r.id, s) for r, s in zip(rows, scores) if s > 0), key=lambda t: -t[1])
    return ranked[:n]
