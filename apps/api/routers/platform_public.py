"""Public read surface for platform-controlled state.

Endpoints here are visible to all authenticated users (NOT just platform
admins) so the user app can render banners, gate features, etc.

Two routes for now:
  * ``GET /api/v1/platform/announcements/active`` — banners the user
    should see right now, filtered by audience.
  * ``GET /api/v1/platform/flags`` — feature-flag values for the
    current user/entity, so client code can branch on them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.feature_flag import FeatureFlag, FeatureFlagOverride
from packages.core.models.platform_announcement import (
    PlatformAnnouncement,
    PlatformAnnouncementAudience,
    PlatformAnnouncementDismissal,
)
from packages.core.models.user import Entity, User
from packages.core.services.feature_flags import is_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platform", tags=["platform-public"])


# ── Signup configuration (no auth — read by the public signup form) ──

class SignupConfigResponse(BaseModel):
    invitation_code_required: bool
    """Whether ``POST /auth/register`` will reject signups that don't
    supply a valid invitation_code. Read from the
    ``require_invitation_code`` feature flag, default False."""


@router.get("/signup-config", response_model=SignupConfigResponse)
async def signup_config(db: AsyncSession = Depends(get_db)):
    """Tell the unauthenticated signup form whether it needs to render
    the invitation-code field. NO auth — must be reachable before the
    user has any credential."""
    try:
        from packages.core.services.invitation_codes import is_required
        return SignupConfigResponse(
            invitation_code_required=await is_required(db),
        )
    except Exception:
        # Fail open — don't block signups if feature-flag tables
        # are missing or DB is unreachable.
        return SignupConfigResponse(invitation_code_required=False)


# ── Announcements (active for this user) ─────────────────────────────

class PublicAnnouncement(BaseModel):
    id: str
    title: str
    body_md: str
    severity: str
    starts_at: Optional[str]
    ends_at: Optional[str]


@router.get("/announcements/active", response_model=list[PublicAnnouncement])
async def active_announcements(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Announcements the current user should see right now.

    Filters:
      * ``status == 'active'``
      * ``starts_at`` is null or in the past
      * ``ends_at`` is null or in the future
      * ``show_in_app`` is true
      * audience matches: ``all`` always wins; ``user:<id>`` matches the
        current user; ``tenant:<id>`` if matches user's entity;
        ``plan:<id>`` if matches entity's plan; ``trial`` if entity has a
        future ``trial_ends_at``. Audience terms are stored in the child
        table ``platform_announcement_audiences`` (one row per term).

    Sorted by severity (critical > warning > info), then most recent.
    """
    now = datetime.now(timezone.utc)

    entity = (await db.execute(
        select(Entity).where(Entity.id == user.entity_id)
    )).scalar_one_or_none()

    audiences = ["all", f"user:{user.id}"]
    if entity:
        audiences.append(f"tenant:{entity.id}")
        if entity.plan_id:
            audiences.append(f"plan:{entity.plan_id}")
        if entity.trial_ends_at and entity.trial_ends_at > now:
            audiences.append("trial")

    dismissed_ids = set((await db.execute(
        select(PlatformAnnouncementDismissal.announcement_id).where(
            PlatformAnnouncementDismissal.user_id == user.id,
        )
    )).scalars().all())

    rows = list((await db.execute(
        select(PlatformAnnouncement)
        .join(
            PlatformAnnouncementAudience,
            PlatformAnnouncementAudience.announcement_id == PlatformAnnouncement.id,
        )
        .where(
            PlatformAnnouncement.status == "active",
            PlatformAnnouncement.show_in_app == True,  # noqa: E712
            PlatformAnnouncementAudience.term.in_(audiences),
            or_(
                PlatformAnnouncement.starts_at.is_(None),
                PlatformAnnouncement.starts_at <= now,
            ),
            or_(
                PlatformAnnouncement.ends_at.is_(None),
                PlatformAnnouncement.ends_at > now,
            ),
        )
        .distinct()
    )).scalars().all())

    if dismissed_ids:
        rows = [r for r in rows if r.id not in dismissed_ids]

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    rows.sort(key=lambda r: (
        severity_rank.get(r.severity, 99),
        -(r.created_at.timestamp() if r.created_at else 0),
    ))

    return [
        PublicAnnouncement(
            id=r.id, title=r.title, body_md=r.body_md, severity=r.severity,
            starts_at=r.starts_at.isoformat() if r.starts_at else None,
            ends_at=r.ends_at.isoformat() if r.ends_at else None,
        )
        for r in rows
    ]


@router.post("/announcements/{announcement_id}/dismiss", status_code=204)
async def dismiss_announcement(
    announcement_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist that this user dismissed this announcement so the banner
    stops surfacing on every device. Idempotent — re-dismissing is a
    no-op via ON CONFLICT DO NOTHING."""
    exists = (await db.execute(
        select(PlatformAnnouncement.id).where(
            PlatformAnnouncement.id == announcement_id,
        )
    )).scalar_one_or_none()
    if not exists:
        raise HTTPException(404, "Announcement not found")

    stmt = pg_insert(PlatformAnnouncementDismissal).values(
        announcement_id=announcement_id,
        user_id=user.id,
    ).on_conflict_do_nothing(
        index_elements=["announcement_id", "user_id"],
    )
    await db.execute(stmt)
    await db.commit()


# ── Feature flags (resolved for this user) ────────────────────────────

class PublicFlagsResponse(BaseModel):
    flags: dict[str, bool]


@router.get("/flags", response_model=PublicFlagsResponse)
async def my_flags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolved on/off for every active feature flag, for THIS user
    and entity. Cheap — single query for the registry, then in-memory
    evaluation against any matching overrides."""
    flag_rows = list((await db.execute(
        select(FeatureFlag).where(FeatureFlag.status == "active")
    )).scalars().all())
    if not flag_rows:
        return PublicFlagsResponse(flags={})

    out: dict[str, bool] = {}
    for f in flag_rows:
        out[f.key] = await is_enabled(
            db, f.key,
            entity_id=user.entity_id, user_id=user.id,
            fallback=bool(f.default_enabled),
        )
    return PublicFlagsResponse(flags=out)
