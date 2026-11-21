from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import PermissionDenied
from app.database.models import User
from app.knowledge_graph.retrieval import GraphRetriever
from app.llm.base import LLM, UsageMeter
from app.retrieval.pipeline import RetrievalPipeline
from app.retrieval.service import HybridRetriever
from app.retrieval.transform import QueryTransformer


@dataclass
class AgentDeps:
    session_factory: async_sessionmaker[AsyncSession]
    pipeline: RetrievalPipeline
    retriever: HybridRetriever
    graph: GraphRetriever
    llm: LLM
    settings: Settings

    async def principal(self, state: dict, db: AsyncSession) -> Principal:
        """Re-validates the caller on every data-touching node: a run resumed hours later (human review) must not
        outlive a deactivated account or a role change."""
        pr = state["principal"]
        user = await db.get(User, pr["user_id"])
        if user is None or not user.is_active or user.org_id != pr["org_id"]:
            raise PermissionDenied("Run owner is no longer active")
        return Principal(user.id, user.org_id, user.role, user.email)

    def transformer(self, meter: UsageMeter) -> QueryTransformer:
        return QueryTransformer(self.llm, meter)


def usage_dict(m: UsageMeter) -> dict:
    return {"calls": m.calls, "prompt_tokens": m.prompt_tokens, "completion_tokens": m.completion_tokens, "by_task": dict(m.by_task)}
