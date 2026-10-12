"""
Async PostgreSQL connection pool via SQLAlchemy 2.0.

Usage:
    from packages.core.database import get_db

    @router.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Item))
        return result.scalars().all()
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
import os
import re
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from packages.core.config import get_settings
from packages.core.database_runtime import (
    DatabaseEngineKind,
    DatabaseSession,
    configure_database_runtime,
)
from packages.core.services.integration_availability_cache import install as _install_integration_availability_invalidation

# Every session's committed writes invalidate discovery's cached integration
# availability (subclass listeners such as admission release run first).
_install_integration_availability_invalidation(Session)
settings = get_settings()


def is_test_database_url(database_url: str, *, test_database_url: str | None = None) -> bool:
    """Whether ``database_url`` is a throwaway test database.

    A test database gets ``NullPool``: pytest gives every test its own event
    loop, and a pooled asyncpg connection is bound to the loop that opened it,
    so the second test to reuse it dies with "attached to a different loop".
    Recognised by name (``…_test`` or ``…_test_<suffix>``, e.g. the per-worker
    ``manor_test_gw3`` or a scratch ``manor_test_hi``) or explicitly, when the
    URL is the one the test harness exported as ``TEST_DATABASE_URL``.
    """
    normalized = database_url.replace("+asyncpg", "")
    if test_database_url and normalized == test_database_url.replace("+asyncpg", ""):
        return True
    path = urlparse(normalized).path
    return re.search(r"_test(?:_[A-Za-z0-9]+)?$", path) is not None


_IS_TEST_DATABASE = is_test_database_url(
    settings.DATABASE_URL, test_database_url=os.environ.get("TEST_DATABASE_URL"),
)


def _build_engine_kwargs(settings, *, is_test_database: bool) -> dict:
    kwargs = {
        "echo": settings.DATABASE_ECHO,
        "pool_pre_ping": True,
    }
    if getattr(settings, "DATABASE_POOL_MODE", "sqlalchemy") == "pgbouncer":
        # PgBouncer pools server connections, but every new client connection
        # is still a TCP + TLS + SCRAM handshake (~105ms p50 on DOKS against
        # ~3ms per query on an open one). API/chat run one long-lived event
        # loop, so keep a bounded client pool; transaction pooling stays safe
        # because prepared statements are disabled. Workers keep NullPool:
        # their tasks run on short-lived asyncio.run loops.
        kwargs["connect_args"] = _pgbouncer_connect_args()
    if is_test_database:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.DATABASE_POOL_SIZE
        kwargs["max_overflow"] = settings.DATABASE_MAX_OVERFLOW
        kwargs["pool_timeout"] = settings.DATABASE_POOL_TIMEOUT
        kwargs["pool_recycle"] = settings.DATABASE_POOL_RECYCLE
    return kwargs


def _pgbouncer_connect_args() -> dict:
    return {
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }


def _build_worker_engine_kwargs(settings) -> dict:
    kwargs = {
        "echo": settings.DATABASE_ECHO,
        "poolclass": NullPool,
    }
    if getattr(settings, "DATABASE_POOL_MODE", "sqlalchemy") == "pgbouncer":
        kwargs["connect_args"] = _pgbouncer_connect_args()
    return kwargs


def create_database_engine(settings, *, kind: DatabaseEngineKind, is_test_database: bool = False):
    """One engine policy for API/chat and disposable worker event loops."""
    kind = DatabaseEngineKind(kind)
    kwargs = (
        _build_worker_engine_kwargs(settings)
        if kind is DatabaseEngineKind.WORKER
        else _build_engine_kwargs(settings, is_test_database=is_test_database)
    )
    engine = create_async_engine(settings.DATABASE_URL, **kwargs)
    configure_database_runtime(engine, settings, kind)
    return engine


def create_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=DatabaseSession,
        expire_on_commit=False,
    )


engine = create_database_engine(
    settings, kind=DatabaseEngineKind.for_service(settings.MANOR_SERVICE_ROLE),
    is_test_database=_IS_TEST_DATABASE,
)
async_session = create_session_factory(engine)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session, auto-commits on success."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def create_worker_session() -> async_sessionmaker[AsyncSession]:
    """Create a disposable engine + session factory for Celery workers.

    Celery prefork workers inherit the module-level engine whose connections
    are bound to the parent process's event loop.  Each asyncio.run() call
    creates a new loop, causing 'Future attached to a different loop' errors.
    This function creates a fresh engine (and session factory) that is safe
    to use inside asyncio.run().

    Uses NullPool since each task creates a fresh engine anyway.
    The DATABASE_URL is re-read from settings to avoid any stale state
    from the forked parent process's module-level engine.
    """
    _settings = get_settings()
    _engine = create_database_engine(_settings, kind=DatabaseEngineKind.WORKER)
    return create_session_factory(_engine)
