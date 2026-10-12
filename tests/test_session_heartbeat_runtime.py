"""Real PostgreSQL heartbeat fast path, isolated in a per-test schema."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.models.user_session import UserSessionLease, UserSessionLog, UserPageViewLog
from packages.core.services.user_session_service import touch_user_session


@pytest_asyncio.fixture
async def heartbeat_db():
    from conftest import TEST_DATABASE_URL

    url = TEST_DATABASE_URL
    schema = "heartbeat_test_" + uuid4().hex
    admin = create_async_engine(url, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            for table in (UserSessionLog.__table__, UserSessionLease.__table__, UserPageViewLog.__table__):
                await connection.run_sync(table.create)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            # This exact schema was created above; no shared tables are removed.
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["live", "legacy_closed", "expired", "wrong_scope"])
async def test_heartbeat_preserves_lifecycle_and_bounds_hot_path(heartbeat_db, case):
    engine, factory = heartbeat_db
    now = datetime.now(UTC)
    async with factory() as db:
        row = UserSessionLog(id="session", entity_id="entity", user_id="user",
            started_at=now - timedelta(minutes=2), last_seen_at=now - timedelta(seconds=30),
            heartbeat_count=40, current_path="/chat", current_path_started_at=now - timedelta(minutes=1))
        if case == "legacy_closed":
            row.status, row.ended_at = "closed", now - timedelta(seconds=1)
        db.add(row)
        await db.flush()
        db.add(UserSessionLease(id="lease", session_id=row.id,
            entity_id="other" if case == "wrong_scope" else "entity", user_id="user",
            expires_at=now + timedelta(seconds=-5 if case == "expired" else 60)))
        await db.commit()

    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as db:
            # An already-loaded ORM row must also see the returned update.
            loaded = await db.get(UserSessionLog, "session")
            statements.clear()
            touched = await touch_user_session(db, session_id="session",
                entity_id="entity", user_id="user", lease_id="lease")
            assert touched is (case in {"live", "legacy_closed"})
            if touched:
                assert len(statements) == 3  # scope lock + two conditional updates
                assert loaded.heartbeat_count == 41 and loaded.status == "active"
                assert loaded.ended_at is None and loaded.duration_seconds >= 120
                assert loaded.current_path == "/chat"
                lease = await db.get(UserSessionLease, "lease")
                assert lease.expires_at >= now + timedelta(seconds=89)
            elif case == "wrong_scope":
                assert loaded.heartbeat_count == 40
            await db.commit()
        async with factory() as db:
            persisted = await db.get(UserSessionLog, "session")
            if case == "expired":
                assert persisted.status == "closed" and persisted.heartbeat_count == 40
                assert await db.get(UserSessionLease, "lease") is None
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
