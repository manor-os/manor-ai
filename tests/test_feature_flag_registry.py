"""Known-flag registry + idempotent seeding.

``is_enabled`` fails closed for keys that have no ``feature_flags`` row,
so a code path gated on an unregistered flag is invisible in the admin
Flags page. ``KNOWN_FLAGS`` + ``seed_known_flags`` register those rows at
their safe default. The critical property under test: seeding NEVER
modifies an existing row — an ops-set default or an archived status wins.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.services.feature_flags import (
    KNOWN_FLAGS,
    _bump_cache,
    is_enabled,
    seed_known_flags,
    set_override,
)


async def _clear_flags(db) -> None:
    await db.execute(delete(FeatureFlag))
    await db.commit()
    _bump_cache()


async def _keys(db) -> set[str]:
    return set((await db.execute(select(FeatureFlag.key))).scalars().all())


# ── Registry shape ───────────────────────────────────────────────────

def test_known_flags_are_unique_and_documented():
    keys = [k.key for k in KNOWN_FLAGS]
    assert len(keys) == len(set(keys)), "duplicate key in KNOWN_FLAGS"
    assert "strategist_review_v2" in keys
    assert "tool_discovery_v2" in keys
    for known in KNOWN_FLAGS:
        assert known.description.strip(), f"{known.key} needs a description"
        assert len(known.description) <= 200


# ── Seeding ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seed_creates_one_row_per_known_flag(db_session):
    await _clear_flags(db_session)

    created = await seed_known_flags(db_session)
    await db_session.commit()

    assert created == len(KNOWN_FLAGS)
    rows = {
        r.key: r for r in
        (await db_session.execute(select(FeatureFlag))).scalars().all()
    }
    assert set(rows) == {k.key for k in KNOWN_FLAGS}
    for known in KNOWN_FLAGS:
        row = rows[known.key]
        assert row.default_enabled is known.default_enabled
        assert row.status == "active"
        assert row.description == known.description


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    await _clear_flags(db_session)

    first = await seed_known_flags(db_session)
    await db_session.commit()
    second = await seed_known_flags(db_session)
    await db_session.commit()

    assert first == len(KNOWN_FLAGS)
    assert second == 0
    assert await _keys(db_session) == {k.key for k in KNOWN_FLAGS}


@pytest.mark.asyncio
async def test_seed_never_overwrites_an_ops_enabled_default(db_session):
    """The whole safety property: ops turned a flag ON — a re-seed must
    not reset it to the registry's default of False."""
    await _clear_flags(db_session)
    await seed_known_flags(db_session)
    await db_session.commit()

    flag = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "strategist_review_v2")
    )).scalar_one()
    flag.default_enabled = True
    flag.description = "ops-edited copy"
    await db_session.commit()
    _bump_cache()

    created = await seed_known_flags(db_session)
    await db_session.commit()

    assert created == 0
    await db_session.refresh(flag)
    assert flag.default_enabled is True
    assert flag.description == "ops-edited copy"


@pytest.mark.asyncio
async def test_seed_does_not_resurrect_an_archived_flag(db_session):
    await _clear_flags(db_session)
    await seed_known_flags(db_session)
    await db_session.commit()

    flag = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "tool_discovery_v2")
    )).scalar_one()
    flag.status = "archived"
    await db_session.commit()

    created = await seed_known_flags(db_session)
    await db_session.commit()

    assert created == 0
    await db_session.refresh(flag)
    assert flag.status == "archived"
    # And exactly one row still — no duplicate "active" twin.
    count = len(list((await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "tool_discovery_v2")
    )).scalars().all()))
    assert count == 1


@pytest.mark.asyncio
async def test_seeded_flag_is_evaluable_and_overridable(db_session):
    """Proves the seeded row is a real evaluation target, not cosmetic:
    safe default off, tenant override flips it on."""
    await _clear_flags(db_session)
    await seed_known_flags(db_session)
    await db_session.commit()

    entity_id = generate_ulid()
    assert await is_enabled(
        db_session, "strategist_review_v2", entity_id=entity_id,
    ) is False

    await set_override(
        db_session, key="strategist_review_v2", scope="tenant",
        scope_id=entity_id, enabled=True,
        set_by_admin_id=None, set_reason="pilot tenant",
    )
    await db_session.commit()

    assert await is_enabled(
        db_session, "strategist_review_v2", entity_id=entity_id,
    ) is True
    # Untargeted tenants stay at the safe default.
    assert await is_enabled(
        db_session, "strategist_review_v2", entity_id=generate_ulid(),
    ) is False
