"""Agent 8 — Quality control: evidence-sufficiency evaluation before analysis, and answer-quality evaluation after
verification. Decides whether to retrieve more (bounded), request human review, or finish. Thresholds are heuristic and
uncalibrated (see docs/research protocol); the decision reasons are recorded so they can be audited."""
from __future__ import annotations

from dataclasses import asdict

from app.agents.deps import AgentDeps
from app.agents.state import hit_from
from app.agents.textutil import content_tokens, coverage
from app.retrieval.confidence import retrieval_confidence

LOW_EVIDENCE_CONFIDENCE = 0.25
LOW_GROUNDEDNESS = 0.6
MIN_COMPLETENESS = 0.5


async def evaluate_evidence(deps: AgentDeps, state: dict) -> dict:
    hits = [hit_from(h) for h in state.get("evidence", [])]
    conf = retrieval_confidence(state.get("standalone") or state["query"], hits, deps.settings.rrf_k)
    sufficient = bool(hits) and conf.score >= deps.settings.evidence_sufficiency_threshold
    return {"confidence": asdict(conf), "sufficient": sufficient,
            "_trace": {"confidence": conf.score, "sufficient": sufficient, "loop": state["loop"]}}


async def quality_evaluation(deps: AgentDeps, state: dict) -> dict:
    s = deps.settings
    claims, ver = state.get("claims", []), state.get("verification", {})
    status_by = {r["index"]: r["status"] for r in ver.get("claims", [])}
    kept = [c for i, c in enumerate(claims) if status_by.get(i) in ("supported", "weak")]
    answer_tokens = set().union(*(content_tokens(c["text"]) for c in kept)) if kept else set()
    questions = state.get("questions") or [state.get("standalone") or state["query"]]
    covered = sum(coverage(content_tokens(q), answer_tokens) >= MIN_COMPLETENESS for q in questions) if kept else 0
    completeness = covered / len(questions)
    titles = " ".join(c["title"] for c in state.get("citations", []))
    relevance = coverage(content_tokens(state.get("standalone") or state["query"]), answer_tokens | content_tokens(titles)) if kept else 0.0
    supported = ver.get("supported", 0)
    groundedness = supported / len(claims) if claims else 0.0
    ev_conf = state.get("confidence", {}).get("score", 0.0)
    contradictions = state.get("analysis", {}).get("contradictions", [])
    no_answer = state.get("answer_status") in ("insufficient_evidence", "no_evidence")
    loops_left = state.get("loop", 0) < s.max_retrieval_loops
    mode = state.get("review_mode", s.human_review_mode)
    reasons: list[str] = []

    if (no_answer or completeness < MIN_COMPLETENESS) and loops_left:
        decision = "retrieve_more"
        reasons.append("no_supported_answer" if no_answer else f"completeness_{completeness:.2f}_below_{MIN_COMPLETENESS}")
    else:
        if contradictions:
            reasons.append(f"{len(contradictions)}_possible_contradictions")
        if claims and groundedness < LOW_GROUNDEDNESS:
            reasons.append(f"groundedness_{groundedness:.2f}")
        if kept and ev_conf < LOW_EVIDENCE_CONFIDENCE:
            reasons.append(f"low_evidence_confidence_{ev_conf:.2f}")
        if mode == "always":
            reasons.append("review_mode_always")
        need = mode == "always" or (mode == "auto" and bool(reasons))
        decision = "human_review" if need else "done"
    quality = {"relevance": round(relevance, 3), "completeness": round(completeness, 3), "groundedness": round(groundedness, 3),
               "citation_coverage": ver.get("citation_coverage", 0.0), "evidence_confidence": ev_conf,
               "contradictions": len(contradictions), "loop": state.get("loop", 0), "reasons": reasons}
    return {"quality": quality, "decision": decision, "_trace": {"decision": decision, "reasons": reasons}}
