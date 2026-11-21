"""Reciprocal Rank Fusion (Cormack et al., 2009): score(d) = Σ_i w_i / (k + rank_i(d))."""
from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]], k: int = 60, weights: dict[str, float] | None = None
) -> list[tuple[str, float, dict[str, int]]]:
    """`rankings` maps retriever name → ids ordered best-first. Returns (id, fused score, per-retriever rank)
    sorted best-first; ties are broken by id for determinism."""
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in rankings.items():
        w = (weights or {}).get(name, 1.0)
        for r, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + r)
            ranks.setdefault(doc_id, {})[name] = r
    return sorted(((d, s, ranks[d]) for d, s in scores.items()), key=lambda t: (-t[1], t[0]))
