"""Structured research-report builder.

Pipeline: discover relevant documents → per section: scoped retrieval (+rerank, optional graph) → sanitised passages →
grounded claims → fact verification (shared with chat) → only supported/weak claims survive. Result tables, contradiction
candidates and research gaps are assembled from VERBATIM source sentences, never paraphrased. A final guard drops any block
whose numbers do not all appear in the sources it cites. Nothing is added from model memory: every cited statement can be
traced to a chunk, page and quote.

Honest limits: contradiction detection and gap detection are lexical heuristics (candidates, flagged as such); the offline
mock LLM only extracts sentences; results from different datasets/metrics are listed side by side, not normalised."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.agents import analytical, report as report_agent, verification
from app.agents.common import TRANSIENT
from app.agents.deps import AgentDeps, usage_dict
from app.agents.state import hit_dict
from app.agents.textutil import best_sentence, content_tokens, coverage, numbers
from app.llm.base import UsageMeter
from app.retrieval.adaptive import RetrievalPlan
from app.retrieval.compression import split_sentences
from app.retrieval.types import SearchFilters

GAP_CUE = re.compile(r"\b(future work|remains? (?:an )?open|open (?:problem|question)s?|unexplored|not yet|further research|"
                     r"left for future|yet to be|remains? unclear|remains? unknown)\b", re.I)
Progress = Callable[[str, str], Awaitable[None]]
_CUES = {
    "methodologies": re.compile(r"\b(uses?|used|builds? on|based on|propos\w+|introduc\w+|architecture|approach|method\w*|employs?|applies|attention|mixing)\b", re.I),
    "datasets": re.compile(r"\b(datasets?|benchmarks?|corpus|corpora|evaluat\w+|trained on|test set|data)\b", re.I),
    "limitations": re.compile(r"\b(limitations?|limited by|suffers?|drawbacks?|weakness\w*|fails?|reduced|struggles?|cannot)\b", re.I),
}


def focus_passages(passages: list[dict], section_id: str) -> list[dict]:
    """Offline-mock mode only: keep just the sentences matching the section's cue words, so the extractive mock does not put
    result sentences under 'Methodologies'. With a real LLM the model decides relevance, so passages are passed unchanged."""
    cue = _CUES.get(section_id)
    if cue is None:
        return passages
    out = []
    for ps in passages:
        kept = [s for s in split_sentences(ps["text"]) if cue.search(s)]
        if kept:
            out.append({**ps, "text": " ".join(kept)})
    return out


SECTIONS = [
    {"id": "methodologies", "title": "Methodologies and Architectures", "kind": "claims", "retrieval": "{t} methodology approach architecture method",
     "keywords": "method approach architecture uses based on train"},
    {"id": "datasets", "title": "Datasets and Benchmarks", "kind": "claims", "retrieval": "{t} dataset benchmark evaluation corpus",
     "keywords": "dataset benchmark corpus evaluate evaluated"},
    {"id": "results", "title": "Reported Quantitative Results", "kind": "results"},
    {"id": "limitations", "title": "Limitations", "kind": "claims", "retrieval": "{t} limitation weakness drawback suffers challenge",
     "keywords": "limitation suffers drawback weakness reduced"},
    {"id": "contradictions", "title": "Possible Contradictions and Inconsistencies", "kind": "contradictions"},
    {"id": "gaps", "title": "Research Gaps and Opportunities Stated in the Sources", "kind": "gaps", "retrieval": "{t} future work open problem remains unexplored"},
]
MAX_ROWS, MAX_GAPS = 20, 10


class CitationRegistry:
    """Global, deduplicated citations. Also remembers each cited chunk's full text for the final numeric guard (never persisted)."""

    def __init__(self) -> None:
        self.by_chunk: dict[str, dict] = {}
        self.sources: dict[str, str] = {}

    def cite(self, passage: dict, quote: str) -> int:
        c = self.by_chunk.get(passage["chunk_id"])
        if c is None:
            c = {"id": len(self.by_chunk) + 1, "chunk_id": passage["chunk_id"], "document_id": passage["document_id"], "title": passage["title"],
                 "filename": passage["filename"], "page_start": passage["page_start"], "page_end": passage["page_end"], "section": passage["section"],
                 "version": passage["version"], "is_current": passage["is_current"], "quotes": []}
            self.by_chunk[passage["chunk_id"]] = c
            self.sources[passage["chunk_id"]] = passage["text"]
        if quote and quote not in c["quotes"]:
            c["quotes"] = [*c["quotes"], quote][:3]
        return c["id"]

    def export(self) -> list[dict]:
        return [{**c, "quote": c["quotes"][0] if c["quotes"] else ""} for c in sorted(self.by_chunk.values(), key=lambda x: x["id"])]

    def source_numbers(self, ids: list[int]) -> set[str]:
        out: set[str] = set()
        for c in self.by_chunk.values():
            if c["id"] in ids:
                out |= numbers(self.sources[c["chunk_id"]])
        return out


