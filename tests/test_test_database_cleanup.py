"""Shared test database preparation preserves cleanup and isolation semantics."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.models.mcp import MCPServer
from packages.core.models.user import User
from tests import conftest as test_config
from tests.conftest import TEST_DATABASE_URL, _delete_test_tables


@pytest.fixture(autouse=True)
def database_preparations(monkeypatch):
    calls = []
    prepare = test_config._prepare_test_database

    async def record_preparation(engine, *, truncate_existing):
        calls.append(truncate_existing)
        await prepare(engine, truncate_existing=truncate_existing)

    monkeypatch.setattr(test_config, "_prepare_test_database", record_preparation)
    return calls


class TestSharedDatabasePreparation:
    @pytest.fixture(autouse=True)
    async def seeded_catalog_entry(self, database_preparations, db_session):
        # Autouse dependencies run before the non-autouse API client. The
        # client's database reset must already be complete before this write.
        entry = MCPServer(
            server_key="fixture_prepared_catalog",
            name="Fixture-owned catalog entry",
            transport="builtin",
            endpoint="fixture:test",
            auth_type="internal",
        )
        db_session.add(entry)
        await db_session.commit()
        return entry.id

    @pytest.mark.oss_regression
    async def test_api_and_database_share_preparation_without_deleting_fixture_data(
        self, client, db_session, seeded_catalog_entry, database_preparations,
    ):
        assert await db_session.get(MCPServer, seeded_catalog_entry) is not None
        assert database_preparations == [True]
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "fixture_shared_prepare",
                "email": "fixture-shared-prepare@example.com",
                "password": "SetupContract123!",
            },
        )
        assert response.status_code == 200, response.text
        user = await db_session.get(User, response.json()["user_id"])
        assert user is not None
        assert user.email == "fixture-shared-prepare@example.com"


class TestAlternateClient:
    @pytest.fixture
    def client(self):
        """A local fixture with this name does not request API database cleanup."""
        return object()

    @pytest.mark.oss_regression
    async def test_alternate_client_keeps_standalone_database_preparation(
        self, client, db_session, database_preparations,
    ):
        assert database_preparations == [False]
        assert await db_session.scalar(select(MCPServer.id).where(MCPServer.server_key == "gmail"))


@pytest.mark.oss_regression
@pytest.mark.asyncio
async def test_bulk_test_cleanup_preserves_delete_and_transaction_semantics(_test_database_guard):
    metadata = MetaData()
    parent = Table(
        "parent cleanup",
        metadata,
        Column("id", Integer, primary_key=True),
    )
    child = Table(
        'child; cleanup "$manor_test_cleanup$',
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey(parent.c.id), nullable=False),
    )
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(text("CREATE TABLE cleanup_audit (table_name text NOT NULL)"))
            await conn.execute(text("""
                CREATE FUNCTION cleanup_delete_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    INSERT INTO cleanup_audit (table_name) VALUES (TG_TABLE_NAME);
                    RETURN OLD;
                END;
                $$
            """))
            await conn.execute(text("""
                CREATE TRIGGER cleanup_delete_audit_trigger
                AFTER DELETE ON "child; cleanup ""$manor_test_cleanup$"
                FOR EACH ROW EXECUTE FUNCTION cleanup_delete_audit()
            """))
            parent_id = await conn.scalar(parent.insert().returning(parent.c.id))
            await conn.execute(child.insert().values(parent_id=parent_id))

        async with engine.connect() as conn:
            transaction = await conn.begin()
            await _delete_test_tables(conn, metadata.sorted_tables)
            assert await conn.scalar(select(func.count()).select_from(parent)) == 0
            assert await conn.scalar(select(func.count()).select_from(child)) == 0
            await transaction.rollback()

        async with engine.connect() as conn:
            assert await conn.scalar(select(func.count()).select_from(parent)) == 1
            assert await conn.scalar(select(func.count()).select_from(child)) == 1
            assert await conn.scalar(text("SELECT count(*) FROM cleanup_audit")) == 0

        async with engine.begin() as conn:
            await _delete_test_tables(conn, metadata.sorted_tables)

        async with engine.begin() as conn:
            assert await conn.scalar(select(func.count()).select_from(parent)) == 0
            assert await conn.scalar(select(func.count()).select_from(child)) == 0
            assert await conn.scalar(text("SELECT count(*) FROM cleanup_audit")) == 1
            next_parent_id = await conn.scalar(parent.insert().returning(parent.c.id))
            assert next_parent_id == parent_id + 1
    finally:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(child.drop, checkfirst=True)
                await conn.run_sync(parent.drop, checkfirst=True)
                await conn.execute(text("DROP FUNCTION IF EXISTS cleanup_delete_audit()"))
                await conn.execute(text("DROP TABLE IF EXISTS cleanup_audit"))
        finally:
            await engine.dispose()
