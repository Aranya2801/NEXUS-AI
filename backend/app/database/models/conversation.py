from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin, UTCDateTime, utcnow


class Conversation(Base, IdMixin, TimestampMixin):
    __tablename__ = "conversations"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New research session")
    collection_ids: Mapped[list] = mapped_column(JSON, default=list)


class Message(Base, IdMixin):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MemoryItem(Base, IdMixin):
    """kinds: long_term (user preferences) | session (per-conversation working set, TTL) | research_context (saved findings,
    pinned documents) | doc_interaction (recently cited/viewed documents, TTL). Short-term memory is the Message table."""

    __tablename__ = "memory_items"
    __table_args__ = (UniqueConstraint("org_id", "user_id", "kind", "scope_id", "key"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    scope_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AgentRun(Base, IdMixin, TimestampMixin):
    """One execution of the research workflow. The LangGraph checkpoint (thread_id == id) holds the full state."""

    __tablename__ = "agent_runs"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running|needs_review|completed|failed
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    variants: Mapped[dict] = mapped_column(JSON, default=dict)             # experiment id -> {"name","variant","task","version"} this run was assigned to


class AutonomousRun(Base, IdMixin, TimestampMixin):
    """One autonomous agent run: goal, options, full step trace and the verified answer."""

    __tablename__ = "autonomous_runs"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)   # running | completed | failed
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LLMCall(Base, IdMixin):
    """One metered LLM call (cache hits included, with zero cost). Prompt/response TEXT is never stored, only hashes and counts."""

    __tablename__ = "llm_calls"
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(120), index=True)       # configured route name
    provider_model: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")      # ok | cache_hit | error
    fallback_from: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(20), default="builtin")
    prompt_sha: Mapped[str] = mapped_column(String(16), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class PromptVersion(Base, IdMixin):
    """Org-scoped override of a built-in prompt. The built-in text is always version "builtin" (rollback target)."""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("org_id", "task", "version"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    task: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(String(300), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
