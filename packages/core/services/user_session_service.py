"""Helpers for recording user browser usage sessions."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.user_session import UserPageViewLog, UserSessionLog

logger = logging.getLogger(__name__)


def _duration_seconds(started_at: datetime | None, now: datetime) -> int:
    if not started_at:
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0, int((now - started_at).total_seconds()))


# ── Path normalisation ──────────────────────────────────────────────

# Collapse path segments that look like an opaque id so analytics
# group across visits to the same conceptual page. Real ids are ULIDs
# (26 char Crockford-base32), UUIDs (36 char hex w/ dashes), or pure
# numeric ints — kill those, leave human paths alone.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"^\d+$")


def _normalise_path(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    # Drop query string + fragment, keep only the pathname.
    path = raw.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    parts = path.split("/")
    out: list[str] = []
    for seg in parts:
        if not seg:
            out.append(seg)
            continue
        if _ULID_RE.match(seg) or _UUID_RE.match(seg) or _NUMERIC_RE.match(seg):
            out.append(":id")
        else:
            out.append(seg)
    normalised = "/".join(out) or "/"
    return normalised[:500]


# ── Geo enrichment helper ───────────────────────────────────────────

async def _enrich_geo(row: UserSessionLog) -> None:
    """Best-effort geo lookup for the session's IP — never blocks WS."""
    if row.country_code or row.country:
        return  # Already enriched.
    if not row.ip_address:
        return
    try:
        from packages.core.services.geo_ip import lookup_geo
        geo = await lookup_geo(row.ip_address)
    except Exception as exc:
        logger.debug("geo enrichment failed: %s", exc)
        return
    if not geo:
        return
    row.country_code = (geo.get("country_code") or None)
    row.country = (geo.get("country") or None)
    row.city = (geo.get("city") or None)
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    row.latitude = lat if isinstance(lat, (int, float)) else None
    row.longitude = lon if isinstance(lon, (int, float)) else None


async def backfill_session_geo(
    db: AsyncSession,
    *,
    limit: int = 1000,
    days: Optional[int] = None,
) -> dict:
    """Resolve geo for existing sessions that have an IP but no country.

    Enrichment normally runs only at session *start* (``_enrich_geo``),
    so rows created before the geo feature shipped — or via the minimal
    SQL fallback, or while the external lookup was unavailable — keep
    their ``ip_address`` but have a NULL ``country_code``. That leaves
    the admin heat-map empty even though the sessions exist. This
    backfills those rows in one pass, de-duplicating the external lookup
    per distinct IP so a busy IP is resolved only once.

    Returns ``{"scanned": int, "updated": int}``.
    """
    from packages.core.services.geo_ip import lookup_geo

    query = (
        select(UserSessionLog)
        .where(
            UserSessionLog.country_code.is_(None),
            UserSessionLog.ip_address.isnot(None),
        )
        .order_by(desc(UserSessionLog.started_at))
        .limit(max(1, limit))
    )
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))
        query = query.where(UserSessionLog.started_at >= since)

    rows = (await db.execute(query)).scalars().all()

    # One lookup per distinct IP — many rows can share an address.
    geo_by_ip: dict[str, Optional[dict]] = {}
    updated = 0
    for row in rows:
        ip = row.ip_address
        if ip not in geo_by_ip:
            try:
                geo_by_ip[ip] = await lookup_geo(ip)
            except Exception as exc:
                logger.debug("geo backfill lookup failed for %s: %s", ip, exc)
                geo_by_ip[ip] = None
        geo = geo_by_ip[ip]
        if not geo:
            continue
        row.country_code = (geo.get("country_code") or None)
        row.country = (geo.get("country") or None)
        row.city = (geo.get("city") or None)
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        row.latitude = lat if isinstance(lat, (int, float)) else None
        row.longitude = lon if isinstance(lon, (int, float)) else None
        if row.country_code:
            updated += 1

    await db.flush()
    return {"scanned": len(rows), "updated": updated}


# ── Page-view segment helper ────────────────────────────────────────

async def _flush_page_segment(
    db: AsyncSession,
    row: UserSessionLog,
    now: datetime,
) -> None:
    """Close the in-progress page segment, if any, and write it out.

    Idempotent: a no-op when no segment is open or when the open
    segment has a zero / negative duration (clock skew, immediate
    re-navigation).
    """
    if not row.current_path or not row.current_path_started_at:
        return
    duration = _duration_seconds(row.current_path_started_at, now)
    if duration > 0:
        db.add(UserPageViewLog(
            id=generate_ulid(),
            entity_id=row.entity_id,
            user_id=row.user_id,
            session_id=row.id,
            path=row.current_path,
            duration_seconds=duration,
            started_at=row.current_path_started_at,
            ended_at=now,
        ))
    row.current_path = None
    row.current_path_started_at = None


