from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import DDL, JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, EmbeddingType, IdMixin, TimestampMixin, UTCDateTime

# pgvector columns have a fixed dimension; it is read once at import from NEXUS_EMBEDDING_DIM.
EMBEDDING_DIM = int(os.getenv("NEXUS_EMBEDDING_DIM", "384"))
DOC_STATUSES = ("pending", "processing", "ready", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")


class Document(Base, IdMixin, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("org_id", "collection_id", "logical_key"),)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), index=True)
    # logical_key groups versions of "the same" document (normalised filename).
    logical_key: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(300))
    doc_type: Mapped[str] = mapped_column(String(20))
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentVersion(Base, IdMixin):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version"),)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    effective_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Chunk(Base, IdMixin):
    """Retrieval unit. org/collection/is_current are denormalised so that access control and
    temporal filters are applied *inside* the retrieval query, never after it."""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_scope", "org_id", "collection_id", "is_current"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    collection_id: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(10), default="child")  # parent | child
    kind: Mapped[str] = mapped_column(String(10), default="text", server_default="text", index=True)  # text | table | figure
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(500), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    effective_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(EMBEDDING_DIM), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class IngestionJob(Base, IdMixin, TimestampMixin):
    __tablename__ = "ingestion_jobs"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# PostgreSQL-only DDL: extension, full-text GIN index and HNSW cosine index (ignored on SQLite).
event.listen(Base.metadata, "before_create", DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"))
event.listen(Chunk.__table__, "after_create", DDL(
    "CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks USING gin (to_tsvector('simple', text))").execute_if(dialect="postgresql"))
event.listen(Chunk.__table__, "after_create", DDL(
    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)").execute_if(dialect="postgresql"))
