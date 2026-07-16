import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from packages.core.services.mcp_seed import seed_mcp_catalog

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
