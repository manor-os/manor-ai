"""
Test fixtures — async DB session and FastAPI test client.

Uses real PostgreSQL (manor_test) for e2e tests.
Each test gets its own engine to avoid Python 3.14 event loop issues.
"""

from __future__ import annotations

import asyncio
import os
import sys
from fnmatch import fnmatch
from collections import OrderedDict
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

from tests.redis_test_resource import RedisTestResource, require_test_cache_namespace

# ``packages/core/config.py`` bakes its ``os.getenv`` fallbacks at class-body
# time and ``get_settings()`` is lru_cached, so every default below must be in
# the environment before the first ``packages.core`` import on the next lines.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test")
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("DATABASE_URL_SYNC", TEST_DATABASE_URL.replace("+asyncpg", ""))
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
# Never inherit a developer's REDIS_URL. The registry cleaner below uses SCAN
# and DELETE, so it must target either CI's explicit TEST_REDIS_URL or this
# process-owned Unix-socket Redis endpoint. It remains unavailable unless an
# explicit cache consumer starts it, so pure tests never contact a developer
# service.
_TEST_REDIS = RedisTestResource.from_environment()
os.environ["REDIS_URL"] = _TEST_REDIS.url
# Every approval evaluation in the suite must reach a declared effect
# (packages/core/governance/action_effects.py); an undeclared action key
# raises instead of silently evaluating as ``unknown``.
os.environ.setdefault("MANOR_STRICT_EFFECT_CONTRACT", "1")
# A Task run journal write that collides with an existing seq is a journal
# write without its own run-row bump: production logs it, tests fail on it.
os.environ.setdefault("MANOR_TASK_RUN_JOURNAL_STRICT", "1")
TEST_EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024") or 1024)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from packages.core.models.base import Base  # noqa: E402
import packages.core.models  # noqa: E402,F401 — import all models for metadata
from packages.core.services.mcp_seed import seed_mcp_catalog  # noqa: E402
# Imported here so a test stubbing ``packages.core.config.get_settings`` cannot be
# the first to import it: the module would keep that stub for the whole process.
import packages.core.services.sandbox_queue_service  # noqa: E402,F401

_tables_created = False
_TEST_DATABASE_ADVISORY_LOCK_ID = 742918364
_ROOT = Path(__file__).resolve().parents[1]
_CLOUD_ONLY_TEST_PATTERNS: tuple[str, ...] = ()


