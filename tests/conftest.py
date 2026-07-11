"""
Test fixtures — async DB session and FastAPI test client.

Uses real PostgreSQL (manor_test) for e2e tests.
Each test gets its own engine to avoid Python 3.14 event loop issues.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.models.base import Base
import packages.core.models  # noqa: F401 — import all models for metadata
from packages.core.services.mcp_seed import seed_mcp_catalog

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test")
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("DATABASE_URL_SYNC", TEST_DATABASE_URL.replace("+asyncpg", ""))
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
TEST_EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024") or 1024)

_tables_created = False
_ROOT = Path(__file__).resolve().parents[1]
_CLOUD_ONLY_TEST_PATTERNS: tuple[str, ...] = ()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        rel_path = Path(str(getattr(item, "path", ""))).resolve().relative_to(_ROOT).as_posix()
        if any(fnmatch(rel_path, pattern) for pattern in _CLOUD_ONLY_TEST_PATTERNS):
            item.add_marker(pytest.mark.cloud)


async def _prepare_test_database(engine, *, truncate_existing: bool) -> None:
    """Ensure the shared test database schema matches the current models."""

    global _tables_created
    if not _tables_created:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            # Add pgvector embedding column (managed outside SQLAlchemy model)
            await conn.execute(
                text(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector({TEST_EMBEDDING_DIMENSIONS})")
            )
        _tables_created = True
    elif truncate_existing:
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())

    # ASGITransport does not run FastAPI lifespan startup here, so mirror the
    # production boot hook that keeps the MCP catalog available after tests
    # create/drop/truncate all tables.
    await seed_mcp_catalog(engine)


@pytest_asyncio.fixture
async def client(request) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client — fresh engine per test, tables auto-created."""
    env_overrides = getattr(request, "param", None) or {}
    old_env: dict[str, str | None] = {}
    for key, value in env_overrides.items():
        old_env[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)

    from apps.api.main import create_app
    import packages.core.database as db_module

    # Create a fresh engine in THIS event loop
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Override the app's database module
        db_module.engine = engine
        db_module.async_session = session_factory

        # Create tables on first test, truncate on subsequent client tests.
        await _prepare_test_database(engine, truncate_existing=True)

        app = create_app()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        await engine.dispose()
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """A standalone async DB session, sharing the same DB the ``client``
    fixture uses. Useful for tests that need to seed / inspect rows
    directly without going through the HTTP API.

    Tests that mix ``client`` + ``db_session`` get the same database
    (separate sessions, same Postgres DB) so a row created via
    ``client.post(...)`` is visible to a subsequent ``db_session.execute``.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    await _prepare_test_database(engine, truncate_existing=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        # Tests that mutate via this session are responsible for
        # committing themselves; rolling back here keeps fixture
        # teardown deterministic without surprising committers.
        await session.rollback()
    await engine.dispose()
