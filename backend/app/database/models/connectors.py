from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin, UTCDateTime


class Connector(Base, IdMixin, TimestampMixin):
    """An external data source synced into one collection. Secrets are stored Fernet-encrypted and never returned by the API."""

    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(32))          # sync runs with this user's permissions (re-read every run)
    kind: Mapped[str] = mapped_column(String(20))                # google_drive | sharepoint | slack | teams | sql | rest
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict] = mapped_column(JSON, default=dict)     # non-secret settings
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    acl_mode: Mapped[str] = mapped_column(String(10), default="strict")   # strict | inherit  (see docs/connectors.md)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)   # None = manual only
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)                # incremental-sync watermark / delta link
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ConnectorItem(Base, IdMixin):
    """Mapping external object -> NEXUS document; drives change detection and deletion propagation."""

    __tablename__ = "connector_items"
    __table_args__ = (UniqueConstraint("connector_id", "external_id"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="synced")   # synced | skipped_acl | failed | deleted
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_acl: Mapped[list | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ConnectorRun(Base, IdMixin):
    __tablename__ = "connector_runs"
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")   # running | succeeded | partial | failed
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
