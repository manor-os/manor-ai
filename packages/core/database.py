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
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.config import get_settings

settings = get_settings()

_IS_TEST_DATABASE = urlparse(settings.DATABASE_URL.replace("+asyncpg", "")).path.endswith("_test")


def _build_engine_kwargs(settings, *, is_test_database: bool) -> dict:
    kwargs = {
        "echo": settings.DATABASE_ECHO,
        "pool_pre_ping": True,
    }
    if getattr(settings, "DATABASE_POOL_MODE", "sqlalchemy") == "pgbouncer":
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = _pgbouncer_connect_args()
    elif is_test_database:
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


_engine_kwargs = _build_engine_kwargs(settings, is_test_database=_IS_TEST_DATABASE)

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session, auto-commits on success."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
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
    _engine = create_async_engine(_settings.DATABASE_URL, **_build_worker_engine_kwargs(_settings))
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
