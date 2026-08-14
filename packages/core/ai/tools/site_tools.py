"""publish_site — publish a static website folder/file to a public URL."""
from __future__ import annotations

import json
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

        async with async_session() as session:
            result = await sp.publish(
                session, entity_id=entity_id, rel_path=path, name=name
            )
    except sp.SitePublishError as e:
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
