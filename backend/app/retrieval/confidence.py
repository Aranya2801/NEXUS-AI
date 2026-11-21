"""Retrieval-confidence and evidence-sufficiency heuristics.

`confidence = 0.5*coverage + 0.35*top_score + 0.15*margin` with weights chosen a priori and NOT calibrated.
Phase 9 must calibrate these against labelled data before any claim about their accuracy."""
from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.sparse import tokenize
from app.retrieval.types import Hit


@dataclass(slots=True)
class Confidence:
    score: float
    coverage: float
    top_score: float
    margin: float


def _top_norm(hit: Hit, rrf_k: int) -> float:
    if "rerank" in hit.scores:
        return min(1.0, max(0.0, hit.scores["rerank"]))
    n_lists = max(1, len(hit.ranks))
    return min(1.0, hit.score * (rrf_k + 1) / n_lists)  # 1.0 == ranked first by every retriever


def retrieval_confidence(query: str, hits: list[Hit], rrf_k: int = 60, probe: int = 3) -> Confidence:
    if not hits:
        return Confidence(0.0, 0.0, 0.0, 0.0)
    q = set(tokenize(query))
    covered: set[str] = set()
    for h in hits[:probe]:
        covered |= q & set(tokenize(h.text))
    coverage = len(covered) / len(q) if q else 0.0
    top = _top_norm(hits[0], rrf_k)
    last = _top_norm(hits[min(len(hits), 5) - 1], rrf_k)
    margin = (top - last) / top if top > 0 else 0.0
    return Confidence(round(0.5 * coverage + 0.35 * top + 0.15 * margin, 4), round(coverage, 4), round(top, 4), round(margin, 4))
