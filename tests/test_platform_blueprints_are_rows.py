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

import asyncio
import copy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.blueprints.seed import (
    PLATFORM_BLUEPRINT_ID_PREFIX,
    platform_blueprint_id,
    seed_platform_blueprints,
)
from packages.core.blueprints.payload import detect_version
from packages.core.blueprints.solo_company import get_solo_company_blueprints
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.workspace import Workspace

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
    assert rows[SLUG].payload_version == detect_version(rows[SLUG].payload)
    assert rows[SLUG].author_handle == "manor"
    assert rows[SLUG].author_display_name == "Manor AI"


@pytest.mark.asyncio
async def test_raw_payload_inherits_official_identity_only_for_exact_content(
    db_session,
):
    from apps.api.routers.blueprints import (
        _published_platform_blueprint_matching_payload,
    )

    await seed_platform_blueprints(db_session)
    payload = copy.deepcopy(next(
        item for item in get_solo_company_blueprints()
        if item["manifest"]["slug"] == SLUG
    ))

    matched = await _published_platform_blueprint_matching_payload(
        db_session, payload,
    )
    assert matched is not None
    assert matched.id == platform_blueprint_id(SLUG)

    payload["embedded"]["skills"][0]["system_prompt"] = "caller supplied rewrite"
    assert await _published_platform_blueprint_matching_payload(
        db_session, payload,
    ) is None


@pytest.mark.asyncio
async def test_seeding_projects_showcase_metadata_onto_the_marketplace_row(
    db_session,
    monkeypatch,
):
    payloads = [copy.deepcopy(payload) for payload in get_solo_company_blueprints()]
    target = next(payload for payload in payloads if payload["manifest"]["slug"] == SLUG)
    target["manifest"]["cover_image_url"] = "/assets/blueprints/stickman/cover.png"
    target["manifest"]["showcase_assets"] = [{
        "id": "stickman-cover",
        "kind": "image",
        "url": "/assets/blueprints/stickman/cover.png",
        "alt_text": "Stickman Blueprint preview",
    }]
    monkeypatch.setattr(
        "packages.core.blueprints.seed.get_solo_company_blueprints",
        lambda: payloads,
    )

    await seed_platform_blueprints(db_session)

    row = await db_session.get(WorkspaceBlueprint, platform_blueprint_id(SLUG))
    assert row is not None
    assert row.cover_image_url == "/assets/blueprints/stickman/cover.png"
    assert row.showcase_assets == target["manifest"]["showcase_assets"]


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
async def test_seeding_adopts_a_legacy_platform_row_without_aborting(db_session):
    """A pre-stable-id official row must not block every Blueprint refresh."""
    older = copy.deepcopy(next(
        payload for payload in get_solo_company_blueprints()
        if payload["manifest"]["slug"] == SLUG
    ))
    older["embedded"]["skills"][0]["system_prompt"] = "legacy stub"
    legacy = WorkspaceBlueprint(
        id="01LEGACYPLATFORMBLUEPRINT01",
        entity_id=None,
        slug=SLUG,
        title="Legacy platform listing",
        payload=older,
        content_version="1.0.0",
        status="published",
    )
    db_session.add(legacy)
    await db_session.flush()

    versions = await seed_platform_blueprints(db_session)

    assert versions[SLUG] != "1.0.0"
    assert legacy.payload["embedded"]["skills"][0]["system_prompt"] != "legacy stub"
    assert legacy.title != "Legacy platform listing"
    assert await db_session.get(WorkspaceBlueprint, platform_blueprint_id(SLUG)) is None

    from apps.api.routers.blueprints import _find_blueprint_row

    resolved = await _find_blueprint_row(db_session, platform_blueprint_id(SLUG))
    assert resolved is legacy

    workspace = Workspace(
        entity_id="01TESTENTITY0000000000000P",
        name="Legacy slug-only install",
        settings={"_blueprint": {"blueprint_slug": SLUG}},
    )
    db_session.add(workspace)
    await db_session.flush()

    from apps.api.routers.workspaces import _blueprint_payloads_for

    payloads = await _blueprint_payloads_for(db_session, [workspace])
    assert payloads[workspace.id] == (
        legacy.payload,
        legacy.content_version,
        legacy.id,
    )


@pytest.mark.asyncio
async def test_install_count_increment_is_atomic_across_sessions(db_session):
    from apps.api.routers.blueprints import _increment_blueprint_install_count
    from packages.core.models.base import generate_ulid

    blueprint = WorkspaceBlueprint(
        entity_id=generate_ulid(),
        slug=f"concurrent-installs-{generate_ulid().lower()}",
        title="Concurrent install counter",
        payload={"manifest": {"blueprint_version": "1.1"}},
        payload_version="1.1",
        status="published",
        install_count=0,
    )
    db_session.add(blueprint)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
    )

    async def increment_once() -> None:
        async with session_factory() as session:
            await _increment_blueprint_install_count(session, blueprint.id)
            await session.commit()

    increments = 8
    await asyncio.gather(*(increment_once() for _ in range(increments)))
    async with session_factory() as verification_session:
        install_count = (await verification_session.execute(
            select(WorkspaceBlueprint.install_count).where(
                WorkspaceBlueprint.id == blueprint.id,
            )
        )).scalar_one()

    assert install_count == increments


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
    assert "resolve_blueprint_rows" in body


def test_marketplace_listing_does_not_append_the_configs_to_the_rows():
    """Seeded platform rows and frozen configs are the same blueprints.

    Returning both makes every marketplace card and its count appear twice.
    """
    import inspect

    from apps.api.routers import blueprints

    body = inspect.getsource(blueprints.list_blueprints)
    assert "get_solo_company_blueprints" not in body
    assert "return summaries" in body


def test_marketplace_detail_and_install_use_the_canonical_row():
    import inspect

    from apps.api.routers import blueprints

    detail_body = inspect.getsource(blueprints.get_blueprint)
    install_body = inspect.getsource(blueprints.install)
    assert "_builtin_detail" not in detail_body
    assert "_load_blueprint" in detail_body
    assert "_builtin_payload_for_id" not in install_body
    assert "_load_blueprint" in install_body


def test_the_seeder_runs_at_startup():
    from pathlib import Path

    body = Path("apps/api/main.py").read_text(encoding="utf-8")
    assert "seed_platform_blueprints" in body
