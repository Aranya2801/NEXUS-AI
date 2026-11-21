"""Autonomous run loop: plan -> execute (parallel within dependency layers) -> observe -> re-plan, under hard budgets (waves, steps, wall
clock). The final answer reuses the existing grounded pipeline (sanitised passages -> claims -> claim verification -> citation validation),
so an autonomous answer is held to the same evidence standard as /chat. Numbers produced by calculator/python are reported separately,
labelled as computed (they are not citations)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from app.agentic.planner import Planner, Step
from app.agentic.tools import ToolContext, ToolRegistry, ToolResult
from app.agents.analytical import build_passages
from app.agents.deps import AgentDeps, usage_dict
from app.agents.report import generate
from app.agents.verification import INSUFFICIENT, validate_citations, verify_facts
from app.llm.base import UsageMeter


def resolve(v, results: dict[str, ToolResult]):
    if isinstance(v, dict):
        if set(v) == {"$ref"}:
            sid, _, path = str(v["$ref"]).partition(".")
            if sid not in results or not results[sid].ok:
                raise ValueError(f"reference to unavailable step {sid}")
            cur = results[sid].output
            for tok in [t for t in path.split(".") if t]:
                try:
                    cur = cur[int(tok)] if isinstance(cur, list) else cur[tok]
                except (KeyError, IndexError, ValueError, TypeError) as exc:
                    raise ValueError(f"unresolved reference {v['$ref']}") from exc
            return cur
        return {k: resolve(x, results) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, results) for x in v]
    return v


def _summary(res: ToolResult) -> str:
    if not res.ok:
        return ""
    o = res.output
    if "hits" in o:
        return "; ".join(f"{h['title']}: {h['text'][:80]}" for h in o["hits"][:3])
    return json.dumps(o, default=str)[:300]


class AutonomousAgent:
    def __init__(self, deps: AgentDeps, registry: ToolRegistry, planner: Planner | None = None) -> None:
        self.deps, self.registry = deps, registry
        self.planner = planner or Planner(deps.llm, registry, deps.settings)

    async def run(self, goal: str, ctx: ToolContext, role: str, *, max_steps: int | None = None) -> dict:
        s, meter = self.deps.settings, UsageMeter()
        max_steps = min(max_steps or s.agentic_max_steps, s.agentic_max_steps)
        deadline = time.monotonic() + s.agentic_run_timeout_seconds
        visible = self.registry.visible(role, s, ctx.allow_code)
        results: dict[str, ToolResult] = {}
        history: list[dict] = []
        trace: list[dict] = []
        evidence: dict[str, dict] = {}
        planners: list[str] = []
        stopped = "planner_done"
        wave = 0
        while wave < s.agentic_max_waves:
            room = max_steps - len(results)
            if room <= 0:
                stopped = "step_budget"
                break
            if time.monotonic() > deadline:
                stopped = "time_budget"
                break
            steps, who = await self.planner.next_steps(goal, history, visible, set(results), room, wave, meter)
            planners.append(who)
            if not steps:
                break
            await self._execute(steps, ctx, role, results, history, trace, evidence, deadline)
            wave += 1
        else:
            stopped = "wave_budget"
        out = await self._synthesise(goal, results, history, evidence, meter)
        return {**out, "steps": trace, "planner": planners, "stopped_reason": stopped, "budget": {"steps_used": len(results), "max_steps": max_steps,
                "waves_used": wave, "max_waves": s.agentic_max_waves}, "llm_usage": usage_dict(meter)}

    async def _execute(self, steps: list[Step], ctx, role, results, history, trace, evidence, deadline) -> None:
        pending = {st.id: st for st in steps}
        while pending:
            ready = [st for st in pending.values() if all(d in results for d in st.depends_on)]
            if not ready:
                for st in list(pending.values()):
                    results[st.id] = ToolResult(False, {}, "dependency cycle or missing dependency")
                    self._record(st, results[st.id], history, trace, evidence, {})
                    pending.pop(st.id)
                return
            outs = await asyncio.gather(*[self._one(st, ctx, role, results, deadline) for st in ready])   # independent steps run in parallel
            for st, (res, args) in zip(ready, outs):
                results[st.id] = res
                self._record(st, res, history, trace, evidence, args)
                pending.pop(st.id)

    async def _one(self, st: Step, ctx, role, results, deadline) -> tuple[ToolResult, dict]:
        if any(not results[d].ok for d in st.depends_on):
            return ToolResult(False, {}, "skipped: a dependency failed"), st.args
        if time.monotonic() > deadline:
            return ToolResult(False, {}, "skipped: run time budget exhausted"), st.args
        try:
            args = resolve(st.args, results)
        except ValueError as exc:
            return ToolResult(False, {}, str(exc)), st.args
        return await self.registry.execute(ctx, role, st.tool, args), args

    @staticmethod
    def _record(st: Step, res: ToolResult, history, trace, evidence, args: dict) -> None:
        for h in res.evidence:
            evidence.setdefault(h["chunk_id"], h)
        shown = {k: (f"<{len(v)} chars sha256:{hashlib.sha256(v.encode()).hexdigest()[:12]}>" if k == "code" and isinstance(v, str) else v) for k, v in args.items()}
        hits = res.output.get("hits", []) if res.ok else []
        history.append({"id": st.id, "tool": st.tool, "args": shown, "ok": res.ok, "error": res.error, "summary": _summary(res),
                        "chunk_ids": [h["chunk_id"] for h in hits], "confidence": res.output.get("confidence", 0.0) if res.ok else 0.0,
                        "top_document": hits[0]["document_id"] if hits else None, "output": res.output if res.ok else {}})
        trace.append({"id": st.id, "wave": st.wave, "tool": st.tool, "purpose": st.purpose, "args": shown, "depends_on": st.depends_on, "ok": res.ok,
                      "error": res.error, "ms": res.ms, "summary": _summary(res)[:200]})

    async def _synthesise(self, goal: str, results, history, evidence, meter: UsageMeter) -> dict:
        computations = []
        for h in history:
            if h["ok"] and h["tool"] == "calculator" and "result" in h["output"]:
                computations.append({"tool": "calculator", "expression": h["args"].get("expression"), "result": h["output"]["result"]})
            elif h["ok"] and h["tool"] == "python" and h["output"].get("ok"):
                computations.append({"tool": "python", "code": h["args"].get("code"), "stdout": h["output"].get("stdout", "")[:1000]})
        # evidence order = best-first by score, deterministic tie-break on chunk id
        hits = sorted(evidence.values(), key=lambda h: (-h.get("score", 0.0), h["chunk_id"]))
        passages, flags = build_passages(hits, 8)
        answer, status, citations, verification = INSUFFICIENT, "insufficient_evidence", [], {}
        if passages:
            conf = max((h.get("confidence", 0.0) for h in history if h["tool"] == "search_documents"), default=1.0)
            state = {"query": goal, "passages": passages, "confidence": {"score": conf}}
            gen = await generate(self.deps, state)
            state.update(claims=gen["claims"])
            if gen.get("llm_usage"):
                meter.calls += gen["llm_usage"]["calls"]
                meter.prompt_tokens += gen["llm_usage"]["prompt_tokens"]
                meter.completion_tokens += gen["llm_usage"]["completion_tokens"]
            ver = await verify_facts(self.deps, state)
            state.update(verification=ver["verification"])
            fin = await validate_citations(self.deps, state)
            answer, status, citations, verification = fin["answer"], fin["answer_status"], fin["citations"], fin["verification"]
        if computations:
            lines = [f"{c['expression']} = {c['result']}" if c["tool"] == "calculator" else f"python output: {c['stdout'].strip()[:300]}" for c in computations]
            note = "Computed by tools (not from documents): " + "; ".join(lines)
            answer = note if status == "insufficient_evidence" else f"{answer}\n\n{note}"
            status = "computed_only" if status == "insufficient_evidence" else status
        return {"answer": answer, "answer_status": status, "citations": citations, "verification": verification, "computations": computations,
                "injection_flags": flags}
