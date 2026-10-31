"""An account timezone follows the device until the user chooses one."""

import pytest
from sqlalchemy import text

from packages.core.services.auth_service import register_user


async def _register(client, email, **extra):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "securepass123", "entity_name": "Timezone Co", **extra},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _zone(client, headers):
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return me["timezone"], me["timezone_user_set"]


@pytest.mark.asyncio
async def test_signup_adopts_device_zone_and_an_explicit_choice_survives_device_sync(client):
    headers = await _register(client, "tz-device@example.com", timezone="America/Los_Angeles")
    assert await _zone(client, headers) == ("America/Los_Angeles", False)

    # An unknown device zone must not block signup; the account keeps the default.
    fallback = await _register(client, "tz-invalid@example.com", timezone="Mars/Olympus")
    assert await _zone(client, fallback) == ("UTC", False)

    synced = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"timezone": "Asia/Tokyo", "timezone_source": "device"},
    )
    assert synced.status_code == 200, synced.text
    assert (synced.json()["timezone"], synced.json()["timezone_user_set"]) == ("Asia/Tokyo", False)

    # Choosing UTC is a choice even though it equals the old default.
    chosen = await client.put("/api/v1/auth/me", headers=headers, json={"timezone": "UTC"})
    assert chosen.status_code == 200, chosen.text
    assert await _zone(client, headers) == ("UTC", True)

    ignored = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"timezone": "Europe/Paris", "timezone_source": "device"},
    )
    assert ignored.status_code == 200, ignored.text
    assert await _zone(client, headers) == ("UTC", True)

    invalid = await client.put("/api/v1/auth/me", headers=headers, json={"timezone": "Mars/Olympus"})
    assert invalid.status_code == 422, invalid.text
    missing = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"timezone_source": "device", "display_name": "No zone"},
    )
    assert missing.status_code == 400, missing.text
    assert await _zone(client, headers) == ("UTC", True)


@pytest.mark.asyncio
async def test_migration_keeps_non_default_zones_fixed_and_releases_utc(db_session):
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/migrations"))
    migration = ScriptDirectory.from_config(config).get_revision("20261021_01").module
    zones = {"UTC": False, "America/New_York": True}
    ids = {}
    for index, zone in enumerate(zones):
        user, _entity = await register_user(
            db_session, email=f"tz-backfill-{index}@example.com",
            password="securepass123", entity_name="Backfill Co",
        )
        user.timezone = zone
        ids[zone] = user.id
    await db_session.flush()

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.downgrade()
            migration.upgrade()

    connection = await db_session.connection()
    await connection.run_sync(migrate)
    rows = (await db_session.execute(
        text("SELECT id, timezone_user_set FROM users WHERE id = ANY(:ids)"),
        {"ids": list(ids.values())},
    )).all()
    assert {row.id: row.timezone_user_set for row in rows} == {ids[zone]: flag for zone, flag in zones.items()}
