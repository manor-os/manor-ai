from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.pool import NullPool

from packages.core import database


def test_cloud_pgbouncer_mode_keeps_a_client_pool_and_unique_prepared_names() -> None:
    """Each new PgBouncer client connection costs a TLS + SCRAM handshake
    (~105ms on managed Postgres); API/chat reuse connections from a bounded pool."""
    settings = SimpleNamespace(
        DATABASE_ECHO=False,
        DATABASE_POOL_MODE="pgbouncer",
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = database._build_engine_kwargs(settings, is_test_database=False)

    assert "poolclass" not in kwargs
    assert kwargs["pool_size"] == 5
    assert kwargs["max_overflow"] == 2
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0
    prepared_name = kwargs["connect_args"]["prepared_statement_name_func"]
    assert callable(prepared_name)
    assert prepared_name() != prepared_name()


def test_pgbouncer_mode_on_a_test_database_still_uses_null_pool() -> None:
    settings = SimpleNamespace(
        DATABASE_ECHO=False,
        DATABASE_POOL_MODE="pgbouncer",
        DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=2,
        DATABASE_POOL_TIMEOUT=10,
        DATABASE_POOL_RECYCLE=1800,
    )

    kwargs = database._build_engine_kwargs(settings, is_test_database=True)

    assert kwargs["poolclass"] is NullPool
    assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0


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


def test_test_database_is_recognised_by_suffix_or_explicit_test_url() -> None:
    """A pooled engine on a test database ties asyncpg connections to the
    first test's event loop; every later test then fails with "attached to a
    different loop". The name check must therefore cover the per-worker and
    scratch spellings, and the explicit TEST_DATABASE_URL always wins."""
    is_test = database.is_test_database_url

    assert is_test("postgresql+asyncpg://u:p@localhost:5434/manor_test")
    assert is_test("postgresql+asyncpg://u:p@localhost:5434/manor_test_gw3")
    assert is_test("postgresql+asyncpg://u:p@localhost:5434/manor_test_hi")
    assert not is_test("postgresql+asyncpg://u:p@localhost:5432/manor")
    assert not is_test("postgresql+asyncpg://u:p@localhost:5432/manor_testing_ground")
    assert not is_test("postgresql+asyncpg://u:p@localhost:5432/latest")

    assert is_test(
        "postgresql+asyncpg://u:p@localhost:5432/anything",
        test_database_url="postgresql://u:p@localhost:5432/anything",
    )
    assert not is_test(
        "postgresql+asyncpg://u:p@localhost:5432/manor",
        test_database_url="postgresql+asyncpg://u:p@localhost:5434/manor_test",
    )
