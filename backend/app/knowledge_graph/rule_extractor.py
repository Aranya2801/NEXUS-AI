"""Offline, rule-based entity/relation extractor (the default). Precision-oriented and domain-neutral:
capitalised phrases, acronyms/model names, hyphenated technical terms, organisations and 'Name et al.' authors;
relations from a predicate lexicon between nearby entity mentions, with co-occurrence as a low-confidence fallback.
It will miss lowercase concepts that are not hyphenated; use the LLM extractor (or a gazetteer) for those."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.knowledge_graph.resolution import canonicalize
from app.knowledge_graph.types import ChunkExtraction, RawEntity, RawRelation
from app.retrieval.compression import split_sentences

_STOP_CAP = {
    "the", "this", "that", "these", "those", "however", "moreover", "furthermore", "therefore", "thus", "we", "our", "in",
    "it", "its", "they", "their", "for", "to", "of", "and", "or", "but", "as", "on", "at", "by", "with", "from", "an", "a",
    "section", "figure", "table", "abstract", "introduction", "results", "conclusion", "methods", "finally", "first",
    "second", "third", "also", "both", "each", "while", "although", "since", "because", "when", "where", "which", "what",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "one", "two", "three",
    "many", "most", "some", "such", "other", "another", "here", "there", "then", "if", "all", "any", "no", "not", "so",
}
_HYPHEN_STOP = {"state-of-the-art", "well-known", "long-term", "short-term", "real-world", "large-scale", "high-quality"}
_ORG_SUFFIX = r"(?:Inc|Corp|Corporation|Ltd|LLC|GmbH|University|Institute|Labs?|Research|Foundation|Group|Technologies|Systems)"

_PATTERNS = [
    (re.compile(r"\b(?:is|are|was|were)\s+(?:improved|enhanced|extended|outperformed|surpassed|proposed|introduced|used|employed|adopted)\s+by\b"), None),
    (re.compile(r"\b(?:improv(?:es|ed|ing)|enhanc(?:es|ed)|boosts?|boosted)\b"), "improves"),
    (re.compile(r"\b(?:extend(?:s|ed|ing)|builds?\s+on|built\s+on|generaliz(?:es|ed)|generalis(?:es|ed))\b"), "extends"),
    (re.compile(r"\b(?:outperform(?:s|ed)|surpass(?:es|ed)|beats?)\b"), "outperforms"),
    (re.compile(r"\b(?:is|are)?\s*(?:based\s+on|derived\s+from|adapted\s+from|adapts?)\b"), "based_on"),
    (re.compile(r"\b(?:evaluated\s+on|tested\s+on|trained\s+on|benchmark(?:ed)?\s+on)\b"), "evaluated_on"),
    (re.compile(r"\b(?:limited\s+by|suffers?\s+from|struggles?\s+with|bottlenecked\s+by)\b"), "limited_by"),
    (re.compile(r"\b(?:compar(?:es|ed)\s+(?:with|to|against)|versus|vs\.?)\b"), "compared_with"),
    (re.compile(r"\b(?:part\s+of|component\s+of|module\s+of)\b"), "part_of"),
    (re.compile(r"\b(?:propos(?:es|ed)|introduc(?:es|ed)|present(?:s|ed))\b"), "proposes"),
    (re.compile(r"\b(?:uses?|used|employs?|employed|leverag(?:es|ed)|relies\s+on|rely\s+on)\b"), "uses"),
]
_PASSIVE_MAP = {"improved": "improves", "enhanced": "improves", "extended": "extends", "outperformed": "outperforms",
                "surpassed": "outperforms", "proposed": "proposes", "introduced": "proposes", "used": "uses",
                "employed": "uses", "adopted": "uses"}
_PASSIVE = re.compile(r"\b(?:is|are|was|were)\s+(improved|enhanced|extended|outperformed|surpassed|proposed|introduced|used|employed|adopted)\s+by\b")

_SUBJECT_VERB = re.compile(r"^(?:is|are|was|were|has|have|can|could|approximates?|reduces?|achieves?|requires?|supports?|provides?|replaces?|computes?|scales?|learns?|applies|applied|addresses|handles?|enables?|allows?|combines?|achieved)\b")
_RX_ORG = re.compile(rf"\b(?:[A-Z][\w&-]*\s+){{0,3}}{_ORG_SUFFIX}\b")
_RX_PERSON = re.compile(r"\b(?:Dr\.|Prof\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|\b[A-Z][a-z]+(?:\s+and\s+[A-Z][a-z]+)?\s+et\s+al\.?")
_RX_MODEL = re.compile(r"\b(?:[A-Z][A-Za-z]*\d[\w.-]*|[A-Z]{2,}[a-z]?(?:-\d+)?|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")
_RX_CAPSEQ = re.compile(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*\b")
_RX_HYPHEN = re.compile(r"\b[a-z]{3,}-[a-z]{3,}(?:-[a-z]{3,})?\b")


@dataclass(slots=True)
class _Span:
    start: int
    end: int
    text: str
    type: str


class RuleBasedExtractor:
    name = "rule-v1"

    def __init__(self, gazetteer: dict[str, str] | None = None, max_entities: int = 25, max_relations: int = 30) -> None:
        self.gaz = {k.lower(): v for k, v in (gazetteer or {}).items()}
        self.max_entities, self.max_relations = max_entities, max_relations

    # ---------------------------------------------------------------- entities
    def _spans(self, sentence: str, chunk_text: str) -> list[_Span]:
        cands: list[_Span] = []
        for m in _RX_ORG.finditer(sentence):
            cands.append(_Span(m.start(), m.end(), m.group(0), "organization"))
        for m in _RX_PERSON.finditer(sentence):
            cands.append(_Span(m.start(), m.end(), m.group(0).rstrip("."), "person"))
        for m in _RX_MODEL.finditer(sentence):
            if m.group(0).lower() not in _STOP_CAP:
                cands.append(_Span(m.start(), m.end(), m.group(0), "model" if re.search(r"\d|[a-z][A-Z]", m.group(0)) else "concept"))
        for m in _RX_CAPSEQ.finditer(sentence):
            words = re.split(r"[ -]", m.group(0))
            txt = m.group(0)
            if words[0].lower() in _STOP_CAP:  # strip leading stop-cap ("The Transformer")
                words = words[1:]
                txt = " ".join(words)
                if not words:
                    continue
                start = m.end() - len(txt)
            else:
                start = m.start()
            if len(words) >= 2:
                cands.append(_Span(start, m.end(), txt, "concept"))
            else:
                w = words[0]
                initial = start == 0
                rest = sentence[m.end():].lstrip()
                follows_predicate = initial and (_SUBJECT_VERB.match(rest) is not None or any(p.match(rest) for p, _ in _PATTERNS[1:]))
                elsewhere = initial and re.search(rf"(?<![.!?]\s)(?<!^)\b{re.escape(w)}\b", chunk_text[1:] if chunk_text else "") is not None
                if w.lower() not in _STOP_CAP and len(w) > 2 and (not initial or follows_predicate or elsewhere):
                    cands.append(_Span(start, m.end(), w, "concept"))
        for m in _RX_HYPHEN.finditer(sentence):
            if m.group(0) not in _HYPHEN_STOP:
                cands.append(_Span(m.start(), m.end(), m.group(0), "concept"))
        for term, typ in self.gaz.items():
            for m in re.finditer(rf"\b{re.escape(term)}\b", sentence, flags=re.I):
                cands.append(_Span(m.start(), m.end(), m.group(0), typ))
        # longest-first, drop overlaps
        cands.sort(key=lambda s: (-(s.end - s.start), s.start))
        chosen: list[_Span] = []
        for c in cands:
            if not any(c.start < k.end and k.start < c.end for k in chosen):
                chosen.append(c)
        return sorted(chosen, key=lambda s: s.start)

    # --------------------------------------------------------------- relations
    @staticmethod
    def _predicate(between: str) -> tuple[str, bool] | None:
        """Returns (predicate, swap_direction)."""
        pm = _PASSIVE.search(between)
        if pm:
            return _PASSIVE_MAP[pm.group(1)], True
        for rx, pred in _PATTERNS[1:]:
            if rx.search(between):
                return pred or "related_to", False
        return None

    def extract_text(self, text: str) -> ChunkExtraction:
        ent_seen: dict[str, RawEntity] = {}
        rels: list[RawRelation] = []
        rel_keys: set[tuple[str, str, str]] = set()
        for sent in split_sentences(text):
            spans = self._spans(sent, text)
            for sp in spans:
                ent_seen.setdefault(canonicalize(sp.text), RawEntity(sp.text, sp.type))
            related = 0
            for i, a in enumerate(spans):
                for b in spans[i + 1 : i + 4]:
                    between = sent[a.end : b.start]
                    if len(between.split()) > 14 or canonicalize(a.text) == canonicalize(b.text):
                        continue
                    found = self._predicate(between)
                    if found:
                        pred, swap = found
                        src, tgt = (b, a) if swap else (a, b)
                        conf = 0.7
                    elif related < 3 and len(between.split()) <= 8:
                        pred, src, tgt, conf = "related_to", a, b, 0.3
                        related += 1
                    else:
                        continue
                    key = (canonicalize(src.text), pred, canonicalize(tgt.text))
                    if key not in rel_keys:
                        rel_keys.add(key)
                        rels.append(RawRelation(src.text, pred, tgt.text, sent, conf))
        return ChunkExtraction(list(ent_seen.values())[: self.max_entities], rels[: self.max_relations])

    async def extract(self, chunks: list[tuple[str, str]]) -> dict[str, ChunkExtraction]:
        return {cid: self.extract_text(text) for cid, text in chunks}
