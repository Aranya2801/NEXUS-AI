"""Prompt-injection defences for retrieved (untrusted) text.

Layers: (1) sentences that look like instructions to an AI system are removed from the evidence before generation
and reported; (2) passages are always passed inside <passage> data blocks with an instruction to treat them as data;
(3) model output is validated against the evidence (claims must be supported by cited passages), so even a missed
injection cannot add unsupported content. This is a heuristic filter, not a guarantee."""
from __future__ import annotations

import re

from app.retrieval.compression import split_sentences

_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|the)\b[^.\n]{0,30}\b(instructions?|prompts?|rules?|guidelines?|context)\b",
    r"\b(new|updated|revised)\s+instructions?\b",
    r"\byou\s+(?:are|must|should|will)\s+(?:now\s+)?(?:act|behave|respond|answer|reply)\b",
    r"\b(?:reveal|show|print|repeat|leak)\b[^.\n]{0,30}\b(?:system|hidden|developer)\s+(?:prompt|message|instructions?)\b",
    r"\bsystem\s+prompt\b",
    r"</?\s*(?:system|assistant|user|instructions?)\s*>",
    r"\[\s*/?INST\s*\]|<\|(?:im_start|im_end|system)\|>",
    r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|mention|reveal)\b[^.\n]{0,30}\b(?:user|human)\b",
    r"\bpretend\s+(?:to\s+be|you\s+are)\b",
    r"\b(?:respond|reply|answer|say|output)\s+(?:only\s+)?(?:with\s+)?['\"]?[A-Z]{4,}['\"]?\s*(?:$|[.!])",
)]


def scan(text: str) -> bool:
    return any(p.search(text) for p in _PATTERNS)


def sanitize_passage(text: str) -> tuple[str, list[str]]:
    """Returns (text without instruction-like sentences, removed sentences)."""
    kept, removed = [], []
    for s in split_sentences(text):
        (removed if scan(s) else kept).append(s)
    return " ".join(kept), removed
