"""LLM-based query transformations. All outputs are validated and every failure degrades to the original query,
so a flaky or malicious LLM response can reduce recall but can never break or redirect retrieval."""
from __future__ import annotations

from app.core.logging import get_logger
from app.llm import prompts
from app.llm.base import LLM, UsageMeter
from app.llm.jsonutil import parse_json_object

log = get_logger("nexus.transform")
MAX_QUERY_LEN = 300


class QueryTransformer:
    def __init__(self, llm: LLM, meter: UsageMeter | None = None) -> None:
        self.llm, self.meter = llm, meter or UsageMeter()

    async def _ask(self, task: str, system: str, user: str, max_tokens: int = 400) -> dict | None:
        try:
            r = await self.llm.complete(system, user, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - network/provider errors must not fail retrieval
            log.warning("llm call failed", extra={"extra_fields": {"task": task, "error": str(exc)[:200]}})
            return None
        self.meter.add(task, r)
        obj = parse_json_object(r.text)
        if obj is None:
            log.warning("llm returned non-json", extra={"extra_fields": {"task": task}})
        return obj

    @staticmethod
    def _clean_list(raw, limit: int, exclude: str = "") -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for x in raw:
            if isinstance(x, str):
                x = " ".join(x.split())[:MAX_QUERY_LEN]
                if x and x.lower() != exclude.lower() and x.lower() not in {o.lower() for o in out}:
                    out.append(x)
        return out[:limit]

    async def rewrite(self, question: str, history: list[tuple[str, str]]) -> str:
        """Contextual retrieval: make a follow-up question standalone."""
        if not history:
            return question
        obj = await self._ask("rewrite", prompts.REWRITE, prompts.rewrite_user(question, history), 150)
        q = obj.get("query") if obj else None
        return " ".join(q.split())[:MAX_QUERY_LEN] if isinstance(q, str) and q.strip() else question

    async def decompose(self, question: str, max_n: int = 4) -> list[str]:
        obj = await self._ask("decompose", prompts.DECOMPOSE.format(max_n=max_n), question)
        subs = self._clean_list(obj.get("queries") if obj else None, max_n)
        return subs or [question]

    async def multi_query(self, question: str, n: int = 3) -> list[str]:
        obj = await self._ask("multi_query", prompts.MULTI_QUERY.format(n=n), question)
        return self._clean_list(obj.get("queries") if obj else None, n, exclude=question)

    async def hyde(self, question: str) -> str | None:
        obj = await self._ask("hyde", prompts.HYDE, question, 200)
        p = obj.get("passage") if obj else None
        return " ".join(p.split())[:1200] if isinstance(p, str) and p.strip() else None
