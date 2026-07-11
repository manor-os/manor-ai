"""Runtime configuration tests for single-server safety knobs."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.pool import NullPool


def test_settings_keep_single_server_defaults(monkeypatch):
    """OSS defaults stay conservative unless Cloud overrides via env."""
    from packages.core.config import get_settings

    for key in (
        "API_WORKERS",
        "API_LIMIT_CONCURRENCY",
        "API_BACKLOG",
        "API_TIMEOUT_KEEP_ALIVE",
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT",
        "DATABASE_POOL_RECYCLE",
        "REDIS_RATE_LIMIT_ENABLED",
        "DEGRADED_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.API_WORKERS == 1
    assert settings.API_LIMIT_CONCURRENCY == 120
    assert settings.API_BACKLOG == 256
    assert settings.API_TIMEOUT_KEEP_ALIVE == 5
    assert settings.DATABASE_POOL_SIZE == 5
    assert settings.DATABASE_MAX_OVERFLOW == 2
    assert settings.DATABASE_POOL_TIMEOUT == 10
    assert settings.DATABASE_POOL_RECYCLE == 1800
    assert settings.REDIS_RATE_LIMIT_ENABLED is False
    assert settings.DEGRADED_MODE is False

    get_settings.cache_clear()


def test_settings_read_cloud_runtime_overrides(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setenv("API_WORKERS", "2")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "6")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "1")
    monkeypatch.setenv("REDIS_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("DEGRADED_MODE", "1")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.API_WORKERS == 2
    assert settings.DATABASE_POOL_SIZE == 6
    assert settings.DATABASE_MAX_OVERFLOW == 1
    assert settings.REDIS_RATE_LIMIT_ENABLED is True
    assert settings.DEGRADED_MODE is True

    get_settings.cache_clear()


def test_database_pool_kwargs_use_env_settings_for_non_test_database():
    from packages.core.database import _build_engine_kwargs

    settings = SimpleNamespace(
        DATABASE_ECHO=False,
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = _build_engine_kwargs(settings, is_test_database=False)

    assert kwargs == {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 2,
        "pool_timeout": 10,
        "pool_recycle": 1800,
    }


def test_database_pool_kwargs_keep_nullpool_for_test_database():
    from packages.core.database import _build_engine_kwargs

    settings = SimpleNamespace(
        DATABASE_ECHO=True,
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = _build_engine_kwargs(settings, is_test_database=True)

    assert kwargs["echo"] is True
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs
