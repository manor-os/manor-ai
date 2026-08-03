"""The platform's own blueprints are published rows, not a second code path.

They used to be frozen JSON configs addressed as ``builtin:<slug>`` and
served beside the marketplace table, so everything about a blueprint existed
twice — and the stale-install check had to carry a branch saying built-ins
have no publish step, judge them by content instead.

They do have one. Approving a blueprint onto the marketplace is already an
admin action; official blueprints had simply never been routed through it.
Seeding them makes them ordinary: published, versioned, and identified by id
rather than by being the one shape you match on a slug.
"""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import select

from packages.core.blueprints.seed import (
    PLATFORM_BLUEPRINT_ID_PREFIX,
    platform_blueprint_id,
    seed_platform_blueprints,
)
from packages.core.blueprints.solo_company import get_solo_company_blueprints
from packages.core.models.blueprint import WorkspaceBlueprint

SLUG = "solo-faceless-stickman-studio-v1"


async def _rows(db_session):
    return (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.entity_id.is_(None))
    )).scalars().all()


# ── Seeding ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_config_becomes_a_published_row(db_session):
    await seed_platform_blueprints(db_session)

    rows = {row.slug: row for row in await _rows(db_session)}
    assert len(rows) == len(get_solo_company_blueprints())
    assert SLUG in rows
    assert rows[SLUG].status == "published"
    assert rows[SLUG].entity_id is None, "the platform owns it"


@pytest.mark.asyncio
async def test_the_id_it_already_had_is_kept(db_session):
    """Minting a ULID would break every URL and stored reference naming it."""
    await seed_platform_blueprints(db_session)

    row = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == platform_blueprint_id(SLUG))
    )).scalar_one_or_none()
    assert row is not None
    assert row.id.startswith(PLATFORM_BLUEPRINT_ID_PREFIX)


@pytest.mark.asyncio
async def test_seeding_starts_the_version(db_session):
    versions = await seed_platform_blueprints(db_session)
    assert versions[SLUG] == "1.0.1", "the first publish is a release"


@pytest.mark.asyncio
async def test_redeploying_unchanged_configs_moves_nothing(db_session):
    """Every workspace installed from these would otherwise be told it is
    behind on every deploy."""
    first = await seed_platform_blueprints(db_session)
    second = await seed_platform_blueprints(db_session)
    assert first == second

    row = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == platform_blueprint_id(SLUG))
    )).scalar_one()
    published_at = row.published_at

    await seed_platform_blueprints(db_session)
    assert row.published_at == published_at


@pytest.mark.asyncio
async def test_shipping_a_corrected_config_publishes_a_new_version(db_session, monkeypatch):
    """The whole point: a fix to an official blueprint reaches the people
    who installed it."""
    await seed_platform_blueprints(db_session)
    before = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == platform_blueprint_id(SLUG))
    )).scalar_one().content_version

    corrected = [copy.deepcopy(p) for p in get_solo_company_blueprints()]
    for payload in corrected:
        if payload["manifest"]["slug"] == SLUG:
            payload["embedded"]["skills"][0]["system_prompt"] = "the corrected procedure"

    monkeypatch.setattr(
        "packages.core.blueprints.seed.get_solo_company_blueprints", lambda: corrected,
    )
    after = await seed_platform_blueprints(db_session)
    assert after[SLUG] != before


@pytest.mark.asyncio
async def test_a_listing_edit_does_not_republish(db_session, monkeypatch):
    await seed_platform_blueprints(db_session)
    before = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == platform_blueprint_id(SLUG))
    )).scalar_one().content_version

    reworded = [copy.deepcopy(p) for p in get_solo_company_blueprints()]
    for payload in reworded:
        if payload["manifest"]["slug"] == SLUG:
            payload["manifest"]["summary"] = "reworded for the listing"

    monkeypatch.setattr(
        "packages.core.blueprints.seed.get_solo_company_blueprints", lambda: reworded,
    )
    after = await seed_platform_blueprints(db_session)
    assert after[SLUG] == before
    row = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == platform_blueprint_id(SLUG))
    )).scalar_one()
    assert row.summary == "reworded for the listing", "the listing still updates"


# ── No special case left ──────────────────────────────────────────────


def test_freshness_identifies_by_id_not_slug():
    """The slug is a display name and a per-owner uniqueness rule. Treating it
    as identity is what made platform blueprints need their own branch."""
    import inspect

    from packages.core.blueprints import freshness

    body = inspect.getsource(freshness.blueprint_freshness)
    assert "BLUEPRINT_ID_KEY" in body
    assert 'record.get("blueprint_slug")' not in body


def test_nothing_resolves_a_payload_from_the_config_directory():
    """One source for what a workspace upgrades toward: the table."""
    import inspect

    from apps.api.routers import workspaces

    body = inspect.getsource(workspaces._blueprint_payloads_for)
    assert "get_solo_company_blueprint" not in body
    assert "WorkspaceBlueprint" in body


def test_the_seeder_runs_at_startup():
    from pathlib import Path

    body = Path("apps/api/main.py").read_text(encoding="utf-8")
    assert "seed_platform_blueprints" in body
