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

Cloud runs this from the deployment's one-shot Blueprint publisher after
every runtime rollout is complete. Single-owner API deployments, including
OSS and Compose, keep their startup compatibility path. Idempotent: unchanged
configs write nothing and move no version.
Archived listings stay withdrawn until an explicit publication action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.blueprints.freshness import FIRST_CONTENT_VERSION, BlueprintDigest
from packages.core.blueprints.payload import detect_version
from packages.core.blueprints.catalogue import get_platform_blueprints
from packages.core.blueprints.registry import next_release_version, record_release
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.services.blueprint_listing_settings import (
    blueprint_listing_overrides,
    lock_blueprint_listing_settings,
)

logger = logging.getLogger(__name__)

#: The id a platform blueprint keeps. Minting a ULID instead would break every
#: URL and stored reference that already names it.
PLATFORM_BLUEPRINT_ID_PREFIX = "builtin:"

RETIRED_PLATFORM_BLUEPRINT_SLUGS = (
    "solo-creator-fundraising-room-v1",
    "solo-creator-partnership-studio-v1",
    "solo-video-account-studio-v1",
    "solo-productized-service-os-v1",
    "solo-digital-product-store-v1",
    # Single-request document helpers do not justify standalone Workspaces.
    # Keep their source examples and existing installs, but withdraw listings.
    "practical-meeting-notes-v1",
    "practical-weekly-update-v1",
    "practical-feedback-triage-v1",
    "practical-support-replies-v1",
    "practical-proposal-draft-v1",
    "practical-sop-builder-v1",
    "practical-job-application-v1",
    "practical-content-repurpose-v1",
    "practical-bilingual-editor-v1",
    "practical-source-brief-v1",
    # Replaced by creative-carousel-agent-v1, the Agent-and-Skill Carousel studio.
    # Existing installs keep working; the listing is withdrawn.
    "creative-carousel-v1",
)

PUBLISHED = BlueprintStatus.PUBLISHED.value
LISTING_OVERRIDE_FIELDS = (
    "title",
    "summary",
    "description",
    "tags",
    "cover_image_url",
    "showcase_assets",
)


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
    *,
    load_payload: bool = True,
) -> dict[str, WorkspaceBlueprint]:
    """Resolve Blueprint rows in two batched queries.

    ``blueprint_slug`` is used only for the historical platform shapes that
    predate durable ids (null id or ``builtin:<slug>`` alias). Any other exact
    id is authoritative even when the row is archived or another Marketplace
    item has the same slug.

    ``load_payload=False`` leaves each row's payload unread.
    """
    options = () if load_payload else (defer(WorkspaceBlueprint.payload),)
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
            select(WorkspaceBlueprint).options(*options).where(
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
            select(WorkspaceBlueprint).options(*options).where(
                WorkspaceBlueprint.entity_id.is_(None),
                WorkspaceBlueprint.slug.in_(unresolved_slugs),
            )
        )).scalars().all()
        legacy_by_slug = {row.slug: row for row in legacy_rows}
        for key, ref in normalized.items():
            if key not in resolved and ref.blueprint_slug in legacy_by_slug:
                resolved[key] = legacy_by_slug[ref.blueprint_slug]

    return resolved


# (row id, row version) -> the payload's digest, or None for an empty payload.
# Postgres gives a row a new ``xmin`` (the writing transaction) on every
# update, so a committed version is never answered from another version's
# entry. Only versions older than every transaction this snapshot still sees
# running are cached: they are committed and were not written here (a row this
# transaction rewrites keeps one ``xmin``).
_PAYLOAD_DIGESTS: dict[tuple[str, int], BlueprintDigest | None] = {}
_PAYLOAD_DIGEST_LIMIT = 1024
_ROW_VERSION = literal_column("workspace_blueprints.xmin")
_SETTLED = literal_column(
    "age(workspace_blueprints.xmin) > age(pg_snapshot_xmin(pg_current_snapshot())::xid)"
)


async def blueprint_payload_digests(
    db: AsyncSession, row_ids: set[str],
) -> dict[str, tuple[BlueprintDigest, str]]:
    """Published digests and content versions from the same row snapshots."""
    if not row_ids:
        return {}
    published = select(
        WorkspaceBlueprint.id, WorkspaceBlueprint.content_version, _ROW_VERSION, _SETTLED,
    ).where(WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED)
    versions = (await db.execute(published.where(WorkspaceBlueprint.id.in_(row_ids)))).all()
    digests = {}
    missing = []
    for row_id, content_version, version, settled in versions:
        key = (row_id, version)
        if settled and key in _PAYLOAD_DIGESTS:
            if (digest := _PAYLOAD_DIGESTS[key]) is not None:
                digests[row_id] = (digest, content_version)
        else:
            missing.append(row_id)
    if missing:
        for row_id, content_version, version, settled, payload in (await db.execute(
            published.add_columns(WorkspaceBlueprint.payload)
            .where(WorkspaceBlueprint.id.in_(missing))
        )).all():
            digest = BlueprintDigest.of(payload) if isinstance(payload, dict) and payload else None
            if digest is not None:
                digests[row_id] = (digest, content_version)
            if not settled:
                continue
            if len(_PAYLOAD_DIGESTS) >= _PAYLOAD_DIGEST_LIMIT:
                _PAYLOAD_DIGESTS.pop(next(iter(_PAYLOAD_DIGESTS)))
            _PAYLOAD_DIGESTS[(row_id, version)] = digest
    return digests


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


