from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, UTCDateTime, utcnow


class Feedback(Base, IdMixin):
    """Thumbs up/down (+ optional comment) on a workflow run's answer. One row per (run, user); re-rating updates it."""

    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("run_id", "user_id"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    rating: Mapped[int] = mapped_column(Integer)  # -1 | 1
    comment: Mapped[str] = mapped_column(Text, default="")
    reasons: Mapped[list] = mapped_column(JSON, default=list)             # fixed vocabulary, see app/learning/feedback.py
    allow_review: Mapped[bool] = mapped_column(Boolean, default=False)    # the user agreed that org admins may read this run's query + answer
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvalCase(Base, IdMixin):
    """A regression case for retrieval: a query and the documents that should be found. Org-scoped; may be promoted from consented feedback."""

    __tablename__ = "eval_cases"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    query: Mapped[str] = mapped_column(Text)
    expected_document_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvalRun(Base, IdMixin):
    __tablename__ = "eval_runs"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)               # mode, k
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)              # means over cases
    per_case: Mapped[list] = mapped_column(JSON, default=list)             # [{case_id, ranked, p_at_k, r_at_k, mrr, ndcg}]
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Experiment(Base, IdMixin):
    """A/B test of prompt versions for one task. Assignment is deterministic per (experiment, run); outcomes come from feedback + run quality signals."""

    __tablename__ = "experiments"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(100))
    task: Mapped[str] = mapped_column(String(40), index=True)
    variants: Mapped[list] = mapped_column(JSON, default=list)             # [{"name","prompt_version": int|"builtin","weight"}]
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)   # running | stopped | concluded
    winner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
