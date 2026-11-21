"""Agent 1 — Orchestrator: understands intent, classifies the query, builds the execution plan, selects specialist
agents, and finalises the run (including failure handling)."""
from __future__ import annotations

from dataclasses import asdict

from langgraph.config import get_stream_writer

from app.agents.deps import AgentDeps
from app.retrieval.adaptive import AdaptivePolicy, extract_features

INTENTS = {"lookup": "find_specific_fact", "factoid": "answer_question", "comparison": "compare_sources",
           "multi_hop": "connect_across_documents", "exploratory": "survey_topic"}
ANALYTICAL_CLASSES = {"comparison", "multi_hop", "exploratory"}


async def classify_and_plan(deps: AgentDeps, state: dict) -> dict:
    q = state["query"]
    feats = extract_features(q)
    cls = feats.query_class
    ladder = [asdict(p) for p in AdaptivePolicy().ladder(q)]
    plan = ladder[0]
    agents = ["orchestrator", "research", "retrieval"]
    if plan["graph"] and deps.settings.graph_enabled:
        agents.append("knowledge_graph")
    if cls in ANALYTICAL_CLASSES:
        agents.append("analytical")
    agents += ["report", "verification", "quality_control"]
    return {"query_class": cls, "intent": INTENTS[cls], "features": {k: getattr(feats, k) for k in feats.__slots__ if k != "query_class"},
            "ladder": ladder, "level": 0, "plan": plan, "selected_agents": agents, "loop": 0, "status": "running",
            "_trace": {"query_class": cls, "plan": plan["name"], "agents": agents}}


async def finalize(deps: AgentDeps, state: dict) -> dict:
    status = "degraded" if state.get("errors") and not state.get("answer") else "completed"
    answer = state.get("answer") or "The workflow could not produce an answer."
    writer = get_stream_writer()  # no-op unless the caller streams
    words = answer.split()
    n = max(1, deps.settings.stream_chunk_words)
    for i in range(0, len(words), n):  # only the VERIFIED answer is ever streamed
        writer({"type": "delta", "text": " ".join(words[i : i + n]) + (" " if i + n < len(words) else "")})
    return {"status": status, "answer": answer, "_trace": {"status": status}}
