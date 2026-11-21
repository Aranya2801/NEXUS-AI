"""Human-in-the-loop: pauses the workflow with `interrupt()`. The full state is checkpointed, so the run can be resumed
by another process (or after a restart) via POST /agents/runs/{id}/resume. The node does nothing before `interrupt`
(it is re-executed from the top on resume), so it is idempotent."""
from __future__ import annotations

from langgraph.types import interrupt

from app.agents.deps import AgentDeps

MAX_EDIT_LEN = 8000


async def human_review(deps: AgentDeps, state: dict) -> dict:
    q = state.get("quality", {})
    decision = interrupt({
        "run_id": state["run_id"], "query": state["query"], "answer": state.get("answer", ""),
        "answer_status": state.get("answer_status"), "reasons": q.get("reasons", []), "quality": q,
        "citations": [{"id": c["id"], "title": c["title"], "page_start": c["page_start"], "quote": c["quote"]} for c in state.get("citations", [])],
        "unsupported": [r["text"] for r in state.get("verification", {}).get("unsupported_removed", [])],
        "contradictions": state.get("analysis", {}).get("contradictions", []),
        "options": ["approve", "edit", "reject"],
    })
    d = decision if isinstance(decision, dict) else {}
    action = d.get("decision") if d.get("decision") in ("approve", "edit", "reject") else "reject"  # unknown input fails closed
    note = str(d.get("note") or "")[:500]
    review = {"decision": action, "reviewer": d.get("reviewer"), "note": note}
    if action == "approve":
        return {"review": review, "_trace": {"decision": action}}
    if action == "edit" and isinstance(d.get("answer"), str) and d["answer"].strip():
        return {"review": review, "answer": d["answer"].strip()[:MAX_EDIT_LEN], "answer_status": "reviewer_edited",
                "_trace": {"decision": action}}
    return {"review": {**review, "decision": "reject"}, "answer": "This answer was withheld by a reviewer." + (f" Note: {note}" if note else ""),
            "answer_status": "rejected_by_reviewer", "citations": [], "_trace": {"decision": "reject"}}
