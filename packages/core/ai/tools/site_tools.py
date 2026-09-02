"""publish_site — publish a static website folder/file to a public URL."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

PUBLISH_SITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "publish_site",
        "description": (
            "Publish a static website folder (its root must contain index.html) or a "
            "single .html file to a public URL. Snapshot semantics: later file edits go "
            "live only after calling publish_site again for the same path. Only "
            "browser-ready static files are served (HTML/CSS/JS/images/fonts/media); "
            "JSX/TSX/Vue sources, package.json and other build-tooling files are "
            "excluded from the published snapshot — generate browser-ready code and "
            "reference assets with RELATIVE paths only (never /api/v1/fs/... URLs, "
            "which fail the publish link check). Returns the public URL, the snapshot "
            "revision, and any files excluded from publishing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Entity-relative path of the folder or .html file to publish, "
                        "e.g. the bundle_path returned by generate_file(kind='code')"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Human-readable site name (display only; the subdomain is "
                        "system-generated and cannot be chosen)"
                    ),
                },
            },
            "required": ["path", "name"],
        },
    },
}


async def _publish_site_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    from packages.core.config import get_settings
    from packages.core.services import site_publisher as sp

    path = str(kwargs.get("path") or "").strip()
    name = str(kwargs.get("name") or "").strip() or "Site"
    if not path:
        return json.dumps({"published": False, "error": "path is required"})
    if not entity_id:
        return json.dumps({"published": False, "error": "no entity context"})
    if not (get_settings().MANOR_SITES_DOMAIN or "").strip():
        # No sites domain means nothing published here is reachable; say so
        # instead of reporting a success with an empty URL.
        return json.dumps({
            "published": False,
            "error": (
                "site hosting is not configured on this deployment "
                "(MANOR_SITES_DOMAIN is unset), so published sites would have no "
                "reachable address. Ask an operator to configure it."
            ),
        })
    try:
        from packages.core.database import async_session
        from packages.core.models.site import Site
        from packages.core.models.user import User
        from packages.core.services.auth_service import get_user_membership
        from packages.core.services.site_access import (
            SitePublishAccessDenied,
            require_site_publish_access,
            user_can_manage_site,
        )
        from sqlalchemy import select

        async with async_session() as session:
            target = sp.resolve_publish_target(entity_id, path)
            if target is None:
                raise sp.SitePublishError(
                    f"{path!r} is not publishable: publish a folder whose root contains "
                    "index.html, or a single .html file"
                )
            user = await session.scalar(
                select(User).where(User.id == user_id, User.status == "active")
            )
            if user is None:
                raise SitePublishAccessDenied(
                    "An active user context is required to publish this site"
                )
            membership = await get_user_membership(
                session,
                user=user,
                entity_id=entity_id,
            )
            if membership is None or membership.status != "active":
                raise SitePublishAccessDenied(
                    "An active entity membership is required to publish this site"
                )
            actor = SimpleNamespace(
                id=user.id,
                entity_id=membership.entity_id,
                role=membership.role,
                email=user.email,
                display_name=user.display_name,
            )
            prepared = await asyncio.to_thread(
                sp.prepare_publication,
                entity_id,
                target,
            )
            try:
                await require_site_publish_access(
                    session,
                    user=actor,
                    target=target,
                    included_paths=prepared.included_source_paths,
                )
                existing_site = await session.scalar(
                    select(Site).where(
                        Site.entity_id == entity_id,
                        Site.source_path == target.root_rel,
                    )
                )
                if existing_site is not None and not await user_can_manage_site(
                    session,
                    user=actor,
                    site=existing_site,
                ):
                    raise SitePublishAccessDenied(
                        "Site management access is required to republish this site"
                    )
                result = await sp.publish(
                    session,
                    entity_id=entity_id,
                    rel_path=target.root_rel,
                    name=name,
                    created_by_user_id=user.id,
                    manage_as_user=actor,
                    prepared=prepared,
                )
                prepared = None
            finally:
                if prepared is not None:
                    await asyncio.to_thread(prepared.cleanup)
    except (OSError, sp.SitePublishError, PermissionError) as e:
        return json.dumps({"published": False, "error": str(e)})

    domain = (get_settings().MANOR_SITES_DOMAIN or "").strip().lower()
    url = f"https://{result.site.slug}.{domain}" if domain else ""
    payload: dict[str, Any] = {
        "published": True,
        "url": url,
        "site_id": result.site.id,
        "slug": result.site.slug,
        "revision": result.site.revision,
        "excluded_files": [f.rel for f in result.excluded],
    }
    if result.excluded:
        payload["note"] = (
            "excluded_files were not published (not browser-ready static assets); "
            "the live site is a snapshot — call publish_site again after edits"
        )
    return json.dumps(payload)


def get_tools():
    return [(PUBLISH_SITE_SCHEMA, _publish_site_handler)]
