"""Deterministic feature-hashing embedder (the offline 'mock' provider).

Signed hashing of unigrams+bigrams into `dim` buckets, L2-normalised. It captures lexical overlap,
NOT semantics, so retrieval-quality experiments must use a real embedding model; this exists so the
system, tests and demos run offline. Results obtained with it must be labelled accordingly.
"""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder:
    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.name = f"hashing-v1:{dim}"

    def _vec(self, text: str) -> list[float]:
        toks = [t.lower() for t in _TOKEN.findall(text)]
        feats = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]
