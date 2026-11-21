"""The explicit LangGraph StateGraph for NEXUS research.

START → classify → research ─┬→ retrieve_branch × N (parallel, Send) ─┐
                             └→ graph_branch (parallel, if planned) ──┴→ fuse_rerank → evaluate_evidence
evaluate_evidence ─ insufficient & loops left → rewrite ─┐ (re-dispatches branches)
                  └ otherwise → analyze → generate → verify_facts → validate_citations → quality
quality ─ retrieve_more (loops left) → rewrite | human_review → finalize | done → finalize → END

Loops are bounded by `settings.max_retrieval_loops`, enforced in the routing functions (evidence AND quality share one
counter). Retrieval/graph/LLM nodes have a RetryPolicy for transient network errors."""
from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send

from app.agents import analytical, human, orchestrator, quality, report, research, retrieval, verification
from app.agents.common import TRANSIENT, traced
from app.agents.deps import AgentDeps
from app.agents.state import ResearchState


def build_research_graph(deps: AgentDeps, checkpointer=None):
    g = StateGraph(ResearchState)
    retry = RetryPolicy(max_attempts=3, initial_interval=0.2, backoff_factor=2.0, max_interval=2.0, retry_on=TRANSIENT)

    def node(name: str, agent: str, fn, *, retry_policy: RetryPolicy | None = None) -> None:
        g.add_node(name, traced(agent, name)(functools.partial(fn, deps)), **({"retry_policy": retry_policy} if retry_policy else {}))

    node("classify", "orchestrator", orchestrator.classify_and_plan)
    node("research", "research", research.research, retry_policy=retry)
    node("rewrite", "research", research.rewrite_and_escalate, retry_policy=retry)
    node("retrieve_branch", "retrieval", retrieval.retrieve_branch, retry_policy=retry)
    node("graph_branch", "knowledge_graph", retrieval.graph_branch, retry_policy=retry)
    node("fuse_rerank", "retrieval", retrieval.fuse_rerank)
    node("evaluate_evidence", "quality_control", quality.evaluate_evidence)
    node("analyze", "analytical", analytical.analyze)
    node("generate", "report", report.generate, retry_policy=retry)
    node("verify_facts", "verification", verification.verify_facts)
    node("validate_citations", "verification", verification.validate_citations)
    node("quality", "quality_control", quality.quality_evaluation)
    node("human_review", "human", human.human_review)
    node("finalize", "orchestrator", orchestrator.finalize)

    def dispatch(state: dict) -> list[Send]:
        base = {k: state.get(k) for k in ("principal", "plan", "collection_ids", "document_ids", "hyde_text", "loop", "standalone")}
        sends = [Send("retrieve_branch", {**base, "sub_query": q, "branch": f"q{i}"}) for i, q in enumerate(state.get("sub_queries") or [state["query"]])]
        if (state.get("plan") or {}).get("graph") and deps.settings.graph_enabled:
            sends.append(Send("graph_branch", base))
        return sends

    def after_evidence(state: dict) -> str:
        if not state.get("sufficient") and state.get("loop", 0) < deps.settings.max_retrieval_loops:
            return "rewrite"
        return "analyze"

    def after_quality(state: dict) -> str:
        decision = state.get("decision", "done")
        if decision == "retrieve_more" and state.get("loop", 0) >= deps.settings.max_retrieval_loops:
            decision = "done"  # defence in depth: never exceed the loop cap even if a node mis-decides
        return {"retrieve_more": "rewrite", "human_review": "human_review"}.get(decision, "finalize")

    g.add_edge(START, "classify")
    g.add_edge("classify", "research")
    g.add_conditional_edges("research", dispatch, ["retrieve_branch", "graph_branch"])
    g.add_conditional_edges("rewrite", dispatch, ["retrieve_branch", "graph_branch"])
    g.add_edge("retrieve_branch", "fuse_rerank")
    g.add_edge("graph_branch", "fuse_rerank")
    g.add_edge("fuse_rerank", "evaluate_evidence")
    g.add_conditional_edges("evaluate_evidence", after_evidence, ["rewrite", "analyze"])
    g.add_edge("analyze", "generate")
    g.add_edge("generate", "verify_facts")
    g.add_edge("verify_facts", "validate_citations")
    g.add_edge("validate_citations", "quality")
    g.add_conditional_edges("quality", after_quality, ["rewrite", "human_review", "finalize"])
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
