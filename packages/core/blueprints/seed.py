"""Publish the platform's own blueprints into the marketplace table.

They used to be frozen JSON configs served through a parallel code path, so
everything about a blueprint existed twice — and a stale-install check had to
carry a branch saying built-ins have no publish step, judge them by content
instead. They do have one: approving a blueprint onto the marketplace is
already an admin action. Official blueprints had simply never gone through it.

Seeding makes them ordinary rows: published, versioned, identified by id. The
version moves the same way a contributor's does — on release, and only when
the installable content actually changed — so shipping a corrected config
tells every workspace installed from it that an update exists, without anyone
remembering to touch a number.

Runs at API start, beside the other catalogue seeders. Idempotent: a
redeploy of unchanged configs writes nothing and moves no version.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.blueprints.freshness import (
    FIRST_CONTENT_VERSION,
    next_content_version,
)
from packages.core.blueprints.payload import detect_version
from packages.core.blueprints.solo_company import get_solo_company_blueprints
from packages.core.models.blueprint import WorkspaceBlueprint

logger = logging.getLogger(__name__)

#: The id a platform blueprint keeps. Minting a ULID instead would break every
#: URL and stored reference that already names it.
PLATFORM_BLUEPRINT_ID_PREFIX = "builtin:"

PUBLISHED = BlueprintStatus.PUBLISHED.value


def platform_blueprint_id(slug: str) -> str:
    return f"{PLATFORM_BLUEPRINT_ID_PREFIX}{slug}"


@dataclass(frozen=True)
class BlueprintRowReference:
    """Durable Blueprint identity plus its historical platform fallback."""

    blueprint_id: str | None = None
    blueprint_slug: str | None = None


async def resolve_blueprint_rows(
    db: AsyncSession,
    references: Mapping[str, BlueprintRowReference],
) -> dict[str, WorkspaceBlueprint]:
    """Resolve Blueprint rows in two batched queries.

    ``blueprint_slug`` is used only for the historical platform shapes that
    predate durable ids (null id or ``builtin:<slug>`` alias). Any other exact
    id is authoritative even when the row is archived or another Marketplace
    item has the same slug.
    """
    normalized = {
        key: BlueprintRowReference(
            blueprint_id=str(ref.blueprint_id or "").strip() or None,
            blueprint_slug=str(ref.blueprint_slug or "").strip() or None,
        )
        for key, ref in references.items()
    }
    candidate_ids = {
        candidate_id
        for ref in normalized.values()
        for candidate_id in (
            ref.blueprint_id,
            platform_blueprint_id(ref.blueprint_slug) if ref.blueprint_slug else None,
        )
        if candidate_id
    }
    rows_by_id: dict[str, WorkspaceBlueprint] = {}
    if candidate_ids:
        rows = (await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.id.in_(candidate_ids),
            )
        )).scalars().all()
        rows_by_id = {row.id: row for row in rows}

    resolved: dict[str, WorkspaceBlueprint] = {}
    unresolved_slugs: set[str] = set()
    for key, ref in normalized.items():
        exact_row = rows_by_id.get(ref.blueprint_id or "")
        row = exact_row
        allows_platform_alias = (
            ref.blueprint_id is None
            or str(ref.blueprint_id).startswith(PLATFORM_BLUEPRINT_ID_PREFIX)
        )
        if row is None and ref.blueprint_slug and allows_platform_alias:
            row = rows_by_id.get(platform_blueprint_id(ref.blueprint_slug))
        if row is not None:
            resolved[key] = row
        elif ref.blueprint_slug and allows_platform_alias:
            unresolved_slugs.add(ref.blueprint_slug)

    if unresolved_slugs:
        legacy_rows = (await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.entity_id.is_(None),
                WorkspaceBlueprint.slug.in_(unresolved_slugs),
            )
        )).scalars().all()
        legacy_by_slug = {row.slug: row for row in legacy_rows}
        for key, ref in normalized.items():
            if key not in resolved and ref.blueprint_slug in legacy_by_slug:
                resolved[key] = legacy_by_slug[ref.blueprint_slug]

    return resolved


async def resolve_blueprint_row(
    db: AsyncSession,
    *,
    blueprint_id: str | None = None,
    blueprint_slug: str | None = None,
) -> WorkspaceBlueprint | None:
    resolved = await resolve_blueprint_rows(
        db,
        {"row": BlueprintRowReference(blueprint_id, blueprint_slug)},
    )
    return resolved.get("row")


def _manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest")
    return manifest if isinstance(manifest, dict) else {}


async def seed_platform_blueprints(db: AsyncSession) -> dict[str, str]:
    """Upsert the platform's blueprints. Returns {slug: content_version}.

    Caller commits.
    """
    published: dict[str, str] = {}

    for payload in get_solo_company_blueprints():
        manifest = _manifest(payload)
        slug = str(manifest.get("slug") or "").strip()
        if not slug:
            logger.warning("platform blueprint seed: config with no slug, skipped")
            continue

        row_id = platform_blueprint_id(slug)
        row = (await db.execute(
            select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == row_id)
        )).scalar_one_or_none()
        if row is None:
            # Before stable builtin:<slug> ids, official listings could be
            # published with a generated id. Creating the stable row beside
            # one of those collides with the platform-slug unique index and
            # aborts the entire startup seed, leaving every later Blueprint
            # payload stale. Reconcile that legacy row in place; its id may
            # still be referenced by installed workspaces and purchases.
            row = (await db.execute(
                select(WorkspaceBlueprint).where(
                    WorkspaceBlueprint.entity_id.is_(None),
                    WorkspaceBlueprint.slug == slug,
                )
            )).scalars().first()
            if row is not None:
                logger.info(
                    "platform blueprint legacy row adopted: %s (%s)",
                    slug,
                    row.id,
                )

        version, fingerprint = next_content_version(
            current_version=getattr(row, "content_version", None) or FIRST_CONTENT_VERSION,
            stored_fingerprint=getattr(row, "content_fingerprint", None),
            payload=payload,
        )

        tags = manifest.get("tags")
        showcase_assets = manifest.get("showcase_assets")
        author = manifest.get("author")
        if not isinstance(author, dict):
            author = {}
        fields = {
            "slug": slug,
            "title": str(manifest.get("title") or slug),
            "summary": manifest.get("summary"),
            "description": manifest.get("description"),
            "cover_image_url": manifest.get("cover_image_url"),
            "showcase_assets": [
                dict(asset)
                for asset in showcase_assets
                if isinstance(asset, dict)
            ] if isinstance(showcase_assets, list) else [],
            "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
            "author_handle": str(author.get("handle") or "").strip() or None,
            "author_display_name": (
                str(author.get("display_name") or "").strip() or None
            ),
            "remixed_from_id": (
                str(manifest.get("forked_from_id") or "").strip() or None
            ),
            "payload": payload,
            "payload_version": detect_version(payload),
            "status": PUBLISHED,
            "content_version": version,
            "content_fingerprint": fingerprint,
        }

        if row is None:
            row = WorkspaceBlueprint(
                id=row_id,
                entity_id=None,  # the platform owns it
                published_at=datetime.now(timezone.utc),
                **fields,
            )
            db.add(row)
            logger.info("platform blueprint seeded: %s at %s", slug, version)
        else:
            changed = version != row.content_version
            for key, value in fields.items():
                setattr(row, key, value)
            if changed:
                row.published_at = datetime.now(timezone.utc)
                logger.info("platform blueprint republished: %s → %s", slug, version)

        published[slug] = version

    await db.flush()
    return published
