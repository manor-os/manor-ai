import pytest


@pytest.mark.asyncio
async def test_fee_defaults_to_zero(db_session):
    from packages.core.services.marketplace_settings import get_marketplace_fee_percent

    assert await get_marketplace_fee_percent(db_session) == 0


@pytest.mark.asyncio
async def test_fee_set_and_read_back(db_session):
    from packages.core.services.marketplace_settings import (
        MARKETPLACE_SETTINGS_KEY,
        get_marketplace_fee_percent,
        set_marketplace_fee_percent,
    )

    try:
        await set_marketplace_fee_percent(db_session, 20, updated_by="admin_x")
        await db_session.commit()
        assert await get_marketplace_fee_percent(db_session) == 20

        # Overwrite, don't duplicate the row.
        await set_marketplace_fee_percent(db_session, 5, updated_by="admin_x")
        await db_session.commit()
        assert await get_marketplace_fee_percent(db_session) == 5
    finally:
        # This test commits rows directly via db_session, and the shared
        # test DB is only truncated by the `client` fixture — clean up
        # here so other db_session-only tests don't see leaked state.
        from sqlalchemy import delete

        from packages.core.models.platform_setting import PlatformSetting

        await db_session.execute(
            delete(PlatformSetting).where(PlatformSetting.key == MARKETPLACE_SETTINGS_KEY)
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_fee_bounds(db_session):
    from packages.core.services.marketplace_settings import set_marketplace_fee_percent

    with pytest.raises(ValueError):
        await set_marketplace_fee_percent(db_session, -1, updated_by="a")
    with pytest.raises(ValueError):
        await set_marketplace_fee_percent(db_session, 101, updated_by="a")


@pytest.mark.asyncio
async def test_fee_rejects_bools(db_session):
    # bool is a subclass of int — must not sneak past the integer guard.
    from packages.core.services.marketplace_settings import set_marketplace_fee_percent

    with pytest.raises(ValueError):
        await set_marketplace_fee_percent(db_session, True, updated_by="a")
    with pytest.raises(ValueError):
        await set_marketplace_fee_percent(db_session, False, updated_by="a")


@pytest.mark.asyncio
async def test_fee_read_clamps_corrupt_values(db_session):
    from packages.core.models.platform_setting import PlatformSetting
    from packages.core.services.marketplace_settings import (
        MARKETPLACE_SETTINGS_KEY,
        get_marketplace_fee_percent,
    )

    try:
        # Non-numeric garbage → safe default 0.
        row = PlatformSetting(
            key=MARKETPLACE_SETTINGS_KEY,
            value={"platform_fee_percent": "abc"},
        )
        db_session.add(row)
        await db_session.commit()
        assert await get_marketplace_fee_percent(db_session) == 0

        # Out-of-range value → clamped to 100.
        row.value = {"platform_fee_percent": 250}
        await db_session.commit()
        assert await get_marketplace_fee_percent(db_session) == 100
    finally:
        # Clean up committed rows — the shared test DB only truncates
        # in the `client` fixture, not for db_session-only tests.
        from sqlalchemy import delete

        await db_session.execute(
            delete(PlatformSetting).where(PlatformSetting.key == MARKETPLACE_SETTINGS_KEY)
        )
        await db_session.commit()


def test_platform_fee_cents_floor():
    from packages.core.services.marketplace_settings import platform_fee_cents

    assert platform_fee_cents(4900, 0) == 0
    assert platform_fee_cents(4900, 20) == 980
    assert platform_fee_cents(999, 15) == 149  # floors, never rounds up
    assert platform_fee_cents(1, 1) == 0
