from __future__ import annotations

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin


class Report(Base, IdMixin, TimestampMixin):
    """A generated research report. `content` is the structured, citation-bearing report (see app/reporting/model.py)."""

    __tablename__ = "reports"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300))
    topic: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|ready|failed
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[list] = mapped_column(JSON, default=list)
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
