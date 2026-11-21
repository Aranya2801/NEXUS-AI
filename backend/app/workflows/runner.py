"""Runs the research graph for a user: persists conversations/runs/messages, streams events, resumes human reviews,
and exposes run inspection. The LangGraph checkpoint (thread_id == run id) is the source of truth for workflow state."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.memory.service import MemoryService
from app.auth.service import audit
from app.core.config import Settings
from app.core.exceptions import Conflict, NotFound, ValidationFailed, WorkflowFailed
from app.core.context import set_prompt_pins
from app.core.logging import get_logger
from app.learning.experiments import pins_of
from app.security import injection, pii
from app.security.policy import PolicyViolation
from app.database.models import AgentRun, Conversation, Message

log = get_logger("nexus.runner")
HISTORY_TURNS = 6


def _evidence_summary(values: dict) -> list[dict]:
    return [{"chunk_id": h["chunk_id"], "document_id": h["document_id"], "title": h["title"], "page_start": h.get("page_start"),
             "section": h.get("section"), "version": h["version"], "score": round(h["score"], 4), "scores": h.get("scores", {}),
             "ranks": h.get("ranks", {}), "snippet": (h.get("compressed_text") or h["text"])[:240]} for h in values.get("evidence", [])]


def build_result(run_id: str, status: str, values: dict, review: dict | None = None) -> dict[str, Any]:
    ver = values.get("verification", {})
    graph = values.get("graph") or {}
    return {
        "run_id": run_id, "status": status, "answer": values.get("answer", ""), "answer_status": values.get("answer_status"),
        "citations": values.get("citations", []),
        "verification": {k: ver.get(k) for k in ("supported", "weak", "unsupported", "claims_total", "claims_kept", "citation_coverage",
                                                  "judge_used", "claims", "unsupported_removed") if k in ver},
        "quality": values.get("quality", {}), "analysis": values.get("analysis", {}), "query_class": values.get("query_class"),
        "intent": values.get("intent"), "plan": values.get("plan"), "queries": values.get("sub_queries", []),
        "confidence": values.get("confidence", {}), "evidence": _evidence_summary(values),
        "graph": {"seeds": graph.get("seeds", []), "edges": len(graph.get("edges", [])), "documents": graph.get("documents", 0)} if graph else None,
        "injection_sanitized": len(values.get("injection_flags", [])), "review": review or values.get("review"),
        "errors": values.get("errors", []), "trace": values.get("trace", []), "llm_usage": values.get("llm_usage", {}),
    }


class ResearchRunner:
    policies = None      # PolicyStore, set by main (Phase 15)
    experiments = None   # ExperimentService, set by main (Phase 17)
    metrics = None

    def __init__(self, graph, session_factory: async_sessionmaker[AsyncSession], settings: Settings, checkpointer=None,
                 memory: MemoryService | None = None) -> None:
        self.graph, self.sf, self.settings, self.checkpointer = graph, session_factory, settings, checkpointer
        self.memory = memory or MemoryService(settings)

    @staticmethod
    def _cfg(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    # ------------------------------------------------------------------ setup
    async def _audit_now(self, p: Principal, action: str, **detail) -> None:
        async with self.sf() as db:
            await audit(db, action, org_id=p.org_id, user_id=p.user_id, **detail)
            await db.commit()
        if self.metrics is not None:
            self.metrics.inc("nexus_policy_blocks_total", "query", detail.get("reason", "other"))

    async def _prepare(self, p: Principal, message: str, conversation_id: str | None, collection_ids: list[str] | None,
                       review: str | None, research_context_id: str | None = None) -> tuple[str, str, dict]:
        policy_events: list[tuple[str, dict]] = []
        if self.policies is not None:
            pol = await self.policies.get(p.org_id)
            if pol.injection_queries != "off" and injection.scan(message):
                if pol.injection_queries == "block":
                    await self._audit_now(p, "policy.query_blocked", reason="injection")
                    raise PolicyViolation("This request looks like an attempt to override the assistant's instructions and was refused by your organisation's policy")
                policy_events.append(("policy.query_injection_flagged", {}))
            if pol.pii_queries != "off":
                found = pii.counts(pii.detect(message, pol.pii_kinds))
                if found and pol.pii_queries == "block":
                    await self._audit_now(p, "policy.query_blocked", reason="pii", kinds=found)
                    raise PolicyViolation(f"This request contains personal data ({', '.join(sorted(found))}) and was refused by your organisation's policy")
                if found:
                    message, _ = pii.redact(message, pol.pii_kinds)
                    policy_events.append(("policy.query_redacted", {"kinds": found}))
        async with self.sf() as db:
            if conversation_id:
                conv = await db.get(Conversation, conversation_id)
                if conv is None or conv.org_id != p.org_id or conv.user_id != p.user_id:
                    raise NotFound("Conversation not found")
            else:
                conv = Conversation(org_id=p.org_id, user_id=p.user_id, title=message.strip()[:80] or "New research session",
                                    collection_ids=collection_ids or [])
                db.add(conv)
                await db.flush()
            prior = (await db.execute(select(Message).where(Message.conversation_id == conv.id).order_by(
                Message.created_at.desc(), Message.id.desc()).limit(HISTORY_TURNS))).scalars().all()
            history = [[m.role, m.content] for m in reversed(prior)]
            db.add(Message(conversation_id=conv.id, org_id=p.org_id, role="user", content=message))
            run = AgentRun(org_id=p.org_id, user_id=p.user_id, conversation_id=conv.id, query=message, status="running")
            db.add(run)
            await db.flush()
            if self.experiments is not None:              # A/B: pin this run's prompt versions; stored so outcomes can be joined and resumes stay consistent
                run.variants = {a.experiment_id: {"name": a.name, "variant": a.variant, "task": a.task, "version": a.version}
                                for a in await self.experiments.assign(p.org_id, run.id, db)}
                set_prompt_pins(pins_of(run.variants))
            await audit(db, "chat.run", org_id=p.org_id, user_id=p.user_id, resource=run.id, conversation=conv.id)
            for action, detail in policy_events:
                await audit(db, action, org_id=p.org_id, user_id=p.user_id, resource=run.id, **detail)
            run_id, conv_id = run.id, conv.id
            preferences = await self.memory.preferences_context(db, p)          # long-term memory (style hints only)
            document_ids = (await self.memory.context_document_ids(db, p, research_context_id) or None) if research_context_id else None
            await db.commit()  # visible to workflow nodes (own sessions) before they start
        mode = "always" if review == "always" else self.settings.human_review_mode  # clients may add review, never remove it
        state = {"run_id": run_id, "principal": {"user_id": p.user_id, "org_id": p.org_id, "role": p.role, "email": p.email},
                 "query": message, "history": history, "collection_ids": collection_ids, "document_ids": document_ids,
                 "preferences": preferences, "review_mode": mode, "status": "running",
                 "branches": [], "errors": [], "trace": [], "llm_usage": {}}
        return run_id, conv_id, state

    # ------------------------------------------------------------- completion
    async def _finish(self, run_id: str) -> dict:
        snap = await self.graph.aget_state(self._cfg(run_id))
        values = dict(snap.values)
        interrupts = [i.value for t in snap.tasks for i in t.interrupts]
        async with self.sf() as db:
            run = await db.get(AgentRun, run_id)
            if snap.next and interrupts:
                run.status, run.review = "needs_review", interrupts[0]
                result = build_result(run_id, "needs_review", values, review={"pending": True, **interrupts[0]})
            else:
                status = values.get("status", "completed")
                result = build_result(run_id, status, values)
                await self._redact_answer(run.org_id, result)
                run.status, run.review, run.result = status, None, result
                db.add(Message(conversation_id=run.conversation_id, org_id=run.org_id, role="assistant", content=result["answer"],
                               citations=result["citations"], verification={"answer_status": result["answer_status"], "quality": result["quality"],
                                                                           "supported": result["verification"].get("supported"),
                                                                           "unsupported": result["verification"].get("unsupported")}))
                if status == "completed":  # memory is written only for answers that were actually delivered
                    owner = Principal(run.user_id, run.org_id, "viewer", "")
                    cited = [c["document_id"] for c in result["citations"]]
                    await self.memory.record_session(db, owner, run.conversation_id, run.query, cited,
                                                     [x["name"] for x in (values.get("graph") or {}).get("seeds", [])], values.get("query_class"))
                    await self.memory.record_document_interactions(db, owner, cited, "cited")
            await db.commit()
            result["conversation_id"] = run.conversation_id
        return result

    async def _redact_answer(self, org_id: str, result: dict) -> None:
        if self.policies is None:
            return
        pol = await self.policies.get(org_id)
        if pol.pii_answers != "redact":
            return
        result["answer"], found = pii.redact(result.get("answer", ""), pol.pii_kinds)
        for c in result.get("citations", []):
            for key in ("quote",):
                if c.get(key):
                    c[key], f2 = pii.redact(c[key], pol.pii_kinds)
                    for k, n in f2.items():
                        found[k] = found.get(k, 0) + n
            if c.get("quotes"):
                qs = []
                for q in c["quotes"]:
                    q2, f3 = pii.redact(q, pol.pii_kinds)
                    qs.append(q2)
                    for k, n in f3.items():
                        found[k] = found.get(k, 0) + n
                c["quotes"] = qs
        if found:
            result["pii_redacted"] = found
            if self.metrics is not None:
                for k, n in found.items():
                    self.metrics.inc("nexus_pii_findings_total", "answer", k, "redacted", by=n)

    async def _fail(self, run_id: str, exc: Exception) -> None:
        log.exception("research run failed", extra={"extra_fields": {"run": run_id}})
        async with self.sf() as db:
            run = await db.get(AgentRun, run_id)
            if run:
                run.status, run.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
                await db.commit()

    # ------------------------------------------------------------------ run
    async def run(self, p: Principal, message: str, *, conversation_id: str | None = None,
                  collection_ids: list[str] | None = None, review: str | None = None, research_context_id: str | None = None) -> dict:
        run_id, _, state = await self._prepare(p, message, conversation_id, collection_ids, review, research_context_id)
        try:
            await self.graph.ainvoke(state, self._cfg(run_id))
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)
            raise WorkflowFailed("The research workflow failed", details={"run_id": run_id}) from exc
        return await self._finish(run_id)

    async def _events(self, run_id: str, input_: Any) -> AsyncIterator[dict]:
        try:
            async for mode, chunk in self.graph.astream(input_, self._cfg(run_id), stream_mode=["updates", "custom"]):
                if mode == "custom":
                    yield chunk
                    continue
                for node, upd in chunk.items():
                    if node == "__interrupt__":
                        yield {"type": "review_required"}
                        continue
                    entry = (upd or {}).get("trace", [{}])[-1] if isinstance(upd, dict) else {}
                    yield {"type": "agent", "node": node, "agent": entry.get("agent"), "ms": entry.get("ms"),
                           "detail": {k: v for k, v in entry.items() if k not in ("agent", "node", "ms", "timings_ms")}}
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)
            yield {"type": "error", "message": "The research workflow failed", "run_id": run_id}
            return
        yield {"type": "done", "result": await self._finish(run_id)}

    async def stream(self, p: Principal, message: str, *, conversation_id: str | None = None, collection_ids: list[str] | None = None,
                     review: str | None = None, research_context_id: str | None = None) -> AsyncIterator[dict]:
        run_id, conv_id, state = await self._prepare(p, message, conversation_id, collection_ids, review, research_context_id)
        yield {"type": "run", "run_id": run_id, "conversation_id": conv_id}
        async for ev in self._events(run_id, state):
            yield ev

    # --------------------------------------------------------------- resume
    async def _owned_run(self, db: AsyncSession, p: Principal, run_id: str) -> AgentRun:
        run = await db.get(AgentRun, run_id)
        if run is None or run.org_id != p.org_id or (run.user_id != p.user_id and p.role != "admin"):
            raise NotFound("Run not found")  # no existence leak across users/orgs
        return run

    async def _resume_input(self, p: Principal, run_id: str, decision: str, answer: str | None, note: str | None) -> Command:
        if decision not in ("approve", "edit", "reject"):
            raise ValidationFailed("decision must be approve, edit or reject")
        if decision == "edit" and not (answer or "").strip():
            raise ValidationFailed("edit requires a non-empty answer")
        async with self.sf() as db:
            run = await self._owned_run(db, p, run_id)
            if run.status != "needs_review":
                raise Conflict("Run is not awaiting review")
            set_prompt_pins(pins_of(run.variants))
            await audit(db, "chat.review", org_id=p.org_id, user_id=p.user_id, resource=run_id, decision=decision)
            await db.commit()
        return Command(resume={"decision": decision, "answer": answer, "note": note, "reviewer": p.user_id})  # reviewer set server-side

    async def resume(self, p: Principal, run_id: str, decision: str, answer: str | None = None, note: str | None = None) -> dict:
        cmd = await self._resume_input(p, run_id, decision, answer, note)
        try:
            await self.graph.ainvoke(cmd, self._cfg(run_id))
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, exc)
            raise WorkflowFailed("The research workflow failed", details={"run_id": run_id}) from exc
        return await self._finish(run_id)

    async def resume_stream(self, p: Principal, run_id: str, decision: str, answer: str | None = None, note: str | None = None) -> AsyncIterator[dict]:
        cmd = await self._resume_input(p, run_id, decision, answer, note)
        async for ev in self._events(run_id, cmd):
            yield ev

    # ------------------------------------------------------------- inspection
    async def inspect(self, p: Principal, run_id: str) -> dict:
        async with self.sf() as db:
            run = await self._owned_run(db, p, run_id)
            meta = {"run_id": run.id, "status": run.status, "query": run.query, "conversation_id": run.conversation_id,
                    "created_at": run.created_at.isoformat(), "error": run.error}
        snap = await self.graph.aget_state(self._cfg(run_id))
        values = dict(snap.values)
        result = build_result(run_id, meta["status"], values)
        result.update(meta, next=list(snap.next), state_keys=sorted(values))
        result["pending_review"] = [i.value for t in snap.tasks for i in t.interrupts] or None
        return result

    async def list_runs(self, p: Principal, limit: int = 50) -> list[dict]:
        async with self.sf() as db:
            q = select(AgentRun).where(AgentRun.org_id == p.org_id)
            if p.role != "admin":
                q = q.where(AgentRun.user_id == p.user_id)
            rows = (await db.execute(q.order_by(AgentRun.created_at.desc()).limit(limit))).scalars().all()
        return [{"run_id": r.id, "status": r.status, "query": r.query[:200], "conversation_id": r.conversation_id,
                 "created_at": r.created_at.isoformat()} for r in rows]

    async def delete_conversation(self, p: Principal, conversation_id: str) -> None:
        from sqlalchemy import delete

        async with self.sf() as db:
            conv = await db.get(Conversation, conversation_id)
            if conv is None or conv.org_id != p.org_id or conv.user_id != p.user_id:
                raise NotFound("Conversation not found")
            run_ids = list((await db.execute(select(AgentRun.id).where(AgentRun.conversation_id == conversation_id))).scalars())
            await db.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await db.execute(delete(AgentRun).where(AgentRun.conversation_id == conversation_id))
            await db.delete(conv)
            await audit(db, "conversation.delete", org_id=p.org_id, user_id=p.user_id, resource=conversation_id)
            await db.commit()
        if self.checkpointer is not None:  # checkpoints contain the question, evidence text and answer: delete them too
            for rid in run_ids:
                await self.checkpointer.adelete_thread(rid)