@pytest.fixture(scope="session")
def isolated_test_redis() -> Generator[str, None, None]:
    """Start the private Redis only for tests that require a live cache."""
    process = _TEST_REDIS.start()
    try:
        yield _TEST_REDIS.url
    finally:
        _TEST_REDIS.cleanup(process)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        rel_path = Path(str(getattr(item, "path", ""))).resolve().relative_to(_ROOT).as_posix()
        if any(fnmatch(rel_path, pattern) for pattern in _CLOUD_ONLY_TEST_PATTERNS):
            item.add_marker(pytest.mark.cloud)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Clear the process-owned namespace once, never a shared cache keyspace."""
    if _TEST_REDIS.directory is None:
        from packages.core.cache import cache

        original_prefix = cache.PREFIX
        cache.PREFIX = _TEST_REDIS.cache_prefix
        try:
            require_test_cache_namespace(cache.PREFIX, _TEST_REDIS.cache_prefix)
            asyncio.run(cache.delete_pattern("*"))
        finally:
            cache.PREFIX = original_prefix
    _TEST_REDIS.cleanup(None)


async def _prepare_test_database(engine, *, truncate_existing: bool) -> None:
    """Ensure the shared test database schema matches the current models."""

    global _tables_created
    if not _tables_created:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            # Add pgvector embedding columns (managed outside SQLAlchemy models)
            # and the trigram index document_chunks' lexical search needs —
            # these come from Alembic migrations in real deployments, but this
            # test bootstrap goes straight from models to schema, bypassing
            # migrations entirely.
            await conn.execute(
                text(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector({TEST_EMBEDDING_DIMENSIONS})")
            )
            await conn.execute(
                text(
                    "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS "
                    f"embedding vector({TEST_EMBEDDING_DIMENSIONS})"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_document_chunks_content_trgm "
                    "ON document_chunks USING GIN (content gin_trgm_ops)"
                )
            )
        _tables_created = True
    elif truncate_existing:
        async with engine.begin() as conn:
            await _delete_test_tables(conn)

    # ASGITransport does not run FastAPI lifespan startup here, so mirror the
    # production boot hook that keeps the MCP catalog available after tests
    # create/drop/truncate all tables.
    await seed_mcp_catalog(engine)


async def _delete_test_tables(conn, tables=None) -> None:
    """Delete test rows in dependency-safe order with one database round trip."""

    tables = Base.metadata.sorted_tables if tables is None else tables
    preparer = conn.dialect.identifier_preparer
    deletes = "\n".join(
        f"DELETE FROM {preparer.format_table(table)};"
        for table in reversed(tables)
    )
    if deletes:
        tag_index = 0
        delimiter = "$manor_test_cleanup$"
        while delimiter in deletes:
            tag_index += 1
            delimiter = f"$manor_test_cleanup_{tag_index}$"
        await conn.execute(text(f"DO {delimiter} BEGIN\n{deletes}\nEND {delimiter};"))


@pytest.fixture(scope="session", autouse=True)
def _schema_for_unfixtured_database_tests() -> None:
    """Create the schema up front for lanes that select tests without DB fixtures.

    Some tests reach the database only through production code and depend on
    an earlier client/db_session fixture having created the schema. A lane
    whose selection may contain no such fixture user opts in with
    MANOR_TEST_PREPARE_SCHEMA=1; other lanes keep lazy schema creation.
    """
    if os.environ.get("MANOR_TEST_PREPARE_SCHEMA") != "1":
        return

    async def prepare() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
        try:
            await _prepare_test_database(engine, truncate_existing=False)
        finally:
            await engine.dispose()

    asyncio.run(prepare())


@pytest_asyncio.fixture(scope="session")
async def _test_database_guard() -> AsyncGenerator[None, None]:
    """Keep one process's shared test schema stable for its entire session."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        async with engine.connect() as guard:
            await guard.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _TEST_DATABASE_ADVISORY_LOCK_ID},
            )
            # Session advisory locks survive COMMIT. Close the implicit
            # transaction so a long test run does not sit idle in transaction.
            await guard.commit()
            try:
                yield
            finally:
                await guard.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _TEST_DATABASE_ADVISORY_LOCK_ID},
                )
                await guard.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def _test_password_hashing(request, monkeypatch) -> None:
    """Keep real bcrypt in DB/API tests without paying the production work factor.

    This scoped patch affects default bcrypt salts, including worker and OAuth
    secrets. Explicit rounds and real_password_hashing tests keep their policy.
    """
    if request.node.get_closest_marker("real_password_hashing"):
        return

    import bcrypt

    original_gensalt = bcrypt.gensalt

    def test_gensalt(*args, **kwargs):
        if not args and "rounds" not in kwargs:
            kwargs["rounds"] = 4
        return original_gensalt(*args, **kwargs)

    monkeypatch.setattr(bcrypt, "gensalt", test_gensalt)


_APP_CACHE: OrderedDict[frozenset[tuple[str, str]], object] = OrderedDict()
_APP_CACHE_SIZE = 4


def _test_app():
    """Reuse an assembled API app for an identical process environment.

    Assembling ~1000 routes dominates API test setup. ``create_app`` derives
    its routers, CORS and docs only from the environment; module state and
    the database factories are resolved per request, so a fresh app per test
    adds no isolation. Overrides are per-test and are cleared on reuse.
    """
    from apps.api.main import create_app

    key = frozenset(
        (name, value) for name, value in os.environ.items() if name != "PYTEST_CURRENT_TEST"
    )
    app = _APP_CACHE.get(key)
    if app is None:
        app = _APP_CACHE[key] = create_app()
        while len(_APP_CACHE) > _APP_CACHE_SIZE:
            _APP_CACHE.popitem(last=False)
    else:
        _APP_CACHE.move_to_end(key)
    app.dependency_overrides.clear()
    return app


@pytest.fixture
def _client_database_reset() -> None:
    """Declare that the root API client needs a clean database for this test."""


@pytest_asyncio.fixture
async def _client_database_preparation(request, _test_database_guard) -> bool:
    """Prepare once before either API or direct DB fixtures can write rows."""
    # Inspect the fixture closure, without resolving the async client. A local
    # fixture named "client" does not necessarily use our application/database.
    if "_client_database_reset" not in request.fixturenames:
        return False

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        await _prepare_test_database(engine, truncate_existing=True)
    finally:
        await engine.dispose()
    return True


