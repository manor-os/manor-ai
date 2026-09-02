from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select

from packages.core.constants.advisory_locks import AdvisoryLockNamespace
from packages.core.constants.notification_types import (
    NotificationCallbackDisposition,
    NotificationDeliveryStatus,
)
from packages.core.database import async_session
from packages.core.models.channel import ChannelContact
from packages.core.models.notification import NotificationDelivery
from packages.core.services.advisory_locks import TransactionAdvisoryLock
from packages.core.services.notification_callbacks import dispatch_callback, match_action
from packages.core.services.notification_channel_linking import claim_token, extract_start_token


@dataclass(frozen=True)
class ChannelInboundAction:
    result: dict[str, Any]
    ack_message: str | None = None


async def maybe_claim_channel_link_token(
    *,
    channel_contact_id: str,
    channel_type: str,
    content: str,
) -> ChannelInboundAction | None:
    if channel_type.strip().lower() == "whatsapp":
        return None
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
        channel_label = {
            "telegram": "Telegram",
            "discord": "Discord",
        }.get(contact.channel_type, contact.channel_type.title())
        ack = (
            f"✅ Linked! This {channel_label} account is now connected to your "
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
    if channel_type.strip().lower() == "whatsapp":
        return None
    if not contact_user_id:
        return None

    # First identify a matching action without locking a row. A dedicated
    # advisory lease below fences the callback while allowing this read
    # transaction to close before any business/provider I/O begins.
    async with async_session() as db:
        delivery = (await db.execute(
            select(NotificationDelivery)
            .join(
                ChannelContact,
                ChannelContact.id == NotificationDelivery.channel_contact_id,
            )
            .where(
                NotificationDelivery.entity_id == entity_id,
                NotificationDelivery.user_id == contact_user_id,
                NotificationDelivery.status.in_((
                    NotificationDeliveryStatus.PENDING.value,
                    NotificationDeliveryStatus.SENT.value,
                )),
                NotificationDelivery.channel_contact_id == channel_contact_id,
                ChannelContact.entity_id == entity_id,
                ChannelContact.user_id == contact_user_id,
                ChannelContact.status == "active",
            )
            .order_by(desc(NotificationDelivery.created_at))
            .limit(1)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if delivery is None:
            return None
        action_key = match_action(content, delivery.actions or [])
        if not action_key:
            return None
        delivery_id = delivery.id

    async with async_session() as lock_db:
        lease = await TransactionAdvisoryLock.try_acquire(
            lock_db,
            namespace=int(
                AdvisoryLockNamespace.NOTIFICATION_DELIVERY_CALLBACK
            ),
            key=delivery_id,
        )
    if lease is None:
        return ChannelInboundAction(
            result={
                "status": "delivery_callback_in_progress",
                "delivery_id": delivery_id,
                "action_key": action_key,
            },
            ack_message="That response is already being applied.",
        )

    try:
        # Lock only long enough to revalidate the exact delivery selected
        # above. Commit before dispatch_callback so its DB/provider work never
        # runs under a NotificationDelivery row lock.
        async with async_session() as db:
            delivery = (await db.execute(
                select(NotificationDelivery)
                .join(
                    ChannelContact,
                    ChannelContact.id
                    == NotificationDelivery.channel_contact_id,
                )
                .where(
                    NotificationDelivery.id == delivery_id,
                    NotificationDelivery.entity_id == entity_id,
                    NotificationDelivery.user_id == contact_user_id,
                    NotificationDelivery.status.in_((
                        NotificationDeliveryStatus.PENDING.value,
                        NotificationDeliveryStatus.SENT.value,
                    )),
                    NotificationDelivery.channel_contact_id
                    == channel_contact_id,
                    ChannelContact.entity_id == entity_id,
                    ChannelContact.user_id == contact_user_id,
                    ChannelContact.status == "active",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if delivery is None:
                await db.rollback()
                return ChannelInboundAction(
                    result={
                        "status": "delivery_already_consumed",
                        "delivery_id": delivery_id,
                        "action_key": action_key,
                    },
                    ack_message="That response was already handled.",
                )

            if delivery.expires_at is not None:
                now = datetime.now(timezone.utc)
                expires_at = delivery.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    delivery.status = NotificationDeliveryStatus.EXPIRED.value
                    await db.commit()
                    return ChannelInboundAction(
                        result={
                            "status": "delivery_expired",
                            "delivery_id": delivery_id,
                            "action_key": action_key,
                        },
                        ack_message="That response has expired.",
                    )

            revalidated_action_key = match_action(
                content,
                delivery.actions or [],
            )
            if revalidated_action_key != action_key:
                await db.rollback()
                return ChannelInboundAction(
                    result={
                        "status": "delivery_action_changed",
                        "delivery_id": delivery_id,
                        "action_key": action_key,
                    },
                    ack_message="That response is no longer available.",
                )
            callback_kind = delivery.callback_kind or ""
            callback_payload = delivery.callback_payload
            notification_id = delivery.notification_id
            delivery_user_id = delivery.user_id
            await db.commit()

        callback_result = await dispatch_callback(
            callback_kind,
            payload=callback_payload,
            action_key=action_key,
            context={
                "entity_id": entity_id,
                "user_id": delivery_user_id,
                "channel_type": channel_type,
                "channel_contact_id": channel_contact_id,
                "conversation_id": conversation_id,
                "notification_id": notification_id,
                "responder": {
                    "source_id": sender_id,
                    "name": sender_name,
                    "user_id": contact_user_id,
                    "role": contact_role,
                },
            },
        )

        try:
            disposition = NotificationCallbackDisposition(
                callback_result.get("disposition")
            )
        except (TypeError, ValueError):
            disposition = (
                NotificationCallbackDisposition.RESOLVED
                if bool(callback_result.get("ok", False))
                else NotificationCallbackDisposition.TERMINAL_FAILURE
            )

        ok = disposition == NotificationCallbackDisposition.RESOLVED
        async with async_session() as db:
            delivery = (await db.execute(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.id == delivery_id,
                    NotificationDelivery.entity_id == entity_id,
                    NotificationDelivery.user_id == contact_user_id,
                    NotificationDelivery.channel_contact_id
                    == channel_contact_id,
                    NotificationDelivery.status.in_((
                        NotificationDeliveryStatus.PENDING.value,
                        NotificationDeliveryStatus.SENT.value,
                    )),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if delivery is None:
                await db.rollback()
                return ChannelInboundAction(
                    result={
                        "status": "delivery_state_conflict",
                        "delivery_id": delivery_id,
                        "notification_id": notification_id,
                        "callback_kind": callback_kind,
                        "action_key": action_key,
                        "callback_disposition": disposition.value,
                        "callback_result": callback_result,
                    },
                    ack_message=(
                        "The response was applied, but its notification "
                        "state needs reconciliation."
                    ),
                )
            if disposition == NotificationCallbackDisposition.RETRYABLE:
                # The callback rolled its business transaction back. Preserve
                # the open state so the same channel action can retry.
                delivery.resolved_action_key = None
                delivery.resolved_at = None
                delivery.error_message = str(
                    callback_result.get("error", "callback failed")
                )[:500]
                result_status = "delivery_callback_retryable"
            else:
                delivery.status = (
                    NotificationDeliveryStatus.RESOLVED.value
                    if ok
                    else NotificationDeliveryStatus.FAILED.value
                )
                delivery.resolved_action_key = action_key
                delivery.resolved_at = datetime.now(timezone.utc)
                delivery.error_message = (
                    None
                    if ok
                    else str(
                        callback_result.get("error", "callback failed")
                    )[:500]
                )
                result_status = (
                    "delivery_resolved"
                    if ok
                    else "delivery_callback_failed"
                )
            result = {
                "status": result_status,
                "delivery_id": delivery.id,
                "notification_id": delivery.notification_id,
                "callback_kind": delivery.callback_kind,
                "action_key": action_key,
                "callback_disposition": disposition.value,
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
    finally:
        await lease.release()
