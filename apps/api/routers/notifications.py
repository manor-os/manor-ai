"""Notification endpoints — list, create, mark read, delete + preferences."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import (
    EVENT_CATALOG,
    SUPPORTED_CHANNELS,
)
from packages.core.database import get_db
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.user import User
from packages.core.services.notification_service import (
    list_notifications, create_notification, mark_read,
    mark_all_read, delete_notification, count_unread,
)
from packages.core.services.auth_service import list_user_memberships
from packages.core.services.settings_service import (
    get_user_preferences,
    update_user_preferences,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])

_CONNECTED_NOTIFICATION_CHANNELS: tuple[str, ...] = tuple(
    channel for channel in SUPPORTED_CHANNELS if channel != "inapp"
)


# ── Schemas ──

class NotificationResponse(BaseModel):
    id: str
    entity_id: str
    user_id: str
    type: str
    title: str | None = None
    content: str | None = None
    metadata: dict = {}
    is_read: bool = False
    read_at: str | None = None
    created_at: str | None = None


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread_count: int


class NotificationCreateRequest(BaseModel):
    type: str
    title: str
    body: str | None = None
    link: str | None = None


class MarkAllReadResponse(BaseModel):
    count: int


def _to_response(n) -> NotificationResponse:
    return NotificationResponse(
        id=n.id,
        entity_id=n.entity_id,
        user_id=n.user_id,
        type=n.type,
        title=n.title,
        content=n.content,
        metadata=n.meta or {},
        is_read=n.read_at is not None,
        read_at=n.read_at.isoformat() if n.read_at else None,
        created_at=n.created_at.isoformat() if n.created_at else None,
    )


async def _notification_entity_scope(db: AsyncSession, user: User) -> list[str]:
    rows = await list_user_memberships(db, user)
    entity_ids = [membership.entity_id for membership, _entity in rows]
    if user.entity_id not in entity_ids:
        entity_ids.append(user.entity_id)
    return entity_ids


# ── Endpoints ──

@router.get("", response_model=NotificationListResponse)
async def list_my_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_ids = await _notification_entity_scope(db, user)
    items, total = await list_notifications(
        db, user.entity_id, user.id,
        entity_ids=entity_ids,
        unread_only=unread_only, limit=limit, offset=offset,
    )
    unread = await count_unread(db, user.entity_id, user.id, entity_ids=entity_ids)
    return NotificationListResponse(
        items=[_to_response(n) for n in items],
        total=total,
        unread_count=unread,
    )


@router.post("", response_model=NotificationResponse, status_code=201)
async def create_new_notification(
    req: NotificationCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notif = await create_notification(
        db, user.entity_id, user.id,
        type=req.type, title=req.title,
        body=req.body, link=req.link,
    )
    return _to_response(notif)


@router.post("/read-all", response_model=MarkAllReadResponse)
async def mark_all_as_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_ids = await _notification_entity_scope(db, user)
    count = await mark_all_read(db, user.entity_id, user.id, entity_ids=entity_ids)
    return MarkAllReadResponse(count=count)


@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_one_as_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    success = await mark_read(db, notification_id, user.id)
    if not success:
        raise HTTPException(404, "Notification not found")
    # Re-fetch to return updated state
    from sqlalchemy import select
    from packages.core.models.notification import Notification
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )
    notif = result.scalar_one()
    return _to_response(notif)


@router.delete("/{notification_id}", status_code=204)
async def delete_one_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    success = await delete_notification(db, notification_id, user.id)
    if not success:
        raise HTTPException(404, "Notification not found")


# ── Preferences ───────────────────────────────────────────────────────────


class EventDescriptorResponse(BaseModel):
    kind: str
    category: str
    severity: str
    label: str
    description: str


class ConnectedChannel(BaseModel):
    channel_type: str
    channel_config_id: str
    contact_id: str
    display_name: str | None = None
    source_id: str
    last_seen_at: str | None = None


class NotificationPreferencesResponse(BaseModel):
    """Snapshot of the user's notification routing config + the catalog
    the UI needs to render the (event × channel) matrix."""
    default_channels: list[str] = Field(default_factory=list)
    by_kind: dict[str, dict[str, Any]] = Field(default_factory=dict)
    quiet_hours: dict[str, Any] | None = None
    supported_channels: list[str] = Field(default_factory=list)
    configured_channels: list[str] = Field(default_factory=list)
    event_catalog: list[EventDescriptorResponse] = Field(default_factory=list)
    connected_channels: list[ConnectedChannel] = Field(default_factory=list)


class NotificationPreferencesUpdate(BaseModel):
    """Partial update. Send only the fields you want to change; omitted
    fields are preserved. To clear a kind override pass it as ``null``."""
    default_channels: list[str] | None = None
    by_kind: dict[str, dict[str, Any] | None] | None = None
    quiet_hours: dict[str, Any] | None = None


def _normalise_channels(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v in SUPPORTED_CHANNELS and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _validate_quiet_hours(qh: dict[str, Any] | None) -> dict[str, Any] | None:
    if qh is None:
        return None
    if not isinstance(qh, dict):
        raise HTTPException(400, "quiet_hours must be an object")
    if qh.get("from") is None or qh.get("to") is None:
        # Disable quiet hours by clearing the block.
        return None
    return {
        "tz": str(qh.get("tz") or "UTC"),
        "from": str(qh.get("from")),
        "to": str(qh.get("to")),
    }


async def _load_connected_channels(db: AsyncSession, user: User) -> list[ConnectedChannel]:
    rows = (await db.execute(
        select(ChannelContact).where(
            ChannelContact.entity_id == user.entity_id,
            ChannelContact.user_id == user.id,
            ChannelContact.status == "active",
            ChannelContact.channel_type.in_(_CONNECTED_NOTIFICATION_CHANNELS),
        ).order_by(ChannelContact.last_seen_at.desc().nullslast())
    )).scalars().all()
    connected = [
        ConnectedChannel(
            channel_type=row.channel_type,
            channel_config_id=row.channel_config_id,
            contact_id=row.id,
            display_name=row.display_name,
            source_id=row.source_id,
            last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else None,
        )
        for row in rows
    ]
    has_email = any(row.channel_type == "email" for row in rows)
    if user.email and not has_email:
        connected.insert(0, ConnectedChannel(
            channel_type="email",
            channel_config_id="registered_email",
            contact_id=f"registered_email:{user.id}",
            display_name="Account email",
            source_id=user.email,
            last_seen_at=None,
        ))
    return connected


async def _load_configured_notification_channels(db: AsyncSession, user: User) -> list[str]:
    rows = (await db.execute(
        select(ChannelConfig.channel_type)
        .where(
            ChannelConfig.entity_id == user.entity_id,
            ChannelConfig.status == "active",
            ChannelConfig.channel_type.in_(_CONNECTED_NOTIFICATION_CHANNELS),
        )
        .order_by(ChannelConfig.channel_type.asc())
    )).scalars().all()
    seen: set[str] = set()
    out: list[str] = []
    for channel in rows:
        if channel in SUPPORTED_CHANNELS and channel not in seen:
            seen.add(channel)
            out.append(channel)
    return out


@router.get("/preferences", response_model=NotificationPreferencesResponse)
async def get_notification_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the user's notification routing prefs alongside the catalog
    of known event kinds and the channels they're currently linked to —
    so the UI can render the whole matrix from a single request."""
    prefs = await get_user_preferences(db, user.id)
    notif = (prefs or {}).get("notifications") or {}
    connected = await _load_connected_channels(db, user)
    configured = await _load_configured_notification_channels(db, user)
    return NotificationPreferencesResponse(
        default_channels=_normalise_channels(notif.get("default_channels") or []),
        by_kind={k: v for k, v in (notif.get("by_kind") or {}).items() if isinstance(v, dict)},
        quiet_hours=notif.get("quiet_hours") if isinstance(notif.get("quiet_hours"), dict) else None,
        supported_channels=list(SUPPORTED_CHANNELS),
        configured_channels=configured,
        event_catalog=[EventDescriptorResponse(**e) for e in EVENT_CATALOG],
        connected_channels=connected,
    )


