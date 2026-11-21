"""Small, deterministic text helpers shared by the analytical, verification and quality agents."""
from __future__ import annotations

import re

from app.retrieval.compression import split_sentences
from app.retrieval.sparse import STOPWORDS

_TOKEN = re.compile(r"\w+", re.UNICODE)
_NUM = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?")


def stem(w: str) -> str:
    w = w.lower()
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("ches", "shes", "xes", "zes", "sses")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def content_tokens(text: str) -> set[str]:
    return {stem(t) for t in _TOKEN.findall(text) if t.lower() not in STOPWORDS and len(t) > 1}


def numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text):
        m = m.replace(",", "")
        out.add(str(float(m)).rstrip("0").rstrip(".") if "." in m else str(int(m)))
    return out


def coverage(want: set[str], have: set[str]) -> float:
    return len(want & have) / len(want) if want else 1.0


def best_sentence(claim: str, passage: str) -> tuple[float, str]:
    want = content_tokens(claim)
    best = (0.0, "")
    for s in split_sentences(passage):
        c = coverage(want, content_tokens(s))
        if c > best[0]:
            best = (c, s)
    return best


def extractive_claims(question: str, passages: list[dict], max_claims: int = 4) -> list[dict]:
    """Deterministic grounded fallback: verbatim sentences that overlap the question, each citing its passage."""
    q = content_tokens(question)
    scored = []
    for p in passages:
        for s in split_sentences(p["text"]):
            overlap = len(q & content_tokens(s))
            if overlap:
                scored.append((overlap, -p["id"], s, p["id"]))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    seen, claims = set(), []
    for _, _, s, pid in scored:
        if s not in seen and len(claims) < max_claims:
            seen.add(s)
            claims.append({"text": s, "citations": [pid]})
    return claims
