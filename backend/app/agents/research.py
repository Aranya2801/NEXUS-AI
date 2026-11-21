"""Agent 2 — Research: contextual rewrite of follow-ups, decomposition into sub-questions, query variants, HyDE; and,
when evidence is insufficient, plan escalation + query rewriting for the next retrieval loop."""
from __future__ import annotations

from app.agents.deps import AgentDeps, usage_dict
from app.llm.base import UsageMeter

MAX_QUERIES = 8


async def research(deps: AgentDeps, state: dict) -> dict:
    meter, plan, tr = UsageMeter(), state["plan"], None
    tr = deps.transformer(meter)
    standalone = await tr.rewrite(state["query"], [tuple(x) for x in state.get("history", [])])
    queries = [standalone]
    if plan["decompose"]:
        queries = await tr.decompose(standalone)
    questions = list(queries)
    if plan["multi_query"]:
        for base in queries[:2]:
            queries += await tr.multi_query(base, plan["multi_query"])
    queries = list(dict.fromkeys(queries))[:MAX_QUERIES]
    hyde = await tr.hyde(standalone) if plan["hyde"] else None
    return {"standalone": standalone, "questions": questions, "sub_queries": queries, "hyde_text": hyde, "llm_usage": usage_dict(meter),
            "_trace": {"sub_queries": queries, "hyde": bool(hyde), "rewritten": standalone != state["query"]}}


async def rewrite_and_escalate(deps: AgentDeps, state: dict) -> dict:
    """Insufficient evidence → climb the adaptive ladder one level and widen the query set. Bounded by `max_retrieval_loops`
    in the routing functions, so this node can never be re-entered indefinitely."""
    meter = UsageMeter()
    tr = deps.transformer(meter)
    ladder, level = state["ladder"], min(state["level"] + 1, len(state["ladder"]) - 1)
    plan = dict(ladder[level])
    if deps.settings.graph_enabled:
        plan["graph"] = True  # dynamic agent selection: bring the knowledge-graph agent in on retry
    base = state.get("standalone") or state["query"]
    questions = list(state.get("questions") or [base])
    extra = await tr.multi_query(base, 3)
    if len(questions) == 1 and state["query_class"] in ("comparison", "multi_hop", "exploratory"):
        decomposed = await tr.decompose(base)
        if len(decomposed) > 1:
            questions = decomposed
            extra += decomposed
    queries = list(dict.fromkeys([*state.get("sub_queries", [base]), *extra]))[:MAX_QUERIES]
    agents = list(state.get("selected_agents", []))
    if plan["graph"] and "knowledge_graph" not in agents:
        agents.insert(3, "knowledge_graph")
    return {"level": level, "plan": plan, "questions": questions, "sub_queries": queries, "loop": state["loop"] + 1, "selected_agents": agents,
            "llm_usage": usage_dict(meter),
            "_trace": {"escalated_to": plan["name"], "level": level, "loop": state["loop"] + 1, "sub_queries": queries}}
