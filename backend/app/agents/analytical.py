"""Agent 5 — Analytical: builds the (sanitised) evidence passages and analyses them: cross-document comparison, quantitative
findings, temporal/year trends, version differences and contradiction detection. Heuristic and deterministic: it reports
*candidates* with their source sentences for the verifier/reviewer; it does not claim they are true."""
from __future__ import annotations

import re

from app.agents.deps import AgentDeps
from app.agents.textutil import best_sentence, content_tokens, numbers
from app.retrieval.compression import split_sentences
from app.security.injection import sanitize_passage

_NEG = re.compile(r"\b(?:not|no|never|cannot|without|fails?|unable)\b|n't", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_METRIC = re.compile(r"\d\s?(?:%|percent|ms|s|x|bleu|f1|accuracy|auc|mb|gb|tokens)\b|\d+\.\d+", re.I)


def build_passages(evidence: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    passages, flags = [], []
    for h in evidence:
        if len(passages) >= limit:
            break
        text, removed = sanitize_passage(h.get("compressed_text") or h["text"])
        if removed:
            flags.append({"chunk_id": h["chunk_id"], "document_id": h["document_id"], "removed": [r[:200] for r in removed]})
        if not text.strip():
            continue
        pid = len(passages) + 1
        page = f", p.{h['page_start']}" if h.get("page_start") else ""
        passages.append({"id": pid, "chunk_id": h["chunk_id"], "document_id": h["document_id"], "title": h["title"], "filename": h["filename"],
                         "page_start": h.get("page_start"), "page_end": h.get("page_end"), "section": h.get("section"),
                         "version": h["version"], "is_current": h["is_current"], "text": text, "source": f"{h['title']}{page}"})
    return passages, flags


def analyze_passages(question: str, passages: list[dict], deep: bool) -> dict:
    q = content_tokens(question)
    sents = [(p, s) for p in passages for s in split_sentences(p["text"])]
    docs: dict[str, dict] = {}
    for p in passages:
        d = docs.setdefault(p["document_id"], {"document_id": p["document_id"], "title": p["title"], "passages": [], "versions": set()})
        d["passages"].append(p["id"])
        d["versions"].add(p["version"])
    for d in docs.values():
        own = [(p, s) for p, s in sents if p["document_id"] == d["document_id"]]
        d["key_sentence"] = max(own, key=lambda t: len(q & content_tokens(t[1])))[1] if own else ""
        d["versions"] = sorted(d["versions"])

    findings = []
    for p, s in sents:
        if _METRIC.search(s) and numbers(s):
            findings.append({"passage": p["id"], "document_id": p["document_id"], "sentence": s, "numbers": sorted(numbers(s))})
    contradictions = []
    for i, (pa, sa) in enumerate(sents):
        for pb, sb in sents[i + 1 :]:
            if pa["document_id"] == pb["document_id"]:
                continue
            ta, tb = content_tokens(sa), content_tokens(sb)
            shared = ta & tb
            if len(shared) < 3:
                continue
            jac = len(shared) / len(ta | tb)
            na, nb = numbers(sa), numbers(sb)
            if jac >= 0.35 and na and nb and not (na & nb):
                kind = "numeric"
            elif jac >= 0.5 and bool(_NEG.search(sa)) != bool(_NEG.search(sb)):
                kind = "polarity"
            else:
                continue
            contradictions.append({"type": kind, "a": {"passage": pa["id"], "sentence": sa}, "b": {"passage": pb["id"], "sentence": sb},
                                   "shared_terms": sorted(shared)[:8]})
    out = {"documents": list(docs.values()), "quantitative_findings": findings[:12], "contradictions": contradictions[:5],
           "version_notes": [{"passage": p["id"], "title": p["title"], "version": p["version"]} for p in passages if not p["is_current"]]}
    if deep:
        years = [(int(m.group(0)), s, p["id"]) for p, s in sents for m in [_YEAR.search(s)] if m]
        out["timeline"] = [{"year": y, "sentence": s, "passage": pid} for y, s, pid in sorted(years)][:8]
        out["comparison"] = [{"document": d["title"], "key_sentence": d["key_sentence"], "passages": d["passages"]} for d in docs.values()]
    return out


async def analyze(deps: AgentDeps, state: dict) -> dict:
    passages, flags = build_passages(state.get("evidence", []), deps.settings.answer_passages)
    deep = "analytical" in state.get("selected_agents", [])
    analysis = analyze_passages(state.get("standalone") or state["query"], passages, deep) if passages else {}
    return {"passages": passages, "injection_flags": flags, "analysis": analysis,
            "_trace": {"passages": len(passages), "injection_sanitized": len(flags), "deep": deep,
                       "contradictions": len(analysis.get("contradictions", []))}}
