"""Helpers for recording user browser usage sessions."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, desc, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.user_session import (
    UserPageViewLog,
    UserSessionLease,
    UserSessionLog,
)

logger = logging.getLogger(__name__)

_USER_SESSION_LEASE_TTL_SECONDS = 90


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

def _apply_geo(row: UserSessionLog, geo: dict) -> None:
    """Apply one resolved Geo-IP payload without performing network I/O."""
    row.country_code = (geo.get("country_code") or None)
    row.country = (geo.get("country") or None)
    row.city = (geo.get("city") or None)
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    row.latitude = lat if isinstance(lat, (int, float)) else None
    row.longitude = lon if isinstance(lon, (int, float)) else None


async def enrich_user_session_geo(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    expected_ip_address: str | None,
    geo: dict,
) -> None:
    """Persist an already-resolved Geo-IP payload in a short transaction."""
    row = (await db.execute(
        select(UserSessionLog).where(
            UserSessionLog.id == session_id,
            UserSessionLog.entity_id == entity_id,
            UserSessionLog.user_id == user_id,
            UserSessionLog.ip_address == expected_ip_address,
        )
    )).scalar_one_or_none()
    if row is None or row.country_code or row.country:
        return
    _apply_geo(row, geo)
    await db.flush()


async def backfill_session_geo(
    db: AsyncSession,
    *,
    limit: int = 1000,
    days: Optional[int] = None,
) -> dict:
    """Resolve geo for existing sessions that have an IP but no country.

    Enrichment normally runs in the WebSocket session-start background task,
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


async def _lock_user_session_scope(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
) -> None:
    """Serialize one Entity/User lifecycle across PostgreSQL API processes."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    await db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(CAST(:session_scope_key AS text), 0))"
        ),
        {"session_scope_key": f"user-session:{entity_id}:{user_id}"},
    )


async def _try_lock_user_session_scope(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
) -> bool:
    """Try to serialize maintenance for one scope without blocking workers."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return True
    acquired = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(CAST(:session_scope_key AS text), 0))"),
        {"session_scope_key": f"user-session:{entity_id}:{user_id}"},
    )
    return bool(acquired)


async def _close_user_session_row(
    db: AsyncSession,
    row: UserSessionLog,
    ended_at: datetime,
) -> None:
    """Close one session at its last trustworthy activity timestamp."""
    if row.status == "closed" and row.ended_at is not None:
        return
    row.status = "closed"
    row.last_seen_at = ended_at
    row.ended_at = ended_at
    row.duration_seconds = _duration_seconds(row.started_at, ended_at)
    await _flush_page_segment(db, row, ended_at)


