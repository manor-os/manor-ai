from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.pool import NullPool

from packages.core import database


def test_cloud_pgbouncer_mode_uses_null_pool_and_unique_prepared_names() -> None:
    settings = SimpleNamespace(
        DATABASE_ECHO=False,
        DATABASE_POOL_MODE="pgbouncer",
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = database._build_engine_kwargs(settings, is_test_database=False)

    assert kwargs["poolclass"] is NullPool
    assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0
    prepared_name = kwargs["connect_args"]["prepared_statement_name_func"]
    assert callable(prepared_name)
    assert prepared_name() != prepared_name()


def test_standard_mode_keeps_sqlalchemy_pool() -> None:
    settings = SimpleNamespace(
        DATABASE_ECHO=False,
        DATABASE_POOL_MODE="sqlalchemy",
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = database._build_engine_kwargs(settings, is_test_database=False)

    assert kwargs["pool_size"] == 5
    assert "poolclass" not in kwargs
    assert "connect_args" not in kwargs


def test_worker_engine_uses_pgbouncer_connect_contract() -> None:
    settings = SimpleNamespace(DATABASE_ECHO=False, DATABASE_POOL_MODE="pgbouncer")

    kwargs = database._build_worker_engine_kwargs(settings)

    assert kwargs["poolclass"] is NullPool
    assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0
    assert callable(kwargs["connect_args"]["prepared_statement_name_func"])
