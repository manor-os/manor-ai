import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.services.mcp_seed import seed_mcp_catalog
from packages.core.services.official_remote_mcp import (
    OfficialRemoteMCPFactory,
    OfficialRemoteMCPProvider,
)

pytestmark = pytest.mark.oss_smoke

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test",
)


async def test_mcp_catalog_seed_is_safe_under_concurrent_startup(client):
    """Multiple API/test workers may boot at the same time."""
    engine_a = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    engine_b = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        inserted_a, inserted_b = await asyncio.gather(
            seed_mcp_catalog(engine_a),
            seed_mcp_catalog(engine_b),
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    assert inserted_a >= 0
    assert inserted_b >= 0


async def test_mcp_catalog_seed_refreshes_existing_remote_endpoint(client):
    """Existing installs must not retain the old PayPal production root."""
    import packages.core.database as db_module

    async with db_module.engine.begin() as conn:
        await conn.execute(
            text("""
                UPDATE mcp_servers
                SET endpoint = 'https://mcp.paypal.com'
                WHERE server_key = 'paypal'
            """),
        )

    await seed_mcp_catalog(db_module.engine)

    async with db_module.engine.connect() as conn:
        endpoint = await conn.scalar(
            text("SELECT endpoint FROM mcp_servers WHERE server_key = 'paypal'"),
        )
    assert (
        endpoint
        == OfficialRemoteMCPFactory.create(
            OfficialRemoteMCPProvider.PAYPAL,
        ).endpoint
    )


async def test_mcp_catalog_seed_replaces_legacy_google_scopes(client):
    """Existing installs must display only the currently reviewed scopes."""
    import packages.core.database as db_module

    legacy = {
        "gmail": "https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.send",
        "google_calendar": "https://www.googleapis.com/auth/calendar",
        "google_drive": "https://www.googleapis.com/auth/drive",
        "youtube": "https://www.googleapis.com/auth/youtube,https://www.googleapis.com/auth/youtube.upload",
    }
    async with db_module.engine.begin() as conn:
        for server_key, scopes in legacy.items():
            await conn.execute(
                text("""
                    UPDATE mcp_servers
                    SET scopes = :scopes
                    WHERE server_key = :server_key
                """),
                {"server_key": server_key, "scopes": scopes},
            )

    await seed_mcp_catalog(db_module.engine)

    async with db_module.engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT server_key, scopes
            FROM mcp_servers
            WHERE server_key IN ('gmail', 'google_calendar', 'google_drive', 'youtube')
        """))).all()

    actual = {row.server_key: set((row.scopes or "").split(",")) for row in rows}
    assert actual == {
        "gmail": {"https://www.googleapis.com/auth/gmail.modify"},
        "google_calendar": {
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
        },
        "google_drive": {
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        },
        "youtube": {"https://www.googleapis.com/auth/youtube.force-ssl"},
    }


async def test_mcp_catalog_seed_provides_official_workflow_tool_cache(client):
    import packages.core.database as db_module
    from packages.core.ai.runtime.planning import (
        runtime_planner_action_specs_from_tools_cached,
    )

    await seed_mcp_catalog(db_module.engine)

    async with db_module.engine.connect() as conn:
        cache = await conn.scalar(
            text("SELECT tools_cached FROM mcp_servers WHERE server_key = 'paypal'"),
        )

    assert cache["source"] == "official_fallback"
    specs = runtime_planner_action_specs_from_tools_cached(cache)
    assert specs["create_invoice"]["input_schema"]["required"] == [
        "recipient_email",
        "items",
    ]
    assert "show_product_details" in specs


async def test_mcp_catalog_seed_refreshes_existing_discord_row_to_oauth2(client):
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                UPDATE mcp_servers
                SET auth_type = 'api_key',
                    description = 'legacy Discord bot token form'
                WHERE server_key = 'discord'
            """))

        await seed_mcp_catalog(engine)

        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT auth_type, description
                FROM mcp_servers
                WHERE server_key = 'discord'
            """))).one()
    finally:
        await engine.dispose()

    assert row.auth_type == "oauth2"
    assert "connected Discord Server" in row.description
