from __future__ import annotations

import operator
from dataclasses import asdict
from typing import Annotated, Any, TypedDict

from app.retrieval.types import Hit


def merge_usage(a: dict | None, b: dict | None) -> dict:
    a, b = dict(a or {}), dict(b or {})
    out = {k: a.get(k, 0) + b.get(k, 0) for k in ("calls", "prompt_tokens", "completion_tokens")}
    by: dict[str, int] = dict(a.get("by_task", {}))
    for k, v in b.get("by_task", {}).items():
        by[k] = by.get(k, 0) + v
    out["by_task"] = by
    return out


class ResearchState(TypedDict, total=False):
    # inputs
    run_id: str
    principal: dict            # {user_id, org_id, role, email}; re-validated against the DB by every retrieval node
    query: str
    history: list              # [[role, text], ...]
    collection_ids: list | None
    document_ids: list | None   # research-context scoping
    preferences: list           # long-term user preferences (style only)
    review_mode: str           # auto | always | never
    # orchestrator
    query_class: str
    intent: str
    features: dict
    ladder: list
    level: int
    plan: dict
    selected_agents: list
    loop: int
    # research
    standalone: str
    questions: list            # the (decomposed) questions the answer must cover
    sub_queries: list          # questions + query variants actually sent to retrieval
    hyde_text: str | None
    # retrieval / graph
    branches: Annotated[list, operator.add]
    evidence: list
    graph: dict | None
    confidence: dict
    sufficient: bool
    # analysis / generation / verification
    passages: list
    injection_flags: list
    analysis: dict
    claims: list
    verification: dict
    answer: str
    answer_status: str
    citations: list
    # quality / review
    quality: dict
    decision: str
    review: dict | None
    # bookkeeping
    status: str
    errors: Annotated[list, operator.add]
    trace: Annotated[list, operator.add]
    llm_usage: Annotated[dict, merge_usage]


def hit_dict(h: Hit) -> dict[str, Any]:
    return asdict(h)


def hit_from(d: dict) -> Hit:
    return Hit(**d)
