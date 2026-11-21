"""Agent 7 — Report generation: drafts grounded claims (each with passage citations) from the sanitised passages.
The draft is NOT shown to anyone: verification and citation validation decide what survives into the answer."""
from __future__ import annotations

from app.agents.deps import AgentDeps, usage_dict
from app.agents.textutil import extractive_claims
from app.core.logging import get_logger
from app.llm import prompts
from app.llm.base import UsageMeter
from app.llm.jsonutil import parse_json_object

log = get_logger("nexus.agents.report")
MAX_CLAIMS, MAX_CLAIM_LEN = 8, 600


def clean_claims(raw, valid_ids: set[int]) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
            continue
        cites = c.get("citations")
        ids = sorted({i for i in cites if isinstance(i, int) and not isinstance(i, bool) and i in valid_ids}) if isinstance(cites, list) else []
        out.append({"text": " ".join(c["text"].split())[:MAX_CLAIM_LEN], "citations": ids})
        if len(out) >= MAX_CLAIMS:
            break
    return out


async def generate(deps: AgentDeps, state: dict) -> dict:
    passages = state.get("passages", [])
    if not passages:
        return {"claims": [], "answer_status": "no_evidence", "_trace": {"claims": 0, "source": "none"}}
    meter, source = UsageMeter(), "llm"
    question = state.get("standalone") or state["query"]
    try:
        r = await deps.llm.complete(prompts.ANSWER, prompts.answer_user(question, passages, state.get("preferences")), max_tokens=900)
        meter.add("answer", r)
        obj = parse_json_object(r.text)
        claims = clean_claims(obj.get("claims"), {p["id"] for p in passages}) if obj and "claims" in obj else None
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or type(exc).__module__.startswith("httpx"):
            raise  # transient → LangGraph RetryPolicy
        log.warning("answer generation failed", extra={"extra_fields": {"error": str(exc)[:200]}})
        claims = None
    if claims is None:  # unusable model output → deterministic extractive fallback (still verbatim + cited)
        claims, source = extractive_claims(question, passages), "extractive_fallback"
    return {"claims": claims, "llm_usage": usage_dict(meter), "_trace": {"claims": len(claims), "source": source}}
