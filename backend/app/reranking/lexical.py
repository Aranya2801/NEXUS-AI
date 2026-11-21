"""Deterministic lexical reranker. NOT a cross-encoder: it rescoring candidates with features the first-stage
retrievers don't use jointly (IDF-weighted term coverage over the candidate set, adjacent-term bigram overlap, exact
phrase match). It exists so reranking is testable offline and as an ablation baseline against a learned model."""
from __future__ import annotations

import math
import re

from app.retrieval.sparse import tokenize

_WS = re.compile(r"\s+")


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    return set(zip(tokens, tokens[1:]))


class LexicalReranker:
    name = "lexical-v1"

    async def score(self, query: str, texts: list[str]) -> list[float]:
        q = list(dict.fromkeys(tokenize(query)))
        if not q or not texts:
            return [0.0] * len(texts)
        toks = [tokenize(t) for t in texts]
        sets = [set(t) for t in toks]
        n = len(texts)
        idf = {t: math.log(1 + (n - sum(t in s for s in sets) + 0.5) / (sum(t in s for s in sets) + 0.5)) for t in q}
        idf_total = sum(idf.values()) or 1.0
        q_bi = _bigrams(tokenize(query))
        qnorm = _WS.sub(" ", query.lower()).strip(" ?.!")
        out = []
        for tt, ss, raw in zip(toks, sets, texts):
            weighted = sum(idf[t] for t in q if t in ss) / idf_total
            uniform = sum(t in ss for t in q) / len(q)
            bigram = (len(q_bi & _bigrams(tt)) / len(q_bi)) if q_bi else 0.0
            phrase = 1.0 if qnorm and qnorm in _WS.sub(" ", raw.lower()) else 0.0
            out.append(0.55 * (0.5 * weighted + 0.5 * uniform) + 0.30 * bigram + 0.15 * phrase)
        return out
