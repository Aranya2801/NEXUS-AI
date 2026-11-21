"""Deterministic, rule-based stand-in for an LLM so the whole system, tests and demos run offline.

It is NOT a model of LLM quality: results obtained with it (retrieval metrics, adaptive-policy gains) validate
the plumbing only and must be labelled as mock-LLM runs in any report."""
from __future__ import annotations

import json
import re

from app.llm.base import LLMResponse
from app.retrieval.sparse import STOPWORDS

_TOKEN = re.compile(r"\w+", re.UNICODE)
_FILLER = re.compile(r"^(please|can you|could you|tell me|i want to know|i would like to know|what do you know about)\s+", re.I)
_PRONOUNS = {"it", "they", "this", "that", "them", "these", "those", "its", "their"}
_SPLIT = re.compile(r"\s*(?:;|\band\b|\bvs\.?\b|\bversus\b)\s*", re.I)


def _keywords(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text)) if t not in STOPWORDS]


class MockLLM:
    name = "mock-llm"

    async def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> LLMResponse:
        task = system.split("\n", 1)[0].removeprefix("TASK:").strip()
        handler = getattr(self, f"_{task}", None)
        out = handler(system, user) if handler else {}
        text = json.dumps(out)
        return LLMResponse(text, self.name, prompt_tokens=len(_TOKEN.findall(system + user)), completion_tokens=len(_TOKEN.findall(text)))

    # --- tasks -----------------------------------------------------------------------------
    def _rewrite(self, system: str, user: str) -> dict:
        hist, _, q = user.partition("\nQUESTION: ")
        q = _FILLER.sub("", q.strip()).rstrip("?").strip()
        history_lines = [ln for ln in hist.splitlines()[1:] if ln.startswith("user:")]
        if history_lines and _PRONOUNS & {w.lower() for w in _TOKEN.findall(q)}:
            topic = " ".join(_keywords(history_lines[-1].split(":", 1)[1])[:4])
            q = f"{q} {topic}".strip()
        return {"query": q}

    def _decompose(self, system: str, user: str) -> dict:
        m = re.search(r"at most (\d+)", system)
        max_n = int(m.group(1)) if m else 4
        q = user.strip().rstrip("?")
        compare = re.match(r"(?i)compare\s+(.*)", q)
        parts = [p.strip() for p in _SPLIT.split(compare.group(1) if compare else q) if p.strip()]
        subs = [f"{p}?" for p in parts] if len(parts) > 1 else [user.strip()]
        return {"queries": subs[:max_n]}

    def _multi_query(self, system: str, user: str) -> dict:
        m = re.search(r"Write (\d+)", system)
        n = int(m.group(1)) if m else 3
        kw = _keywords(user)
        variants = [" ".join(kw), " ".join(reversed(kw)), f"{' '.join(kw)} overview details"]
        return {"queries": [v for v in variants if v][:n]}

    def _hyde(self, system: str, user: str) -> dict:
        kw = " ".join(_keywords(user)[:8])
        return {"passage": f"This document discusses {kw}. The main findings about {kw} are described in detail below."}

    def _compress(self, system: str, user: str) -> dict:
        q_part, _, body = user.partition("<passage>")
        q_terms = set(_keywords(q_part.replace("QUESTION:", "")))
        keep = []
        for line in body.splitlines():
            m = re.match(r"\[(\d+)\]\s*(.*)", line)
            if m and q_terms & set(_keywords(m.group(2))):
                keep.append(int(m.group(1)))
        return {"keep": keep}

    def _extract_graph(self, system: str, user: str) -> dict:
        from app.knowledge_graph.rule_extractor import RuleBasedExtractor

        body = user.split("<passage>", 1)[-1].rsplit("</passage>", 1)[0].strip()
        ex = RuleBasedExtractor().extract_text(body)
        return {"entities": [{"name": e.surface, "type": e.type} for e in ex.entities],
                "relations": [{"source": r.source, "predicate": r.predicate, "target": r.target, "evidence": r.evidence}
                              for r in ex.relations]}

    def _answer(self, system: str, user: str) -> dict:
        from app.agents.textutil import extractive_claims

        question = user.split("\n", 1)[0].replace("QUESTION:", "").strip()
        passages = [{"id": int(m.group(1)), "text": m.group(2)}
                    for m in re.finditer(r'<passage id="(\d+)"[^>]*>\n(.*?)\n</passage>', user, flags=re.S)]
        return {"claims": extractive_claims(question, passages)}

    def _verify(self, system: str, user: str) -> dict:
        """Stricter than the lexical verifier in the workflow: a claim must be covered by ONE cited sentence."""
        from app.retrieval.compression import split_sentences

        passages = {int(m.group(1)): m.group(2) for m in re.finditer(r'<passage id="(\d+)">\n(.*?)\n</passage>', user, flags=re.S)}
        verdicts = []
        for m in re.finditer(r"^\[(\d+)\] \(cites (\[[^\]]*\])\) (.*)$", user, flags=re.M):
            idx, cites, text = int(m.group(1)), json.loads(m.group(2)), m.group(3)
            want = set(_keywords(text))
            best = 0.0
            for pid in cites:
                for sent in split_sentences(passages.get(pid, "")):
                    have = set(_keywords(sent))
                    best = max(best, len(want & have) / len(want) if want else 0.0)
            verdicts.append({"claim": idx, "supported": best >= 0.6})
        return {"verdicts": verdicts}
