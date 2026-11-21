"""Contextual compression that preserves evidence. The LLM returns sentence INDICES only; the kept text is always
sliced from the stored passage, so (a) nothing can be paraphrased or invented and (b) instructions injected into a
document cannot reach the output. Invalid/empty answers fall back to lexical selection; at least one sentence is kept."""
from __future__ import annotations

import re

from app.llm import prompts
from app.llm.base import LLM, UsageMeter
from app.llm.jsonutil import parse_json_object
from app.retrieval.sparse import tokenize
from app.retrieval.types import Hit

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s and s.strip()]


def _lexical_keep(question: str, sentences: list[str], limit: int) -> list[int]:
    q = set(tokenize(question))
    scored = sorted(((len(q & set(tokenize(s))), -i, i) for i, s in enumerate(sentences)), reverse=True)
    keep = [i for sc, _, i in scored if sc > 0][:limit] or [scored[0][2]]
    return sorted(keep)


async def compress_hit(llm: LLM, question: str, hit: Hit, meter: UsageMeter | None = None, max_keep: int = 6) -> Hit:
    sentences = split_sentences(hit.text)
    if len(sentences) <= 1:
        hit.compressed_text, hit.evidence_sentences = hit.text, list(range(len(sentences)))
        return hit
    keep: list[int] = []
    try:
        r = await llm.complete(prompts.COMPRESS, prompts.compress_user(question, sentences), max_tokens=120)
        if meter:
            meter.add("compress", r)
        obj = parse_json_object(r.text)
        raw = obj.get("keep") if obj else None
        if isinstance(raw, list):
            keep = sorted({i for i in raw if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(sentences)})[:max_keep]
    except Exception:  # noqa: BLE001
        keep = []
    if not keep:
        keep = _lexical_keep(question, sentences, max_keep)
    hit.evidence_sentences = keep
    hit.compressed_text = " ".join(sentences[i] for i in keep)
    return hit