async def start_user_session(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source: str = "web",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> UserSessionLog:
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(seconds=90)
    existing = (await db.execute(
        select(UserSessionLog)
        .where(
            UserSessionLog.entity_id == entity_id,
            UserSessionLog.user_id == user_id,
            UserSessionLog.status == "active",
            UserSessionLog.last_seen_at >= active_cutoff,
        )
        .order_by(desc(UserSessionLog.last_seen_at))
        .limit(1)
    )).scalar_one_or_none()
    if existing:
        existing.last_seen_at = now
        existing.duration_seconds = _duration_seconds(existing.started_at, now)
        existing.heartbeat_count = int(existing.heartbeat_count or 0) + 1
        await db.flush()
        return existing

    row = UserSessionLog(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        source=source,
        status="active",
        ip_address=ip_address,
        user_agent=user_agent,
        started_at=now,
        last_seen_at=now,
        duration_seconds=0,
        heartbeat_count=0,
    )
    db.add(row)
    await _enrich_geo(row)
    await db.flush()
    return row


async def start_user_session_compat(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source: str = "web",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Start a session and return its id, with a minimal SQL fallback.

    Production databases can lag behind optional analytics migrations. A
    missing page-view or geo column should not make WebSocket presence look
    offline, so this falls back to the original ``user_session_logs`` columns.
    """
    try:
        row = await start_user_session(
            db,
            entity_id=entity_id,
            user_id=user_id,
            source=source,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return row.id
    except Exception as exc:
        logger.warning("user session ORM start failed; using minimal fallback: %s", exc)
        await db.rollback()
        return await _start_user_session_minimal(
            db,
            entity_id=entity_id,
            user_id=user_id,
            source=source,
            ip_address=ip_address,
            user_agent=user_agent,
        )


async def touch_user_session(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    viewing: Optional[str] = None,
) -> None:
    """Refresh ``last_seen_at`` and (optionally) advance the page tracker.

    ``viewing`` is the client's current ``location.pathname`` (or any
    other resource key it wants to attribute time to). When it changes
    from the row's ``current_path`` we flush the prior segment to
    ``user_page_view_logs`` and open a new one — so dwell time is
    proportional to actual navigation, not heartbeat frequency.
    """
    row = (await db.execute(
        select(UserSessionLog).where(
            UserSessionLog.id == session_id,
            UserSessionLog.entity_id == entity_id,
            UserSessionLog.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        return
    now = datetime.now(timezone.utc)
    row.status = "active"
    row.last_seen_at = now
    row.duration_seconds = _duration_seconds(row.started_at, now)
    row.heartbeat_count = int(row.heartbeat_count or 0) + 1

    if viewing is not None:
        new_path = _normalise_path(viewing)
        if new_path != row.current_path:
            await _flush_page_segment(db, row, now)
            if new_path:
                row.current_path = new_path
                row.current_path_started_at = now

    await db.flush()


async def touch_user_session_compat(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    viewing: Optional[str] = None,
) -> None:
    try:
        await touch_user_session(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
            viewing=viewing,
        )
    except Exception as exc:
        logger.debug("user session ORM touch failed; using minimal fallback: %s", exc)
        await db.rollback()
        await _touch_user_session_minimal(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
        )


async def close_user_session(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
) -> None:
    row = (await db.execute(
        select(UserSessionLog).where(
            UserSessionLog.id == session_id,
            UserSessionLog.entity_id == entity_id,
            UserSessionLog.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        return
    now = datetime.now(timezone.utc)
    row.status = "closed"
    row.last_seen_at = now
    row.ended_at = now
    row.duration_seconds = _duration_seconds(row.started_at, now)
    await _flush_page_segment(db, row, now)
    await db.flush()


async def close_user_session_compat(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
) -> None:
    try:
        await close_user_session(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
        )
    except Exception as exc:
        logger.debug("user session ORM close failed; using minimal fallback: %s", exc)
        await db.rollback()
        await _close_user_session_minimal(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
        )


async def _start_user_session_minimal(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source: str = "web",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Use only the columns created by 20260502_01_user_session_logs."""
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(seconds=90)
    existing_id = (await db.execute(
        text(
            """
            SELECT id
            FROM user_session_logs
            WHERE entity_id = :entity_id
              AND user_id = :user_id
              AND status = 'active'
              AND last_seen_at >= :active_cutoff
            ORDER BY last_seen_at DESC
            LIMIT 1
            """
        ),
        {
            "entity_id": entity_id,
            "user_id": user_id,
            "active_cutoff": active_cutoff,
        },
    )).scalar_one_or_none()
    if existing_id:
        await _touch_user_session_minimal(
            db,
            session_id=str(existing_id),
            entity_id=entity_id,
            user_id=user_id,
            now=now,
        )
        return str(existing_id)

    session_id = generate_ulid()
    await db.execute(
        text(
            """
            INSERT INTO user_session_logs (
                id, entity_id, user_id, source, status,
                ip_address, user_agent, started_at, last_seen_at,
                duration_seconds, heartbeat_count, created_at, updated_at
            )
            VALUES (
                :id, :entity_id, :user_id, :source, 'active',
                :ip_address, :user_agent, :now, :now,
                0, 0, :now, :now
            )
            """
        ),
        {
            "id": session_id,
            "entity_id": entity_id,
            "user_id": user_id,
            "source": source,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "now": now,
        },
    )
    return session_id


async def _touch_user_session_minimal(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    await db.execute(
        text(
            """
            UPDATE user_session_logs
            SET status = 'active',
                last_seen_at = :now,
                heartbeat_count = COALESCE(heartbeat_count, 0) + 1,
                updated_at = :now
            WHERE id = :session_id
              AND entity_id = :entity_id
              AND user_id = :user_id
            """
        ),
        {
            "session_id": session_id,
            "entity_id": entity_id,
            "user_id": user_id,
            "now": now,
        },
    )


async def _close_user_session_minimal(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            """
            UPDATE user_session_logs
            SET status = 'closed',
                last_seen_at = :now,
                ended_at = :now,
                updated_at = :now
            WHERE id = :session_id
              AND entity_id = :entity_id
              AND user_id = :user_id
            """
        ),
        {
            "session_id": session_id,
            "entity_id": entity_id,
            "user_id": user_id,
            "now": now,
        },
    )
