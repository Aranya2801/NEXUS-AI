from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IdMixin


class Entity(Base, IdMixin):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("org_id", "canonical_name", "type"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    canonical_name: Mapped[str] = mapped_column(String(300), index=True)
    display_name: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(30), index=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)


class EntityMention(Base, IdMixin):
    """Links an entity to the exact chunk (and therefore document/page) it was found in."""

    __tablename__ = "entity_mentions"
    __table_args__ = (Index("ix_mentions_entity", "entity_id"), UniqueConstraint("entity_id", "chunk_id"))
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), index=True)


class Relation(Base, IdMixin):
    __tablename__ = "relations"
    __table_args__ = (Index("ix_relations_src_tgt", "source_id", "target_id"),)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    target_id: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    predicate: Mapped[str] = mapped_column(String(80), index=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)  # verbatim source sentence


class EntityAlias(Base, IdMixin):
    """Every canonical surface form that resolves to an entity (including its own canonical name)."""

    __tablename__ = "entity_aliases"
    __table_args__ = (UniqueConstraint("entity_id", "alias_canonical"), Index("ix_alias_lookup", "org_id", "alias_canonical"))
    org_id: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    alias_canonical: Mapped[str] = mapped_column(String(300))
