from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.session import create_all, make_engine, make_session_factory


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="test", database_url="sqlite+aiosqlite:///:memory:", storage_dir=str(tmp_path / "storage"),
        checkpoint_path=str(tmp_path / "ckpt.sqlite"), log_json=False, embedding_dim=64,
    )


@pytest_asyncio.fixture
async def db(settings):
    engine = make_engine(settings.database_url)
    await create_all(engine)
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()