def _payload_with_listing_overrides(
    payload: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    """Project durable Admin listing copy without changing runtime content."""
    if not isinstance(overrides, dict) or not overrides:
        return payload
    manifest = dict(_manifest(payload))
    manifest.update({
        key: overrides[key]
        for key in LISTING_OVERRIDE_FIELDS
        if key in overrides
    })
    return {**payload, "manifest": manifest}


def _platform_blueprint_pricing(
    slug: str,
    overrides: Any,
) -> dict[str, Any]:
    """Resolve commerce fields without putting them in portable payloads."""
    pricing: dict[str, Any] = {}
    return pricing


async def seed_platform_blueprints(db: AsyncSession) -> dict[str, str]:
    """Publish current configs while preserving Admin-withdrawn listings.

    Returns {slug: content_version}. Caller commits.
    """
    published: dict[str, str] = {}
    listing_settings = await lock_blueprint_listing_settings(db)
    listing_overrides = blueprint_listing_overrides(listing_settings.value)

    # Removing a config must also withdraw listings already seeded by older
    # releases, including legacy ids. Keep their identity and purchase history.
    retired_rows = (await db.execute(
        select(WorkspaceBlueprint).where(
            WorkspaceBlueprint.entity_id.is_(None),
            WorkspaceBlueprint.slug.in_(RETIRED_PLATFORM_BLUEPRINT_SLUGS),
        ).with_for_update()
    )).scalars().all()
    for row in retired_rows:
        if row.status != BlueprintStatus.ARCHIVED.value or row.share_token is not None:
            row.status = BlueprintStatus.ARCHIVED.value
            row.share_token = None
            logger.info("platform blueprint retired: %s (%s)", row.slug, row.id)

    for payload in get_platform_blueprints():
        manifest = _manifest(payload)
        slug = str(manifest.get("slug") or "").strip()
        if not slug:
            logger.warning("platform blueprint seed: config with no slug, skipped")
            continue

        row_id = platform_blueprint_id(slug)
        row = (await db.execute(
            select(WorkspaceBlueprint)
            .where(WorkspaceBlueprint.id == row_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            # Before stable builtin:<slug> ids, official listings could be
            # published with a generated id. Creating the stable row beside
            # one of those collides with the platform-slug unique index and
            # aborts the publication, leaving every later Blueprint
            # payload stale. Reconcile that legacy row in place; its id may
            # still be referenced by installed workspaces and purchases.
            row = (await db.execute(
                select(WorkspaceBlueprint).where(
                    WorkspaceBlueprint.entity_id.is_(None),
                    WorkspaceBlueprint.slug == slug,
                ).with_for_update()
            )).scalars().first()
            if row is not None:
                logger.info(
                    "platform blueprint legacy row adopted: %s (%s)",
                    slug,
                    row.id,
                )

        # Admin lifecycle changes are durable, including for legacy platform
        # ids. A deployment must not republish or rewrite a withdrawn release
        # merely because its config still ships in the catalogue.
        if row is not None and row.status != BlueprintStatus.PUBLISHED.value:
            continue

        overrides = listing_overrides.get(slug)
        payload = _payload_with_listing_overrides(payload, overrides)
        manifest = _manifest(payload)
        version = await next_release_version(
            db, row.id if row is not None else row_id, payload,
            current_version=getattr(row, "content_version", None) or FIRST_CONTENT_VERSION,
            stored_fingerprint=getattr(row, "content_fingerprint", None),
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
            "status": PUBLISHED,
            **_platform_blueprint_pricing(slug, overrides),
        }

        if row is None:
            row = WorkspaceBlueprint(
                id=row_id,
                entity_id=None,  # the platform owns it
                published_at=datetime.now(timezone.utc),
                **fields,
            )
            db.add(row)
            await record_release(
                db, row.id, payload, version, listing=row,
                payload_version=detect_version(payload),
            )
            logger.info("platform blueprint seeded: %s at %s", slug, version)
        else:
            previous_version = row.content_version
            changed = version != previous_version
            for key, value in fields.items():
                setattr(row, key, value)
            # A recorded version whose content differs means the release
            # history and this row disagree; fail the deployment rather than
            # overwrite a published version.
            await record_release(
                db, row.id, payload, version, listing=row,
                payload_version=detect_version(payload),
            )
            if changed:
                row.published_at = datetime.now(timezone.utc)
                logger.info("platform blueprint republished: %s → %s", slug, version)

        published[slug] = version

    await db.flush()
    return published