# ── Channel link tokens (end-user "claim Telegram as me") ─────────────────


class StartChannelLinkRequest(BaseModel):
    channel_type: str


class StartChannelLinkResponse(BaseModel):
    token: str
    channel_type: str
    expires_at: str
    deep_link: str | None = None
    bot_username: str | None = None
    instructions: str


@router.post("/preferences/link/start", response_model=StartChannelLinkResponse)
async def start_channel_link(
    req: StartChannelLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a short-lived token + deep link so the user can bind their
    Telegram (or future channels) to their Manor account by sending
    ``/start <token>`` to the bot."""
    from packages.core.services.notification_channel_linking import start_link

    try:
        result = await start_link(
            db,
            user_id=user.id,
            entity_id=user.entity_id,
            channel_type=req.channel_type,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await db.commit()
    return StartChannelLinkResponse(
        token=result.token,
        channel_type=result.channel_type,
        expires_at=result.expires_at.isoformat(),
        deep_link=result.deep_link,
        bot_username=result.bot_username,
        instructions=result.instructions,
    )


class ChannelLinkStatusResponse(BaseModel):
    status: str                              # pending | claimed | expired | not_found
    contact_id: str | None = None
    claimed_at: str | None = None


@router.get("/preferences/link/{token}", response_model=ChannelLinkStatusResponse)
async def get_channel_link_status(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll the claim status of a previously-issued token. The UI calls
    this every few seconds while showing the deep-link / instructions
    so it can flip to "Connected" without making the user reload."""
    from datetime import datetime, timezone

    from packages.core.models.channel import ChannelLinkToken

    row = (await db.execute(
        select(ChannelLinkToken).where(
            ChannelLinkToken.token == token,
            ChannelLinkToken.user_id == user.id,
        )
    )).scalar_one_or_none()
    if row is None:
        return ChannelLinkStatusResponse(status="not_found")

    if row.claimed_at is not None:
        return ChannelLinkStatusResponse(
            status="claimed",
            contact_id=row.claimed_contact_id,
            claimed_at=row.claimed_at.isoformat(),
        )

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return ChannelLinkStatusResponse(status="expired")
    return ChannelLinkStatusResponse(status="pending")


@router.put("/preferences", response_model=NotificationPreferencesResponse)
async def update_notification_preferences(
    req: NotificationPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial-merge update: only provided fields change. Send
    ``by_kind`` with a ``null`` value for a kind to drop that override."""
    prefs = await get_user_preferences(db, user.id)
    notif: dict[str, Any] = dict((prefs or {}).get("notifications") or {})

    if req.default_channels is not None:
        notif["default_channels"] = _normalise_channels(req.default_channels)

    if req.by_kind is not None:
        existing_by_kind: dict[str, Any] = dict(notif.get("by_kind") or {})
        for kind, value in req.by_kind.items():
            if value is None:
                existing_by_kind.pop(kind, None)
                continue
            if not isinstance(value, dict):
                raise HTTPException(400, f"by_kind[{kind}] must be an object or null")
            cleaned: dict[str, Any] = {}
            if "channels" in value:
                cleaned["channels"] = _normalise_channels(list(value.get("channels") or []))
            if "enabled" in value:
                cleaned["enabled"] = bool(value.get("enabled"))
            if "bypass_quiet_hours" in value:
                cleaned["bypass_quiet_hours"] = bool(value.get("bypass_quiet_hours"))
            existing_by_kind[kind] = cleaned
        notif["by_kind"] = existing_by_kind

    if req.quiet_hours is not None:
        validated = _validate_quiet_hours(req.quiet_hours)
        if validated is None:
            notif.pop("quiet_hours", None)
        else:
            notif["quiet_hours"] = validated

    await update_user_preferences(db, user.id, {"notifications": notif})
    await db.commit()
    return await get_notification_preferences(user=user, db=db)
