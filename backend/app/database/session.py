"""Async engine/session factory. Dependency-injected; no module-level global engine."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import models  # noqa: F401  (register tables)
from app.database.base import Base


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {}
    if url.startswith("sqlite") and ":memory:" in url:
        kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
    engine = create_async_engine(url, **kwargs)
    if url.startswith("postgresql+asyncpg"):
        from pgvector.asyncpg import register_vector

        @event.listens_for(engine.sync_engine, "connect")
        def _register_vector(dbapi_conn, _record):  # noqa: ANN001
            try:
                dbapi_conn.run_async(register_vector)
            except Exception:  # extension not created yet (first boot); create_all recycles the pool
                pass

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if engine.dialect.name == "postgresql":
        await engine.dispose()  # drop connections opened before the vector type existed


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def psycopg_dsn(url: str) -> str:
    """SQLAlchemy URL (postgresql+asyncpg://...) -> libpq DSN understood by psycopg (used by the LangGraph Postgres checkpointer)."""
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    raise ValueError("checkpoint_backend=postgres requires a postgresql database_url")
