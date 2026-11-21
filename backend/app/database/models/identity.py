from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin, TimestampMixin, UTCDateTime, utcnow

ROLES = ("admin", "editor", "viewer")


class Organization(Base, IdMixin, TimestampMixin):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(200), unique=True)


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    hashed_password: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Collection(Base, IdMixin, TimestampMixin):
    """A knowledge base. Restricted collections are visible only to ACL members."""

    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False)


class CollectionAccess(Base, IdMixin):
    __tablename__ = "collection_access"
    __table_args__ = (UniqueConstraint("collection_id", "user_id"),)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class AuditLog(Base, IdMixin):
    __tablename__ = "audit_logs"
    org_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sig: Mapped[str | None] = mapped_column(String(64), nullable=True)      # HMAC over the row (tamper evidence; see app/security/auditsig.py)


class OrgPolicy(Base):
    __tablename__ = "org_policies"
    org_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
