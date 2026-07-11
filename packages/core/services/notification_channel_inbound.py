"""Channel-inbound notifications.

When an external contact (Telegram customer, WhatsApp prospect, etc.)
sends a message into a workspace, we want to notify the responsible
staff via *their own* bound channels — not just the in-app bell. This
is the producer for that flow, sitting between ``channel_gateway`` and
``notify()``.

Recipients are resolved with the same precedence as the HITL fan-out:

  1. ``workspace.settings.notification_policy.inbound_notify_user_ids``
     — explicit operator opt-in. Empty list = nobody (suppress).
  2. Fallback to entity owners + admins.

Throttling is per ``(recipient_user_id, conversation_id)``. The first
inbound message on a fresh conversation pings every recipient; follow-up
messages within the window stay quiet so a chatty customer doesn't
spam the operator's Telegram. The window is wide enough to cover a
typical reply turn (15 min) but short enough that a paused conversation
re-pings when the customer comes back the next day.

We deliberately do NOT notify on:
  - HITL ack replies (the channel_gateway pre-step short-circuits before
    this fires)
  - workspace members chatting in their own bound channel (role !=
    "external")
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.notification import Notification
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.notify import notify

logger = logging.getLogger(__name__)


# Cooldown window for "new customer message" notifications. Shorter than
# a typical SLA + longer than a single agent turn, so a fast back-and-
# forth between agent + customer doesn't fire multiple pings.
_THROTTLE_WINDOW = timedelta(minutes=15)

NOTIFICATION_KIND = "channel_inbound_message"


async def notify_channel_inbound_recipients(
    *,
    entity_id: str,
    workspace_id: str | None,
    channel_type: str,
    channel_contact_id: str,
    conversation_id: str,
    sender_name: str | None,
    sender_source_id: str,
    content_preview: str,
) -> int:
    """Page the configured recipients about a new external message.

    Caller is responsible for catching exceptions — failures here are
    logged but never raised back to ``channel_gateway`` so a misconfigured
    notify path can never take down message handling.

    Returns the number of recipients that actually received a fresh
    notification (i.e. were past their cooldown window).
    """
    user_ids = await _resolve_recipients(entity_id=entity_id, workspace_id=workspace_id)
    if not user_ids:
        return 0

    title = _compose_title(sender_name=sender_name, channel_type=channel_type)
    body = _compose_body(content_preview)
    link = f"/conversations/{conversation_id}"
    shared_meta = {
        "conversation_id": conversation_id,
        "channel_type": channel_type,
        "channel_contact_id": channel_contact_id,
        "sender_name": sender_name,
        "sender_source_id": sender_source_id,
    }

    delivered = 0
    async with async_session() as db:
        cutoff = datetime.now(timezone.utc) - _THROTTLE_WINDOW
        for user_id in user_ids:
            if await _recently_notified(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
                cutoff=cutoff,
            ):
                logger.debug(
                    "channel_inbound notify: user=%s already pinged for conv=%s, skip",
                    user_id, conversation_id,
                )
                continue
            try:
                await notify(
                    entity_id=entity_id,
                    user_id=user_id,
                    type=NOTIFICATION_KIND,
                    title=title,
                    body=body,
                    link=link,
                    severity="info",
                    workspace_id=workspace_id,
                    meta=dict(shared_meta),
                )
                delivered += 1
            except Exception:
                logger.warning(
                    "channel_inbound notify: failed for user=%s conv=%s",
                    user_id, conversation_id, exc_info=True,
                )
    return delivered


# ── Recipient resolution ────────────────────────────────────────────────────

async def _resolve_recipients(
    *, entity_id: str, workspace_id: str | None,
) -> list[str]:
    """Look up who should hear about new inbound messages.

    Mirrors ``notify_workspace_hitl_approvers`` semantics: an explicit
    empty list at the workspace level opts the workspace out; a missing
    list falls back to entity owners + admins so we never silently drop
    a customer touch."""
    explicit_empty = False

    if workspace_id:
        async with async_session() as db:
            ws = (await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                )
            )).scalar_one_or_none()
            if ws and isinstance(ws.settings, dict):
                policy = ws.settings.get("notification_policy") or {}
                configured = policy.get("inbound_notify_user_ids")
                if isinstance(configured, list):
                    user_ids = [
                        str(u) for u in configured
                        if isinstance(u, str) and u
                    ]
                    if user_ids:
                        return user_ids
                    explicit_empty = True

    if explicit_empty:
        return []

    async with async_session() as db:
        rows = (await db.execute(
            select(User).where(
                User.entity_id == entity_id,
                User.status == "active",
                User.role.in_(("owner", "admin")),
            )
        )).scalars().all()
        return [u.id for u in rows]


# ── Throttling ─────────────────────────────────────────────────────────────

async def _recently_notified(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    cutoff: datetime,
) -> bool:
    """Has this user already been pinged about this conversation
    within the throttle window? JSONB ``->`` lookup is selective enough
    given the ``user_id + created_at`` index already on the table."""
    row = (await db.execute(
        select(Notification.id).where(
            Notification.user_id == user_id,
            Notification.type == NOTIFICATION_KIND,
            Notification.created_at >= cutoff,
            Notification.meta["conversation_id"].astext == conversation_id,
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


# ── Rendering ───────────────────────────────────────────────────────────────

def _compose_title(*, sender_name: str | None, channel_type: str) -> str:
    name = (sender_name or "").strip() or "Someone"
    return f"New message from {name} ({channel_type})"


def _compose_body(content_preview: str) -> str:
    text = (content_preview or "").strip()
    if not text:
        return "(no content)"
    if len(text) > 280:
        text = text[:280].rstrip() + "…"
    return text