class ReportBuilder:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps
        self.meter = UsageMeter()

    # ----------------------------------------------------------------- helpers
    async def _retrieve(self, state: dict, query: str, plan: RetrievalPlan, filters: SearchFilters) -> list[dict]:
        async with self.deps.session_factory() as db:
            p = await self.deps.principal(state, db)  # run owner re-validated before every data access
            res = await self.deps.pipeline.run(db, p, query, plan, filters=filters)
        self.meter.calls += res.usage.calls
        self.meter.prompt_tokens += res.usage.prompt_tokens
        self.meter.completion_tokens += res.usage.completion_tokens
        return [hit_dict(h) for h in res.hits]

    async def _claims(self, state: dict) -> list[dict]:
        last: Exception | None = None
        for attempt in range(3):  # transient LLM/network errors retried with backoff; then the extractive fallback takes over
            try:
                out = await report_agent.generate(self.deps, state)
                self._add_usage(out.get("llm_usage"))
                return out["claims"]
            except TRANSIENT as exc:
                last = exc
                await asyncio.sleep(0.2 * 2**attempt)
        from app.agents.textutil import extractive_claims

        return extractive_claims(state["standalone"], state["passages"])

    def _add_usage(self, u: dict | None) -> None:
        if u:
            self.meter.calls += u.get("calls", 0)
            self.meter.prompt_tokens += u.get("prompt_tokens", 0)
            self.meter.completion_tokens += u.get("completion_tokens", 0)

    # ------------------------------------------------------------------- build
    async def build(self, principal: dict, *, topic: str, title: str, collection_ids: list[str] | None, document_ids: list[str] | None,
                    max_documents: int, include_graph: bool, preferences: list[str] | None, progress: Progress) -> dict:
        state_base = {"principal": principal, "preferences": preferences or []}
        filters = SearchFilters(collection_ids=collection_ids, document_ids=document_ids)
        reg, pool = CitationRegistry(), {}
        removed: list[dict] = []
        used_claims: set[str] = set()

        # 1. discover documents ---------------------------------------------------------------------------------
        await progress("discover", "finding relevant documents")
        discover_plan = RetrievalPlan(name="report_discover", mode="hybrid", top_k=60, candidates=80, rerank=False, graph=include_graph)
        hits = await self._retrieve(state_base, topic, discover_plan, filters)
        score: dict[str, float] = {}
        info: dict[str, dict] = {}
        floor = self.deps.settings.report_min_relevance
        topic_terms = content_tokens(topic)
        best: dict[str, float] = {}
        for h in hits:  # dense search always returns neighbours, so require topic-term overlap in at least one retrieved passage
            best[h["document_id"]] = max(best.get(h["document_id"], 0.0), coverage(topic_terms, content_tokens(h["text"])))
        hits = [h for h in hits if best[h["document_id"]] >= floor]
        for h in hits:
            score[h["document_id"]] = score.get(h["document_id"], 0.0) + h["score"]
            d = info.setdefault(h["document_id"], {"document_id": h["document_id"], "title": h["title"], "filename": h["filename"], "versions": set(), "pages": set()})
            d["versions"].add(h["version"])
            if h.get("page_start"):
                d["pages"].add(h["page_start"])
        selected = [d for d, _ in sorted(score.items(), key=lambda t: (-t[1], t[0]))[:max_documents]]
        documents = [{**info[d], "versions": sorted(info[d]["versions"]), "pages": sorted(info[d]["pages"])} for d in selected]
        base = {"title": title, "topic": topic, "generated_at": datetime.now(timezone.utc).isoformat(),
                "parameters": {"max_documents": max_documents, "include_graph": include_graph, "collection_ids": collection_ids,
                               "document_ids": document_ids}, "documents": documents}
        if not selected:
            await progress("done", "no relevant documents")
            return {**base, "executive_summary": [], "sections": [], "citations": [],
                    "verification": {"claims_total": 0, "claims_kept": 0, "unsupported_removed": [], "dropped_at_final_check": []},
                    "coverage_notes": ["No documents accessible to you matched this topic, so no report content was generated."],
                    "method": self._method(), "llm_usage": usage_dict(self.meter)}
        doc_filter = SearchFilters(collection_ids=collection_ids, document_ids=selected)

        # 2. sections ------------------------------------------------------------------------------------------------
        sections: list[dict] = []
        for spec in SECTIONS:
            await progress(spec["id"], spec["title"])
            blocks: list[dict] = []
            if spec["kind"] in ("claims", "gaps"):
                plan = RetrievalPlan(name=f"report_{spec['id']}", mode="hybrid", top_k=10, candidates=40, rerank=True, graph=include_graph and spec["id"] == "methodologies")
                evidence = await self._retrieve(state_base, spec["retrieval"].format(t=topic), plan, doc_filter)
                passages, _ = analytical.build_passages(evidence, self.deps.settings.answer_passages)
                for ps in passages:
                    pool.setdefault(ps["chunk_id"], ps)   # the pool keeps FULL passages for tables/gaps/contradictions
                if spec["kind"] == "claims" and self.deps.llm.name == "mock-llm":
                    passages = [{**ps, "id": i} for i, ps in enumerate(focus_passages(passages, spec["id"]), start=1)]
                if spec["kind"] == "claims" and passages:
                    question = f"{spec['title']} of {topic}: {spec['keywords']}"
                    st = {**state_base, "query": question, "standalone": question, "passages": passages}
                    claims = await self._claims(st)
                    ver = await verification.verify_facts(self.deps, {**st, "claims": claims})
                    self._add_usage(ver.get("llm_usage"))
                    status_by = {r["index"]: r["status"] for r in ver["verification"]["claims"]}
                    removed += [{**r, "section": spec["id"]} for r in ver["verification"]["claims"] if r["status"] == "unsupported"]
                    kept, _, _ = verification.assemble_citations(claims, {p["id"]: p for p in passages}, status_by)
                    by_id = {p["id"]: p for p in passages}
                    for c, status in kept:
                        key = " ".join(c["text"].lower().split())
                        if key in used_claims:
                            continue
                        used_claims.add(key)
                        ids = [reg.cite(by_id[pid], best_sentence(c["text"], by_id[pid]["text"])[1]) for pid in c["citations"] if pid in by_id]
                        blocks.append({"type": "claim", "text": c["text"], "citations": ids, "status": status})
            sections.append({"id": spec["id"], "title": spec["title"], "blocks": blocks, "_spec": spec})

        # 3. sections built from verbatim sentences across everything retrieved --------------------------------------
        glob = list(pool.values())
        for i, ps in enumerate(glob, start=1):
            ps["gid"] = i
        gpass = [{**ps, "id": ps["gid"]} for ps in glob]
        by_gid = {ps["id"]: ps for ps in gpass}
        analysis = analytical.analyze_passages(topic, gpass, deep=False) if gpass else {}
        for sec in sections:
            kind = sec["_spec"]["kind"]
            if kind == "results":
                rows, seen = [], set()
                for f in analysis.get("quantitative_findings", []):
                    ps = by_gid[f["passage"]]
                    if f["sentence"] in seen:
                        continue
                    seen.add(f["sentence"])
                    cid = reg.cite(ps, f["sentence"])
                    rows.append({"cells": [ps["title"], f["sentence"], ", ".join(f["numbers"]), str(ps["page_start"] or "-")], "citations": [cid]})
                if rows:
                    sec["blocks"] = [{"type": "table", "columns": ["Document", "Reported result (verbatim)", "Numbers", "Page"], "rows": rows[:MAX_ROWS]},
                                     {"type": "note", "text": "Results are quoted as written. Values from different datasets, metrics or settings are not normalised and "
                                                              "may not be comparable."}]
            elif kind == "contradictions":
                items = []
                for c in analysis.get("contradictions", []):
                    pa, pb = by_gid[c["a"]["passage"]], by_gid[c["b"]["passage"]]
                    ca, cb = reg.cite(pa, c["a"]["sentence"]), reg.cite(pb, c["b"]["sentence"])
                    items.append({"text": f"[{c['type']} candidate] \"{c['a']['sentence']}\" ({pa['title']}) vs \"{c['b']['sentence']}\" ({pb['title']}). "
                                          f"Shared terms: {', '.join(c['shared_terms'])}.", "citations": [ca, cb]})
                if items:
                    sec["blocks"] = [{"type": "list", "items": items},
                                     {"type": "note", "text": "Heuristic candidates only: the sources may describe different models, datasets or settings. Verify manually."}]
            elif kind == "gaps":
                items, seen = [], set()
                for ps in gpass:
                    for s in split_sentences(ps["text"]):
                        if GAP_CUE.search(s) and s not in seen and len(items) < MAX_GAPS:
                            seen.add(s)
                            items.append({"text": s, "citations": [reg.cite(ps, s)]})
                if items:
                    sec["blocks"] = [{"type": "list", "items": items},
                                     {"type": "note", "text": "These gaps are stated by the sources themselves; NEXUS does not infer additional ones."}]
            if not sec["blocks"]:
                sec["blocks"] = [{"type": "note", "text": "No supporting evidence was found in the selected documents for this section."}]

        # 4. final numeric guard: every number in a cited block must occur in its cited sources -------------------------
        dropped: list[dict] = []

        def _ok(text: str, cites: list[int]) -> bool:
            n = numbers(text)
            return not n or n <= reg.source_numbers(cites)

        for sec in sections:
            clean = []
            for b in sec["blocks"]:
                if b["type"] == "claim" and not _ok(b["text"], b["citations"]):
                    dropped.append({"section": sec["id"], "text": b["text"], "reason": "number_not_in_cited_sources"})
                    continue
                if b["type"] == "table":
                    kept_rows = [r for r in b["rows"] if _ok(" ".join(r["cells"][1:3]), r["citations"])]
                    dropped += [{"section": sec["id"], "text": r["cells"][1], "reason": "number_not_in_cited_sources"} for r in b["rows"] if r not in kept_rows]
                    b = {**b, "rows": kept_rows}
                clean.append(b)
            sec["blocks"] = clean or [{"type": "note", "text": "No supporting evidence was found in the selected documents for this section."}]
            sec.pop("_spec", None)

        # 5. executive summary (existing verified items only) + coverage notes -----------------------------------------
        summary = []
        for sid in ("methodologies", "datasets", "limitations", "gaps"):
            sec = next(s for s in sections if s["id"] == sid)
            for b in sec["blocks"]:
                item = b if b["type"] == "claim" else (b["items"][0] if b["type"] == "list" and b["items"] else None)
                if item:
                    summary.append({"text": item["text"], "citations": item["citations"], "section": sid})
                    break
        notes = [f"{len(documents)} document(s) analysed from {len({s['document_id'] for s in gpass})} with retrieved evidence."]
        with_numbers = {by_gid[f["passage"]]["document_id"] for f in analysis.get("quantitative_findings", [])}
        notes += [f"No quantitative result sentence was extracted from: {d['title']}." for d in documents if d["document_id"] not in with_numbers]
        notes += [f"Section '{s['title']}' has no supporting evidence." for s in sections if s["blocks"] and s["blocks"][0]["type"] == "note"
                  and s["blocks"][0]["text"].startswith("No supporting")]
        total = sum(1 for s in sections for b in s["blocks"] if b["type"] == "claim")
        return {**base, "executive_summary": summary, "sections": sections, "citations": reg.export(),
                "verification": {"claims_total": total + len(removed) + len(dropped), "claims_kept": total, "unsupported_removed": removed,
                                 "dropped_at_final_check": dropped},
                "coverage_notes": notes, "method": self._method(), "llm_usage": usage_dict(self.meter)}

    def _method(self) -> dict:
        mock = self.deps.llm.name == "mock-llm"
        return {"statement": "Every cited statement is either a claim verified against the passages it cites (content coverage, exact numbers) or a "
                             "verbatim source sentence. Contradiction and gap sections are heuristic and flagged. Nothing is added from model memory.",
                "llm": self.deps.llm.name, "reranker": self.deps.pipeline.reranker.name,
                "warning": "Generated in offline mock mode: the language model only extracts sentences, so this report does not reflect real "
                           "model quality." if mock else None}
