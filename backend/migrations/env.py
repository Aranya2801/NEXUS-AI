"""Alembic environment (async). The database URL is read from NEXUS_DATABASE_URL through the app's Settings."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.database import models  # noqa: F401  (register all tables on Base.metadata)
from app.database.base import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}:
        return False   # owned by LangGraph (AsyncPostgresSaver.setup)
    if type_ == "index" and name in {"ix_chunks_fts", "ix_chunks_embedding_hnsw"}:
        return False   # PostgreSQL-only indexes created with explicit DDL in the migration; not representable in portable metadata
    return True


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, include_object=_include_object)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=_include_object, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