async def _prune_expired_session_leases(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    now: datetime,
) -> set[str]:
    expired_session_ids = set((await db.execute(
        select(UserSessionLease.session_id).where(
            UserSessionLease.entity_id == entity_id,
            UserSessionLease.user_id == user_id,
            UserSessionLease.expires_at <= now,
        )
    )).scalars())
    if not expired_session_ids:
        return set()

    await db.execute(
        delete(UserSessionLease).where(
            UserSessionLease.entity_id == entity_id,
            UserSessionLease.user_id == user_id,
            UserSessionLease.expires_at <= now,
        )
    )
    await db.flush()

    closed_session_ids: set[str] = set()
    for session_id in expired_session_ids:
        has_live_lease = (await db.execute(
            select(UserSessionLease.id).where(
                UserSessionLease.session_id == session_id,
                UserSessionLease.expires_at > now,
            ).limit(1)
        )).scalar_one_or_none()
        if has_live_lease:
            continue
        row = (await db.execute(
            select(UserSessionLog)
            .where(
                UserSessionLog.id == session_id,
                UserSessionLog.entity_id == entity_id,
                UserSessionLog.user_id == user_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if not row:
            continue
        was_closed = row.status == "closed" and row.ended_at is not None
        ended_at = row.last_seen_at or now
        await _close_user_session_row(db, row, min(ended_at, now))
        if not was_closed:
            closed_session_ids.add(session_id)

    await db.flush()
    return closed_session_ids


async def cleanup_expired_user_session_leases(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Close abandoned sessions, committing each scope to release its lock."""
    now = now or datetime.now(timezone.utc)
    scopes = (
        await db.execute(
            select(UserSessionLease.entity_id, UserSessionLease.user_id)
            .where(UserSessionLease.expires_at <= now)
            .distinct()
            .order_by(UserSessionLease.entity_id, UserSessionLease.user_id)
            .limit(max(1, limit))
        )
    ).all()
    closed_session_ids: set[str] = set()
    for entity_id, user_id in scopes:
        acquired = await _try_lock_user_session_scope(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
        if not acquired:
            # A lifecycle request owns this scope. End this transaction so the
            # maintenance worker can move on and retry the expired lease later.
            await db.rollback()
            continue
        closed_session_ids.update(
            await _prune_expired_session_leases(
                db,
                entity_id=entity_id,
                user_id=user_id,
                now=now,
            )
        )
        # The advisory lock is transaction-scoped. Commit each
        # independent scope so a large maintenance batch never accumulates up
        # to ``limit`` locks and blocks reconnecting API requests until the end.
        await db.commit()
    return len(closed_session_ids)


def _session_lease_expiry(now: datetime) -> datetime:
    return now + timedelta(seconds=_USER_SESSION_LEASE_TTL_SECONDS)


async def start_user_session(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source: str = "web",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    lease_id: str | None = None,
) -> UserSessionLog:
    await _lock_user_session_scope(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(seconds=90)
    await _prune_expired_session_leases(
        db,
        entity_id=entity_id,
        user_id=user_id,
        now=now,
    )

    if lease_id:
        previous_lease = (await db.execute(
            select(UserSessionLease)
            .where(UserSessionLease.id == lease_id)
            .with_for_update()
        )).scalar_one_or_none()
        if previous_lease:
            if (
                previous_lease.entity_id != entity_id
                or previous_lease.user_id != user_id
            ):
                raise ValueError("User session lease belongs to another scope")
            leased_session = (await db.execute(
                select(UserSessionLog)
                .where(
                    UserSessionLog.id == previous_lease.session_id,
                    UserSessionLog.entity_id == entity_id,
                    UserSessionLog.user_id == user_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if leased_session:
                previous_lease.expires_at = _session_lease_expiry(now)
                leased_session.status = "active"
                leased_session.ended_at = None
                leased_session.last_seen_at = now
                leased_session.duration_seconds = _duration_seconds(
                    leased_session.started_at,
                    now,
                )
                leased_session.heartbeat_count = int(
                    leased_session.heartbeat_count or 0
                ) + 1
                await db.flush()
                return leased_session
            await db.execute(
                delete(UserSessionLease).where(UserSessionLease.id == lease_id)
            )

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
        .with_for_update()
    )).scalar_one_or_none()
    if existing:
        existing.status = "active"
        existing.ended_at = None
        existing.last_seen_at = now
        existing.duration_seconds = _duration_seconds(existing.started_at, now)
        existing.heartbeat_count = int(existing.heartbeat_count or 0) + 1
        if lease_id:
            db.add(UserSessionLease(
                id=lease_id,
                entity_id=entity_id,
                user_id=user_id,
                session_id=existing.id,
                expires_at=_session_lease_expiry(now),
            ))
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
    # ``UserSessionLease`` stores only the scalar foreign key, so SQLAlchemy
    # has no relationship edge that would order these two pending INSERTs.
    # Persist the parent row first to keep a fresh session + lease atomic
    # without relying on mapper flush ordering.
    await db.flush()
    if lease_id:
        db.add(UserSessionLease(
            id=lease_id,
            entity_id=entity_id,
            user_id=user_id,
            session_id=row.id,
            expires_at=_session_lease_expiry(now),
        ))
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
    lease_id: str | None = None,
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
            lease_id=lease_id,
        )
        return row.id
    except ProgrammingError as exc:
        await db.rollback()
        if lease_id is not None:
            raise
        logger.warning(
            "user session ORM start hit a schema mismatch; using minimal fallback: %s",
            exc,
        )
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
    lease_id: str | None = None,
) -> bool:
    """Refresh ``last_seen_at`` and (optionally) advance the page tracker.

    ``viewing`` is the client's current ``location.pathname`` (or any
    other resource key it wants to attribute time to). When it changes
    from the row's ``current_path`` we flush the prior segment to
    ``user_page_view_logs`` and open a new one — so dwell time is
    proportional to actual navigation, not heartbeat frequency.
    """
    if lease_id:
        await _lock_user_session_scope(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
    now = datetime.now(timezone.utc)
    if lease_id:
        await _prune_expired_session_leases(
            db,
            entity_id=entity_id,
            user_id=user_id,
            now=now,
        )

    session_query = select(UserSessionLog).where(
        UserSessionLog.id == session_id,
        UserSessionLog.entity_id == entity_id,
        UserSessionLog.user_id == user_id,
    )
    if lease_id:
        session_query = session_query.with_for_update()
    row = (await db.execute(session_query)).scalar_one_or_none()
    if not row:
        return False

    if lease_id:
        lease = (await db.execute(
            select(UserSessionLease)
            .where(UserSessionLease.id == lease_id)
            .with_for_update()
        )).scalar_one_or_none()
        if lease and (
            lease.entity_id != entity_id
            or lease.user_id != user_id
            or lease.session_id != session_id
        ):
            logger.warning(
                "Ignoring mismatched user session lease %s for %s/%s",
                lease_id,
                entity_id,
                user_id,
            )
            return False
        if lease:
            lease.expires_at = _session_lease_expiry(now)
        else:
            # A lease can disappear after a Pod stalls past the TTL or after
            # another lifecycle path closes it. Re-creating it against this
            # caller's cached session id could revive an obsolete session in
            # parallel with the user's current one. Let the WebSocket manager
            # discard its local mapping and re-enter the serialized start path.
            return False
    row.status = "active"
    row.ended_at = None
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
    return True


async def touch_user_session_compat(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    viewing: Optional[str] = None,
    lease_id: str | None = None,
) -> bool:
    try:
        return await touch_user_session(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
            viewing=viewing,
            lease_id=lease_id,
        )
    except ProgrammingError as exc:
        await db.rollback()
        if lease_id is not None:
            logger.debug(
                "user session lease touch unavailable after schema mismatch: %s",
                exc,
            )
            return False
        logger.debug(
            "user session ORM touch hit a schema mismatch; using minimal fallback: %s",
            exc,
        )
        return await _touch_user_session_minimal(
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
    lease_id: str | None = None,
) -> None:
    if lease_id:
        await _lock_user_session_scope(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
    now = datetime.now(timezone.utc)
    if lease_id:
        await _prune_expired_session_leases(
            db,
            entity_id=entity_id,
            user_id=user_id,
            now=now,
        )
        lease = (await db.execute(
            select(UserSessionLease)
            .where(UserSessionLease.id == lease_id)
            .with_for_update()
        )).scalar_one_or_none()
        if lease and (
            lease.entity_id != entity_id
            or lease.user_id != user_id
            or lease.session_id != session_id
        ):
            logger.warning(
                "Ignoring mismatched user session lease %s for %s/%s",
                lease_id,
                entity_id,
                user_id,
            )
            return
        if lease:
            await db.delete(lease)
        await db.flush()
        remaining_lease = (await db.execute(
            select(UserSessionLease.id).where(
                UserSessionLease.session_id == session_id,
                UserSessionLease.expires_at > now,
            ).limit(1)
        )).scalar_one_or_none()
        if remaining_lease:
            return

    row = (await db.execute(
        select(UserSessionLog).where(
            UserSessionLog.id == session_id,
            UserSessionLog.entity_id == entity_id,
            UserSessionLog.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        return
    await _close_user_session_row(db, row, now)
    await db.flush()


async def close_user_session_compat(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    lease_id: str | None = None,
) -> None:
    try:
        await close_user_session(
            db,
            session_id=session_id,
            entity_id=entity_id,
            user_id=user_id,
            lease_id=lease_id,
        )
    except ProgrammingError as exc:
        await db.rollback()
        if lease_id is not None:
            logger.debug(
                "user session lease close unavailable after schema mismatch: %s",
                exc,
            )
            return
        logger.debug(
            "user session ORM close hit a schema mismatch; using minimal fallback: %s",
            exc,
        )
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
    await _lock_user_session_scope(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
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
) -> bool:
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        text(
            """
            UPDATE user_session_logs
            SET status = 'active',
                ended_at = NULL,
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
    return bool(result.rowcount)


async def _close_user_session_minimal(
    db: AsyncSession,
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
) -> None:
    await _lock_user_session_scope(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
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
