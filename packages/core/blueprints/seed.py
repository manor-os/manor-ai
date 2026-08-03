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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.blueprints.freshness import (
    FIRST_CONTENT_VERSION,
    next_content_version,
)
from packages.core.blueprints.solo_company import get_solo_company_blueprints
from packages.core.models.blueprint import WorkspaceBlueprint

logger = logging.getLogger(__name__)

#: The id a platform blueprint keeps. Minting a ULID instead would break every
#: URL and stored reference that already names it.
PLATFORM_BLUEPRINT_ID_PREFIX = "builtin:"

PUBLISHED = BlueprintStatus.PUBLISHED.value


def platform_blueprint_id(slug: str) -> str:
    return f"{PLATFORM_BLUEPRINT_ID_PREFIX}{slug}"


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

        version, fingerprint = next_content_version(
            current_version=getattr(row, "content_version", None) or FIRST_CONTENT_VERSION,
            stored_fingerprint=getattr(row, "content_fingerprint", None),
            payload=payload,
        )

        tags = manifest.get("tags")
        fields = {
            "slug": slug,
            "title": str(manifest.get("title") or slug),
            "summary": manifest.get("summary"),
            "description": manifest.get("description"),
            "cover_image_url": manifest.get("cover_image_url"),
            "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
            "payload": payload,
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
