"""DM pairing endpoints — operator-side mint, bot-side redeem.

Two faces:

  POST /api/v1/channel-pairings           operator → mint a code (auth: User JWT)
  POST /api/v1/channel-pairings/redeem    bot webhook → consume code (auth: shared secret)

The redeem endpoint is intentionally NOT user-authenticated — it's
called from each channel's webhook handler (Telegram/WhatsApp/etc.)
which presents a per-channel shared secret instead of a per-user JWT.
The secret is read from the ``MANOR_PAIRING_REDEEM_SECRET`` env var so
multi-tenant SaaS deployments can rotate it without a code change.

In single-tenant OSS deployments the secret defaults to a derived
value tied to the deployment's secret-key — adequate for the typical
"my one bot, my one Manor" case.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.channels import (
    PairingError,
    PairingExpired,
    PairingMismatch,
    create_pairing_code,
    redeem_pairing_code,
)
from packages.core.database import get_db
from packages.core.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channel-pairings", tags=["channel-pairing"])


# ── Models ────────────────────────────────────────────────────────────

class CreatePairingRequest(BaseModel):
    channel_type: str = Field(..., pattern=r"^[a-z_]+$", min_length=1, max_length=30)
    workspace_id: Optional[str] = None
    hint: Optional[str] = Field(None, max_length=255)


class CreatePairingResponse(BaseModel):
    code: str
    expires_at: datetime
    channel_type: str
    workspace_id: Optional[str] = None


class RedeemPairingRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=8)
    channel_type: str
    address: str
    """Channel-native identity — Telegram chat_id, WhatsApp phone, etc."""
    address_kind: str = "chat_id"
    display_name: Optional[str] = None
    agent_id: Optional[str] = None


class RedeemPairingResponse(BaseModel):
    channel_id: str
    entity_id: str
    workspace_id: Optional[str] = None
    type: str
    name: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────

@router.post("", response_model=CreatePairingResponse, status_code=201)
async def create_code(
    req: CreatePairingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Operator UI calls this to get a fresh code to type into the bot."""
    row = await create_pairing_code(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        workspace_id=req.workspace_id,
        channel_type=req.channel_type,
        hint=req.hint,
    )
    await db.commit()
    return CreatePairingResponse(
        code=row.code,
        expires_at=row.expires_at,
        channel_type=row.channel_type,
        workspace_id=row.workspace_id,
    )


@router.post("/redeem", response_model=RedeemPairingResponse)
async def redeem_code(
    req: RedeemPairingRequest,
    db: AsyncSession = Depends(get_db),
    x_pairing_secret: Optional[str] = Header(default=None, alias="X-Pairing-Secret"),
):
    """Bot webhook calls this with the shared secret + the user-typed code."""
    expected = _expected_secret()
    if not expected or not x_pairing_secret or not hmac.compare_digest(
        x_pairing_secret, expected
    ):
        # Constant-time compare; uniform 401 so a probing bot can't
        # tell whether the route exists.
        raise HTTPException(401, "invalid pairing secret")

    try:
        ch = await redeem_pairing_code(
            db,
            code=req.code.strip().upper(),
            channel_type=req.channel_type,
            address=req.address,
            address_kind=req.address_kind,
            display_name=req.display_name,
            agent_id=req.agent_id,
        )
    except PairingExpired as exc:
        raise HTTPException(404, str(exc))
    except PairingMismatch as exc:
        raise HTTPException(409, str(exc))
    except PairingError as exc:
        raise HTTPException(400, str(exc))

    await db.commit()
    return RedeemPairingResponse(
        channel_id=ch.id,
        entity_id=ch.entity_id,
        workspace_id=ch.workspace_id,
        type=ch.type,
        name=ch.name,
    )


# ── Internals ─────────────────────────────────────────────────────────

def _expected_secret() -> Optional[str]:
    """Single-tenant deployments may not have set the env var; we
    fall back to ``SECRET_KEY`` so the route is still functional out
    of the box. Returning None disables the route entirely (raised as
    401 in the handler)."""
    return os.getenv("MANOR_PAIRING_REDEEM_SECRET") or os.getenv("SECRET_KEY")
