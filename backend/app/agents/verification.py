"""Agent 6 — Verification: fact verification of every claim against the passages it cites, then citation validation and
assembly of the final answer. A claim survives only if (a) it cites existing passages, (b) its content words are covered
by them, (c) every number it states appears in them, and (d) the optional LLM judge agrees. Unsupported claims are
removed; weakly supported ones are explicitly qualified in the answer."""
from __future__ import annotations

from app.agents.deps import AgentDeps, usage_dict
from app.agents.textutil import best_sentence, content_tokens, coverage, numbers
from app.core.logging import get_logger
from app.llm import prompts
from app.llm.base import UsageMeter
from app.llm.jsonutil import parse_json_object

log = get_logger("nexus.agents.verify")
INSUFFICIENT = "I could not find sufficient evidence in the accessible documents to answer this question."


async def verify_facts(deps: AgentDeps, state: dict) -> dict:
    claims, passages = state.get("claims", []), {p["id"]: p for p in state.get("passages", [])}
    s = deps.settings
    results: list[dict] = []
    for i, c in enumerate(claims):
        cited = [passages[x] for x in c["citations"] if x in passages]
        if not cited:
            results.append({"index": i, "text": c["text"], "status": "unsupported", "reason": "no_valid_citation", "coverage": 0.0})
            continue
        have, have_nums = set(), set()
        for p in cited:
            have |= content_tokens(p["text"])
            have_nums |= numbers(p["text"])
        cov = coverage(content_tokens(c["text"]), have)
        nums_ok = numbers(c["text"]) <= have_nums
        if not nums_ok:
            status, reason = "unsupported", "number_not_in_sources"
        elif cov >= s.support_threshold:
            status, reason = "supported", "ok"
        elif cov >= s.weak_support_threshold:
            status, reason = "weak", "partial_coverage"
        else:
            status, reason = "unsupported", "low_coverage"
        results.append({"index": i, "text": c["text"], "status": status, "reason": reason, "coverage": round(cov, 3), "judge": None})

    meter, judge_used = UsageMeter(), False
    judgeable = [r for r in results if r["status"] != "unsupported"]
    if s.verify_with_llm and judgeable:
        try:
            cited_ids = sorted({x for r in judgeable for x in claims[r["index"]]["citations"] if x in passages})
            r = await deps.llm.complete(prompts.VERIFY, prompts.verify_user(claims, {i: passages[i]["text"] for i in cited_ids}), max_tokens=400)
            meter.add("verify", r)
            obj = parse_json_object(r.text)
            verdicts = {v["claim"]: v["supported"] for v in (obj or {}).get("verdicts", [])
                        if isinstance(v, dict) and isinstance(v.get("claim"), int) and isinstance(v.get("supported"), bool)}
            judge_used = bool(verdicts)
            for res in judgeable:
                if res["index"] in verdicts:
                    res["judge"] = verdicts[res["index"]]
                    if verdicts[res["index"]] is False:  # the judge can only downgrade, never upgrade
                        res["status"], res["reason"] = "unsupported", "judge_rejected"
        except Exception as exc:  # noqa: BLE001 - lexical gate alone still protects the answer
            log.warning("llm judge unavailable", extra={"extra_fields": {"error": str(exc)[:200]}})
    summary = {k: sum(r["status"] == k for r in results) for k in ("supported", "weak", "unsupported")}
    return {"verification": {"claims": results, **summary, "judge_used": judge_used}, "llm_usage": usage_dict(meter),
            "_trace": {**summary, "judge_used": judge_used}}


def assemble_citations(claims: list[dict], passages: dict[int, dict], status_by: dict[int, str]):
    """Shared by chat answers and reports. Returns (kept [(claim, status)], order {passage_id: n}, citations [...]).
    Only supported/weak claims survive; citation numbers follow first appearance; each citation carries a verbatim quote."""
    kept = [(c, status_by[i]) for i, c in enumerate(claims) if status_by.get(i) in ("supported", "weak")]
    order: dict[int, int] = {}
    for c, _ in kept:
        for pid in c["citations"]:
            if pid in passages:
                order.setdefault(pid, len(order) + 1)
    citations = []
    for pid, n in order.items():
        p = passages[pid]
        quotes: list[str] = []
        for c, _ in kept:
            if pid in c["citations"]:
                q = best_sentence(c["text"], p["text"])[1]
                if q and q not in quotes:
                    quotes.append(q)
        citations.append({"id": n, "chunk_id": p["chunk_id"], "document_id": p["document_id"], "title": p["title"], "filename": p["filename"],
                          "page_start": p["page_start"], "page_end": p["page_end"], "section": p["section"], "version": p["version"],
                          "is_current": p["is_current"], "quote": " ".join(quotes[:2]), "quotes": quotes[:3]})
    return kept, order, citations


async def validate_citations(deps: AgentDeps, state: dict) -> dict:
    claims, passages = state.get("claims", []), {p["id"]: p for p in state.get("passages", [])}
    ver = state.get("verification", {"claims": []})
    status_by = {r["index"]: r["status"] for r in ver["claims"]}
    kept, order, citations = assemble_citations(claims, passages, status_by)
    parts = []
    for c, st in kept:
        marks = "".join(f"[{order[x]}]" for x in c["citations"] if x in order)
        parts.append(f"{c['text'].rstrip()} {marks}" + (" (limited support)" if st == "weak" else ""))
    floor = deps.settings.abstain_confidence_threshold
    low_conf = floor > 0 and state.get("confidence", {}).get("score", 1.0) < floor   # calibrated abstention gate
    if not kept or low_conf:
        kept = []
        if low_conf:
            citations = []   # abstaining: nothing is presented as support
        answer, answer_status = INSUFFICIENT, "insufficient_evidence"
    else:
        n_weak = sum(st == "weak" for _, st in kept)
        answer = " ".join(parts)
        answer_status = "grounded" if len(kept) == len(claims) and not n_weak else "partial"
    cited_ok = sum(1 for c in claims if any(x in passages for x in c["citations"]))
    metrics = {"claims_total": len(claims), "claims_kept": len(kept), "citation_coverage": round(cited_ok / len(claims), 3) if claims else 0.0,
               "unsupported_removed": [r for r in ver["claims"] if r["status"] == "unsupported"]}
    return {"answer": answer, "answer_status": answer_status, "citations": citations,
            "verification": {**ver, **metrics}, "_trace": {"kept": len(kept), "citations": len(citations), "status": answer_status}}
