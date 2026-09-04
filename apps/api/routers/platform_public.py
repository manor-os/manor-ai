"""Public read surface for platform-controlled state.

Endpoints here are visible to all authenticated users (NOT just platform
admins) so the user app can render banners, gate features, etc.

The concrete routes below expose only the platform state available in the
current deployment mode.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.feature_flag import FeatureFlag, FeatureFlagOverride
from packages.core.models.user import User
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
