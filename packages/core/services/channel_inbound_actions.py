from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select

from packages.core.database import async_session
from packages.core.models.channel import ChannelContact
from packages.core.models.notification import NotificationDelivery
from packages.core.services.notification_callbacks import dispatch_callback, match_action
from packages.core.services.notification_channel_linking import claim_token, extract_start_token


@dataclass(frozen=True)
class ChannelInboundAction:
    result: dict[str, Any]
    ack_message: str | None = None


async def maybe_claim_channel_link_token(
    *,
    channel_contact_id: str,
    content: str,
) -> ChannelInboundAction | None:
    token = extract_start_token(content)
    if token is None:
        return None

    async with async_session() as db:
        contact = (await db.execute(
            select(ChannelContact).where(ChannelContact.id == channel_contact_id)
        )).scalar_one_or_none()
        if contact is None:
            return None
        outcome = await claim_token(db, token=token, contact=contact)
        await db.commit()

    if outcome.ok:
        ack = (
            "✅ Linked! This Telegram account is now connected to your "
            "Manor profile. You'll receive your notifications here."
        )
    else:
        reasons = {
            "token_not_found": "❌ That code isn't valid — generate a new one in your Notification settings.",
            "token_already_used": "❌ That code was already used. Generate a new one if you need to relink.",
            "token_expired": "⏱  That code expired. Generate a new one in your Notification settings.",
            "channel_type_mismatch": "❌ That code is for a different channel type.",
            "entity_mismatch": "❌ That code belongs to a different organization.",
            "user_inactive": "❌ The Manor account this code was tied to is no longer active.",
        }
        ack = reasons.get(outcome.reason or "", "❌ Couldn't link this account.")

    return ChannelInboundAction(
        result={
            "status": "channel_link_claimed" if outcome.ok else "channel_link_failed",
            "channel_contact_id": channel_contact_id,
            "user_id": outcome.user_id,
            "reason": outcome.reason,
        },
        ack_message=ack,
    )


async def maybe_handle_pending_channel_delivery(
    *,
    entity_id: str,
    channel_type: str,
    conversation_id: str,
    channel_contact_id: str,
    sender_id: str,
    sender_name: Optional[str],
    content: str,
    contact_user_id: Optional[str],
    contact_role: Optional[str],
) -> ChannelInboundAction | None:
    async with async_session() as db:
        rows = (await db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.entity_id == entity_id,
                NotificationDelivery.status.in_(("pending", "sent")),
                NotificationDelivery.channel_contact_id == channel_contact_id,
            ).order_by(desc(NotificationDelivery.created_at)).limit(1)
        )).scalars().all()
        delivery = rows[0] if rows else None
        if delivery is None:
            return None

        if delivery.expires_at is not None:
            now = datetime.now(timezone.utc)
            expires_at = delivery.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                delivery.status = "expired"
                await db.commit()
                return None

        action_key = match_action(content, delivery.actions or [])
        if not action_key:
            return None

        callback_result = await dispatch_callback(
            delivery.callback_kind or "",
            payload=delivery.callback_payload,
            action_key=action_key,
            context={
                "entity_id": entity_id,
                "user_id": delivery.user_id,
                "channel_type": channel_type,
                "channel_contact_id": channel_contact_id,
                "conversation_id": conversation_id,
                "notification_id": delivery.notification_id,
                "responder": {
                    "source_id": sender_id,
                    "name": sender_name,
                    "user_id": contact_user_id,
                    "role": contact_role,
                },
            },
        )

        ok = bool(callback_result.get("ok", False))
        delivery.status = "resolved" if ok else "failed"
        delivery.resolved_action_key = action_key
        delivery.resolved_at = datetime.now(timezone.utc)
        if not ok:
            delivery.error_message = str(callback_result.get("error", "callback failed"))[:500]
        result = {
            "status": "delivery_resolved" if ok else "delivery_callback_failed",
            "delivery_id": delivery.id,
            "notification_id": delivery.notification_id,
            "callback_kind": delivery.callback_kind,
            "action_key": action_key,
            "callback_result": callback_result,
        }
        await db.commit()

    ack_message = (
        callback_result.get("message")
        if isinstance(callback_result, dict)
        else None
    )
    if not ack_message:
        ack_message = (
            f"Got it — recorded your response: {action_key}."
            if ok
            else f"Sorry, couldn't apply '{action_key}' right now."
        )

    return ChannelInboundAction(result=result, ack_message=ack_message)
