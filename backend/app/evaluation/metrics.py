"""Retrieval metrics and the small statistics toolkit used by the experiments (numpy only, no scipy).

Retrieval metrics work on a ranked list of *document ids* (duplicates collapsed, first occurrence wins) against a set of
gold document ids with binary relevance."""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def dedupe(ranked: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for r in ranked:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def precision_at_k(ranked: Sequence[str], gold: set[str], k: int) -> float:
    top = dedupe(ranked)[:k]
    return sum(1 for r in top if r in gold) / k if k else 0.0


def recall_at_k(ranked: Sequence[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(gold & set(dedupe(ranked)[:k])) / len(gold)


def reciprocal_rank(ranked: Sequence[str], gold: set[str]) -> float:
    for i, r in enumerate(dedupe(ranked), 1):
        if r in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    top = dedupe(ranked)[:k]
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(top, 1) if r in gold)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def retrieval_metrics(ranked: Sequence[str], gold: set[str]) -> dict[str, float]:
    out = {}
    for k in (5, 10):
        out[f"precision_at_{k}"] = precision_at_k(ranked, gold, k)
        out[f"recall_at_{k}"] = recall_at_k(ranked, gold, k)
        out[f"ndcg_at_{k}"] = ndcg_at_k(ranked, gold, k)
    out["mrr"] = reciprocal_rank(ranked, gold)
    return out


def percentile(xs: Sequence[float], q: float) -> float:
    return float(np.percentile(np.asarray(xs, dtype=float), q)) if len(xs) else 0.0


def paired_bootstrap(a: Sequence[float], b: Sequence[float], *, n: int = 10_000, seed: int = 0) -> dict[str, float]:
    """Mean of (a - b) with a 95% percentile-bootstrap CI over paired per-query differences."""
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if len(d) == 0:
        return {"mean_diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n, len(d)))
    means = d[idx].mean(axis=1)
    return {"mean_diff": float(d.mean()), "ci_low": float(np.percentile(means, 2.5)), "ci_high": float(np.percentile(means, 97.5)),
            "n": int(len(d))}


def bootstrap_mean_ci(x: Sequence[float], *, n: int = 10_000, seed: int = 0) -> tuple[float, float, float]:
    v = np.asarray(x, dtype=float)
    if len(v) == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    return float(v.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def wilcoxon_signed_rank(a: Sequence[float], b: Sequence[float]) -> dict[str, float]:
    """Two-sided Wilcoxon signed-rank test, normal approximation with tie and continuity correction. Zero differences dropped."""
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    d = d[np.abs(d) > 1e-12]
    n = len(d)
    if n < 6:
        return {"n": int(n), "p": 1.0, "note": "fewer than 6 non-zero pairs; test not informative"}
    absd = np.abs(d)
    order = absd.argsort()
    ranks = np.empty(n)
    sorted_abs = absd[order]
    i = 0
    tie_term = 0.0
    while i < n:
        j = i
        while j + 1 < n and sorted_abs[j + 1] == sorted_abs[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    w_plus = float(ranks[d > 0].sum())
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if var <= 0:
        return {"n": int(n), "p": 1.0}
    z = (abs(w_plus - mean) - 0.5) / math.sqrt(var)
    p = math.erfc(max(z, 0.0) / math.sqrt(2))
    return {"n": int(n), "w_plus": w_plus, "z": float(z), "p": float(min(1.0, p))}


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """Probability a random positive outranks a random negative (ties count half). None if one class is missing."""
    s = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=int)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    greater = (pos[:, None] > neg[None, :]).sum()
    equal = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * equal) / (len(pos) * len(neg)))
