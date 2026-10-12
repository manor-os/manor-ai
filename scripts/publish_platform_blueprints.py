#!/usr/bin/env python3
"""Publish built-in Skills and the platform Blueprint catalogue after a deploy."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from packages.core.blueprints.seed import seed_platform_blueprints
from packages.core.database import async_session
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.services.builtin_skill_loader import seed_builtin_skills


async def publish_platform_blueprints() -> None:
    published_blueprints: list[dict[str, str]] = []

    async with async_session() as db:
        # Built-in Skills ship in the image. K8s disables API startup side
        # effects, so this post-rollout job is where a deploy's Skill changes
        # reach the database; chat turns only verify them once per process.
        # Same transaction as the Blueprints: a failed publish writes nothing.
        builtin_skills = await seed_builtin_skills(db)
        seeded_versions = await seed_platform_blueprints(db)

        if seeded_versions:
            rows = (await db.execute(
                select(WorkspaceBlueprint).where(
                    WorkspaceBlueprint.entity_id.is_(None),
                    WorkspaceBlueprint.slug.in_(seeded_versions),
                )
            )).scalars().all()
            rows_by_slug = {row.slug: row for row in rows}

            seeded_slugs = set(seeded_versions)
            if set(rows_by_slug) != seeded_slugs:
                raise RuntimeError(
                    "published platform Blueprint rows do not match seeded slugs"
                )

            for slug in sorted(seeded_slugs):
                row = rows_by_slug[slug]
                if row.content_version != seeded_versions[slug]:
                    raise RuntimeError(
                        f"published platform Blueprint version mismatch for {slug}"
                    )
                if not row.content_fingerprint:
                    raise RuntimeError(
                        f"published platform Blueprint fingerprint missing for {slug}"
                    )
                published_blueprints.append({
                    "slug": slug,
                    "content_version": row.content_version,
                    "content_fingerprint": row.content_fingerprint,
                })

        await db.commit()

    print(json.dumps({
        "builtin_skills": len(builtin_skills),
        "published_blueprints": published_blueprints,
    }))


if __name__ == "__main__":
    asyncio.run(publish_platform_blueprints())