@pytest_asyncio.fixture
async def client(
    request, _client_database_reset, _client_database_preparation, _test_password_hashing, monkeypatch,
) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client — fresh engine per test, tables auto-created."""
    env_overrides = getattr(request, "param", None) or {}
    old_env: dict[str, str | None] = {}
    for key, value in env_overrides.items():
        old_env[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)

    import packages.core.database as db_module

    # Create a fresh engine in THIS event loop
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Override the app's database module
        db_module.engine = engine
        db_module.async_session = session_factory

        app = _test_app()

        # Persisted browser Chat now always runs through the durable worker.
        # ASGI tests have no Celery consumer: drive that same worker in-process
        # before draining SSE, without bypassing its claim/checkpoint/commit.
        from apps.api.routers import chat as chat_router
        from packages.core.ai import runtime
        from packages.core.tasks import runtime_tasks

        durable_events = chat_router._durable_runtime_event_stream

        async def test_chat_events(run, **kwargs):
            if run.status == "queued":
                await runtime_tasks.execute_runtime_run_once(
                    run_id=run.id, worker_id=f"test-chat:{run.id}",
                )
            async for frame in durable_events(run, **kwargs):
                yield frame

        async def test_chat_turn(*args, **kwargs):
            # Existing route tests may replace this turn adapter with a stub;
            # the worker must exercise the same stub as the former inline lane.
            async for frame in chat_router.runtime_stream_chat_turn(*args, **kwargs):
                yield frame

        monkeypatch.setattr(db_module, "create_worker_session", lambda: session_factory)
        monkeypatch.setattr(chat_router, "_durable_runtime_event_stream", test_chat_events)
        monkeypatch.setattr(runtime, "runtime_stream_chat_turn", test_chat_turn)


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
async def db_session(_client_database_preparation, _test_password_hashing) -> AsyncGenerator[AsyncSession, None]:
    """A standalone async DB session, sharing the same DB the ``client``
    fixture uses. Useful for tests that need to seed / inspect rows
    directly without going through the HTTP API.

    Tests that mix ``client`` + ``db_session`` get the same database
    (separate sessions, same Postgres DB) so a row created via
    ``client.post(...)`` is visible to a subsequent ``db_session.execute``.
    Their shared preparation completes before either fixture exposes a session.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    if not _client_database_preparation:
        await _prepare_test_database(engine, truncate_existing=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        # Tests that mutate via this session are responsible for
        # committing themselves; rolling back here keeps fixture
        # teardown deterministic without surprising committers.
        await session.rollback()
    await engine.dispose()


@pytest.fixture
def undeclared_effects_allowed(monkeypatch):
    """Run the production fallback for undeclared action effects.

    Only for tests that deliberately exercise an undeclared key (it evaluates
    as ``unknown`` and asks), e.g. plan_dag steps whose action key is a raw
    tool name or absent. Every other test runs the strict effect contract.
    """
    monkeypatch.delenv("MANOR_STRICT_EFFECT_CONTRACT", raising=False)


@pytest.fixture(autouse=True)
def _reset_builtin_skill_seed_verification() -> None:
    """Built-in Skill seeding is verified once per process; each test gets a
    fresh database state, so it must verify again."""
    from packages.core.services.skill_service import reset_builtin_skill_seed_cache

    reset_builtin_skill_seed_cache()


@pytest.fixture(autouse=True)
def _isolate_plan_cache() -> Generator[None, None, None]:
    """Plan gates re-read subscription_plans into a process-global cache; a
    test that seeds plan rows must not leave its plans to the next test."""
    from packages.core.constants import plans

    snapshot = dict(plans.PLANS)
    loaded, read_at = plans._cache_loaded_from_db, plans._cache_read_at
    plans._cache_read_at = None
    yield
    plans.PLANS.clear()
    plans.PLANS.update(snapshot)
    plans._cache_loaded_from_db, plans._cache_read_at = loaded, read_at


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry() -> Generator[None, None, None]:
    """Give every test a private sandbox-cache namespace without I/O.

    The earlier cleaner scanned Redis before and after every async test. That
    grew linearly with unrelated tests and made a cache outage affect tests
    that never use sandbox state. A unique child namespace keeps unknown
    runtime callers isolated; session cleanup scans only the process-owned
    parent namespace once when CI supplied a disposable Redis service.
    """
    from packages.core.cache import cache

    original_prefix = cache.PREFIX
    cache.PREFIX = _TEST_REDIS.cache_prefix_for_test()
    # The session service also keeps a per-process answer per runner URL
    # (its capabilities); every test starts without another test's answer.
    session_service = sys.modules.get("packages.core.services.sandbox_session_service")
    if session_service is not None:
        session_service._RUNNER_CAPABILITIES_CACHE.clear()

    try:
        yield
    finally:
        cache.PREFIX = original_prefix
