"""Read-side site analytics shared by the Sites API and agent tools."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.site import Site, SiteEvent

TOP_PAGES = 10
_PAGE_SCAN_LIMIT = 20_000


def page_address(path: str | None) -> str:
    """One address per page: no query string, decoded, no trailing slash except '/'."""
    address = unquote(urlsplit(path or "/").path or "/")
    if len(address) > 1:
        address = address.rstrip("/") or "/"
    return address


async def site_analytics_summary(db: AsyncSession, site: Site, *, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(SiteEvent.event_type, func.count(SiteEvent.id))
        .where(SiteEvent.site_id == site.id, SiteEvent.created_at >= since)
        .group_by(SiteEvent.event_type)
    )).all()
    counts = {event_type: int(count) for event_type, count in rows}
    unique_sessions = await db.scalar(
        select(func.count(func.distinct(SiteEvent.session_id))).where(
            SiteEvent.site_id == site.id,
            SiteEvent.created_at >= since,
            SiteEvent.session_id.is_not(None),
        )
    )
    view_rows = (await db.execute(
        select(SiteEvent.path, SiteEvent.properties)
        .where(
            SiteEvent.site_id == site.id,
            SiteEvent.created_at >= since,
            SiteEvent.event_type == "page_view",
            SiteEvent.path.is_not(None),
        )
        .order_by(SiteEvent.created_at.desc())
        .limit(_PAGE_SCAN_LIMIT)
    )).all()
    views: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for path, properties in view_rows:
        target = missing if str((properties or {}).get("status") or "") == "404" else views
        target[page_address(path)] += 1
    not_found_views = sum(missing.values())
    return {
        "site_id": site.id,
        "days": days,
        "analytics_enabled": bool((site.connections or {}).get("analytics_enabled", True)),
        # Visits that landed on the site's 404 page are not page views.
        "page_views": max(counts.get("page_view", 0) - not_found_views, 0),
        "not_found_views": not_found_views,
        "unique_sessions": int(unique_sessions or 0),
        "interactions": counts.get("interaction", 0),
        "form_submissions": counts.get("form_submit", 0),
        "chat_opens": counts.get("chat_open", 0),
        "top_pages": [
            {"path": path, "views": count} for path, count in views.most_common(TOP_PAGES)
        ],
        # Addresses visitors tried that do not exist: broken links or old URLs.
        "top_missing_pages": [
            {"path": path, "views": count} for path, count in missing.most_common(TOP_PAGES)
        ],
    }
