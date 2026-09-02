"""Unified notification dispatcher — single entry point for all notifications.

Usage:
    from packages.core.services.notify import notify

    # Default: route per user preferences (in-app + whatever external
    # channels the user opted into for this kind).
    await notify(
        entity_id="...",
        user_id="...",
        type="task_hitl_requested",
        title="Approve external reply",
        body="Draft reply ready for review.",
        link="/tasks/01KQ...",
        meta={"task_id": "..."},
    )

    # Explicit override — pin the channels for this single call. Legacy
    # callers pass ["db"] / ["db", "ws"] / ["broadcast"] here.
    await notify(..., channels=["db", "ws"])

Channels:
    Legacy (in-app only):
      - "db"          — persist to notifications table (also pushes WS)
      - "ws"          — WebSocket push to the user's connected sessions
      - "broadcast"   — workspace-scoped WS broadcast when workspace_id is set,
                        otherwise entity-wide

    Multi-channel (when ``channels=None``):
      - resolve via ``notification_routing.resolve_channel_targets`` and
        dispatch to every selected target. ``"inapp"`` covers db + WS in
        one step; ``"telegram"`` / ``"wechat"`` / ``"email"`` / etc. push
        through the matching channel adapter using the user's linked
        ``ChannelContact``.

Per-call ``channels`` always wins over user preferences. Pass
``channels=["inapp"]`` to force in-app even when the user opted into
Telegram for this kind; pass an empty list to suppress delivery entirely
(e.g. when the caller has already pushed via another path and only wants
the audit trail off).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from packages.core.constants.notification_types import (
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationDispatchStatus,
    NotificationOutboxStatus,
)
from packages.core.services.notification_targets import (
    NotificationDeliveryTargetFactory,
)

logger = logging.getLogger(__name__)

# Used by callers that want the old "persist + WS" behaviour explicitly.
LEGACY_INAPP_CHANNELS: list[str] = [
    NotificationChannel.DATABASE.value,
    NotificationChannel.WEBSOCKET.value,
]
_LEGACY_CHANNELS = {
    NotificationChannel.DATABASE.value,
    NotificationChannel.WEBSOCKET.value,
    NotificationChannel.BROADCAST.value,
}
_DELIVERED_TARGET_KEYS = "_delivered_target_keys"


def _delivery_intent_payload(payload: dict) -> dict:
    intent = dict(payload)
    intent.pop(_DELIVERED_TARGET_KEYS, None)
    return intent


async def _broadcast_notification(
    *,
    entity_id: str,
    workspace_id: str | None,
    event: str,
    data: dict,
) -> None:
    if workspace_id:
        from packages.core.services.realtime import _broadcast_workspace

        await _broadcast_workspace(entity_id, workspace_id, event, data)
        return
    from packages.core.services.realtime import _broadcast

    await _broadcast(entity_id, event, data)


async def notify(
    entity_id: str,
    user_id: str,
    type: str,
    title: str,
    *,
    body: str | None = None,
    link: str | None = None,
    meta: dict | None = None,
    channels: Sequence[str] | None = None,
    severity: str | None = None,
    workspace_id: str | None = None,
    actions: list[dict] | None = None,
    callback_kind: str | None = None,
    callback_payload: dict | None = None,
    expires_in_seconds: int | None = None,
    deliver_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Dispatch a notification through the resolved channels.

    When ``channels`` is ``None`` (the default and recommended path), the
    dispatcher reads the user's notification preferences + workspace +
    entity policy via ``notification_routing`` and fans out to every
    resolved channel. The user's in-app bell is always included.

    When ``channels`` is explicitly provided, only those names are used
    (legacy ``db`` / ``ws`` / ``broadcast`` semantics still work). An
    empty list suppresses delivery — the caller takes responsibility.

    Actionable notifications:
        Pass ``actions=[{"key": "approve", "label": "Approve"},
        {"key": "reject", "label": "Reject"}]`` together with
        ``callback_kind`` (the key the producer registered via
        ``notification_callbacks.register_callback``) and
        ``callback_payload`` (whatever context the handler needs).

        For every external channel the dispatcher selects we write a
        ``NotificationDelivery`` row carrying the actions + callback. The
        rendered text appended to the message tells the user how to
        reply; when they do, ``dispatch_inbound`` matches their text
        against the action keys and fires the callback.
    """
    from packages.core.services.notification_service import (
        normalize_notification_workspace_scope,
    )

    meta, workspace_id = normalize_notification_workspace_scope(
        meta=meta,
        workspace_id=workspace_id,
    )
    if channels is not None and not channels:
        return
    if not _render_for_external(
        title=title,
        body=body,
        link=link,
    ).strip():
        raise ValueError("notification must include user-visible content")
    if not await notification_recipient_is_authorized(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
    ):
        logger.debug(
            "notify: dropping inaccessible workspace notification user=%s workspace=%s",
            user_id,
            workspace_id,
        )
        return

    meta = _compose_meta(
        meta,
        severity=severity,
        workspace_id=workspace_id,
        actions=actions,
        callback_kind=callback_kind,
    )

    # Future delivery persists one hidden Notification plus its outbox row.
    # Immediate routed delivery uses the same outbox, then attempts it before
    # returning; a process crash leaves a recoverable pending row.
    if deliver_at is not None:
        now = datetime.now(timezone.utc)
        if deliver_at.tzinfo is None:
            deliver_at = deliver_at.replace(tzinfo=timezone.utc)
        if deliver_at > now:
            await _persist_routed_notification(
                entity_id=entity_id, user_id=user_id, type=type, title=title,
                body=body, link=link, meta=meta, severity=severity,
                workspace_id=workspace_id, actions=actions,
                callback_kind=callback_kind, callback_payload=callback_payload,
                expires_in_seconds=expires_in_seconds, deliver_at=deliver_at,
                channels=list(channels) if channels is not None else None,
                idempotency_key=idempotency_key,
            )
            return

    if channels is not None and all(channel in _LEGACY_CHANNELS for channel in channels):
        await _legacy_dispatch(
            entity_id=entity_id,
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            meta=meta,
            channels=list(channels),
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
        )
        return

    await _persist_routed_notification(
        entity_id=entity_id,
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
        meta=meta,
        severity=severity,
        workspace_id=workspace_id,
        actions=actions,
        callback_kind=callback_kind,
        callback_payload=callback_payload,
        expires_in_seconds=expires_in_seconds,
        deliver_at=deliver_at,
        channels=list(channels) if channels is not None else None,
        idempotency_key=idempotency_key,
    )


async def _user_can_receive_workspace_notification(
    *,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    db=None,
) -> bool:
    """Keep every notification channel inside the Workspace read boundary."""
    from packages.core.database import async_session
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.workspace_access import user_can_read_workspace_id

    async def _authorized(session) -> bool:
        role = await resolve_effective_user_role_name(
            session,
            user_id=user_id,
            entity_id=entity_id,
        )
        return await user_can_read_workspace_id(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=role,
        )

    if db is not None:
        return await _authorized(db)
    async with async_session() as session:
        return await _authorized(session)


async def notification_recipient_is_authorized(
    *,
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    db=None,
) -> bool:
    if not workspace_id:
        return True
    return await _user_can_receive_workspace_notification(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        db=db,
    )


# ── Durable routed delivery ───────────────────────────────────────────────

async def _persist_routed_notification(
    *,
    entity_id: str,
    user_id: str,
    type: str,
    title: str,
    body: str | None,
    link: str | None,
    meta: dict | None,
    severity: str | None,
    workspace_id: str | None,
    actions: list[dict] | None,
    callback_kind: str | None,
    callback_payload: dict | None,
    expires_in_seconds: int | None,
    deliver_at: datetime | None,
    channels: list[str] | None,
    idempotency_key: str | None,
) -> None:
    """Persist the in-app fact and external fan-out intent atomically."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.notification import NotificationOutboxEvent
    from packages.core.services.notification_service import create_notification_once

    now = datetime.now(timezone.utc)
    scheduled = deliver_at is not None and deliver_at > now
    available_at = deliver_at if scheduled and deliver_at is not None else now
    payload = {
        "severity": severity,
        "actions": actions,
        "callback_kind": callback_kind,
        "callback_payload": callback_payload,
        "expires_in_seconds": expires_in_seconds,
        "link": link,
        "channels": channels,
        "deliver_at": deliver_at.isoformat() if deliver_at is not None else None,
    }

    async with async_session() as db:
        notification, created = await create_notification_once(
            db,
            entity_id,
            user_id,
            type,
            title,
            body=body,
            link=link,
            meta=meta,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            deliver_at=deliver_at,
            dispatch_status=(
                NotificationDispatchStatus.PENDING.value
                if scheduled
                else NotificationDispatchStatus.DISPATCHED.value
            ),
            push_realtime=not scheduled,
        )
        if created:
            db.add(NotificationOutboxEvent(
                notification_id=notification.id,
                payload=payload,
                status=NotificationOutboxStatus.PENDING.value,
                available_at=available_at,
            ))
        else:
            existing_outbox = (
                await db.execute(
                    select(NotificationOutboxEvent).where(
                        NotificationOutboxEvent.notification_id == notification.id,
                    )
                )
            ).scalar_one_or_none()
            if (
                existing_outbox is None
                or _delivery_intent_payload(dict(existing_outbox.payload or {})) != payload
            ):
                raise ValueError(
                    "notification idempotency_key already exists with different delivery intent"
                )
        await db.commit()

    if not scheduled:
        from packages.core.services.notification_scheduler import (
            dispatch_notification_outbox,
        )

        async with async_session() as db:
            await dispatch_notification_outbox(
                db,
                notification_id=notification.id,
            )


async def dispatch_persisted_notification(
    *,
    notification_id: str,
    entity_id: str,
    user_id: str,
    type: str,
    title: str,
    body: str | None,
    meta: dict | None,
    workspace_id: str | None,
    payload: dict,
    delivered_target_keys: set[str] | None = None,
    before_external_target: Callable[[], Awaitable[bool]] | None = None,
    on_target_delivered: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Fan out an already committed Notification without creating another."""
    from packages.core.database import async_session
    from packages.core.services.notification_routing import (
        resolve_channel_targets,
    )

    severity = payload.get("severity")
    actions = payload.get("actions")
    callback_kind = payload.get("callback_kind")
    callback_payload = payload.get("callback_payload")
    expires_in_seconds = payload.get("expires_in_seconds")
    link = payload.get("link")
    channels = payload.get("channels")
    delivered_targets = set(delivered_target_keys or ())

    async def external_target_is_authorized() -> bool:
        if before_external_target is None:
            return True
        return await before_external_target()

    async def mark_target_delivered(target_key: str) -> None:
        if on_target_delivered is not None:
            await on_target_delivered(target_key)
        delivered_targets.add(target_key)

    explicit_external_channels: list[str] | None = None
    if channels is not None:
        active_channels = list(channels) if isinstance(channels, list) else []
        if NotificationChannel.BROADCAST.value in active_channels:
            broadcast_target = NotificationDeliveryTargetFactory.broadcast(
                entity_id,
                workspace_id=workspace_id,
            ).key
            if broadcast_target not in delivered_targets:
                if not await external_target_is_authorized():
                    return
                broadcast_event = (meta or {}).get("broadcast_event") or type
                await _broadcast_notification(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    event=broadcast_event,
                    data={
                        "title": title,
                        "body": body,
                        "link": link,
                        **(meta or {}),
                    },
                )
                await mark_target_delivered(broadcast_target)
        explicit_external_channels = [
            channel
            for channel in active_channels
            if channel not in _LEGACY_CHANNELS
            and channel != NotificationChannel.INAPP.value
        ]
        if not explicit_external_channels:
            return

    async with async_session() as db:
        targets = await resolve_channel_targets(
            db,
            entity_id=entity_id,
            user_id=user_id,
            kind=type,
            severity=severity,
            workspace_id=workspace_id,
            explicit_channels=explicit_external_channels,
            allow_registered_email=not bool(actions),
        )

    delivery_errors: list[str] = []
    if explicit_external_channels is not None:
        resolved_channel_types = {
            choice.channel_type
            for choice in targets
            if choice.channel_type != NotificationChannel.INAPP.value
            and (choice.contact is not None or choice.address)
        }
        resolved_channel_types.update(
            NotificationDeliveryTargetFactory.parse(target_key).channel_type
            for target_key in delivered_targets
        )
        missing_channel_types = [
            channel
            for channel in dict.fromkeys(explicit_external_channels)
            if channel not in resolved_channel_types
        ]
        if missing_channel_types:
            delivery_errors.append(
                "notification explicit channel has no active target: "
                + ", ".join(missing_channel_types)
            )

    # Phase 2: dispatch external channels. Each gets its own short-lived
    # session so a single adapter failure (or slow network) doesn't hold
    # the others up — and the in-app row is already durable.
    #
    # When actions are present we let the adapter format the prompt — it
    # may use native UI (Telegram inline keyboard, WhatsApp quick replies)
    # or fall back to a text "Reply with…" footer inside its
    # ``send_actionable_message`` default. Either way the callback_data /
    # button id echoes the action ``key`` so the inbound matcher
    # resolves it the same way as a typed reply.
    rendered = _render_for_external(
        title=title, body=body, link=link,
        actions=None,  # adapter renders actions; we just give it the body
    )
    if not rendered.strip():
        if delivery_errors:
            raise RuntimeError("; ".join(delivery_errors))
        if any(
            choice.channel_type != NotificationChannel.INAPP.value
            for choice in targets
        ):
            raise RuntimeError(
                "notification external delivery has no user-visible content"
            )
        return

    for choice in targets:
        if choice.channel_type == NotificationChannel.INAPP.value:
            continue
        target_key = NotificationDeliveryTargetFactory.external(
            channel_type=choice.channel_type,
            contact_id=choice.contact.id if choice.contact is not None else None,
            address=choice.address,
        ).key
        if target_key in delivered_targets:
            continue
        if not await external_target_is_authorized():
            return
        if choice.contact is None:
            if (
                choice.channel_type == NotificationChannel.EMAIL.value
                and choice.address
            ):
                delivered = await _deliver_via_registered_email(
                    to_address=choice.address,
                    title=title,
                    text=rendered,
                    entity_id=entity_id,
                    user_id=user_id,
                )
                if not delivered:
                    delivery_errors.append(
                        f"notification email delivery failed for {choice.address}"
                    )
                else:
                    await mark_target_delivered(target_key)
            continue
        delivered = await _deliver_via_channel_gateway(
            channel_contact_id=choice.contact.id,
            text=rendered,
            notification_id=notification_id,
            entity_id=entity_id,
            user_id=user_id,
            actions=actions,
            callback_kind=callback_kind,
            callback_payload=callback_payload,
            expires_in_seconds=expires_in_seconds,
        )
        if not delivered:
            delivery_errors.append(
                f"notification channel delivery failed for {choice.channel_type}"
            )
        else:
            await mark_target_delivered(target_key)

    if delivery_errors:
        raise RuntimeError("; ".join(delivery_errors))


async def _deliver_via_registered_email(
    *,
    to_address: str,
    title: str,
    text: str,
    entity_id: str,
    user_id: str,
) -> bool:
    """Send an email notification to the user's registered email address.

    Email is the one external channel that has a natural default identity
    inside Manor: ``User.email``. Users can still link extra email contacts,
    but the registered address should work without a manual channel-claim
    flow.
    """
    try:
        from packages.core.database import async_session
        from packages.core.models.channel import MessageLog
        from packages.core.services.email_service import send_notification_email

        sent = await send_notification_email(to_address, title, text)
        async with async_session() as db:
            db.add(MessageLog(
                entity_id=entity_id,
                direction="outbound",
                channel_type=NotificationChannel.EMAIL.value,
                to_address=to_address,
                subject=f"Manor AI: {title}",
                content=text,
                status=(
                    NotificationDeliveryStatus.SENT.value
                    if sent
                    else NotificationDeliveryStatus.FAILED.value
                ),
                error_message=None if sent else "email_service_failed",
            ))
            await db.commit()
        return bool(sent)
    except Exception:
        logger.warning(
            "notify: registered-email dispatch failed for user=%s email=%s",
            user_id,
            to_address,
            exc_info=True,
        )
        return False


async def _deliver_via_channel_gateway(
    *, channel_contact_id: str, text: str, notification_id: str | None,
    entity_id: str, user_id: str,
    actions: list[dict] | None = None,
    callback_kind: str | None = None,
    callback_payload: dict | None = None,
    expires_in_seconds: int | None = None,
) -> bool:
    """Send one rendered notification through channel outbound delivery.

    Loads the contact in a fresh session and hands it to
    ``send_outbound_to_contact`` which writes a MessageLog + invokes the
    matching channel adapter. Failures here are swallowed and logged so a
    broken provider can't take down the rest of the notification fan-out.

    When ``actions`` is set the function also creates a
    ``NotificationDelivery`` row so the channel inbound path can match a
    later user reply against the action keys.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import ChannelConfig, ChannelContact
    from packages.core.models.notification import NotificationDelivery
    from packages.core.models.task import Conversation
    from packages.core.services.channel_outbound_delivery import (
        send_actionable_outbound_to_contact,
        send_outbound_to_contact,
    )

    try:
        async with async_session() as db:
            contact = (await db.execute(
                select(ChannelContact)
                .join(
                    ChannelConfig,
                    ChannelConfig.id == ChannelContact.channel_config_id,
                )
                .where(
                    ChannelContact.id == channel_contact_id,
                    ChannelContact.entity_id == entity_id,
                    ChannelContact.user_id == user_id,
                    ChannelContact.status == "active",
                    ChannelConfig.entity_id == entity_id,
                    ChannelConfig.channel_type == ChannelContact.channel_type,
                    ChannelConfig.status == "active",
                )
            )).scalar_one_or_none()
            if not contact:
                logger.debug(
                    "notify: contact %s is no longer an active delivery target",
                    channel_contact_id,
                )
                return False
            existing_delivery = None
            if actions and notification_id:
                existing_delivery = (
                    await db.execute(
                        select(NotificationDelivery)
                        .where(
                            NotificationDelivery.notification_id == notification_id,
                            NotificationDelivery.channel_contact_id == contact.id,
                        )
                        .order_by(NotificationDelivery.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_delivery and existing_delivery.status in {
                    NotificationDeliveryStatus.SENT.value,
                    NotificationDeliveryStatus.RESOLVED.value,
                }:
                    return True
            if actions:
                result = await send_actionable_outbound_to_contact(
                    db,
                    contact=contact,
                    text=text,
                    actions=actions,
                    notification_id=notification_id,
                )
            else:
                result = await send_outbound_to_contact(
                    db,
                    contact=contact,
                    text=text,
                    notification_id=notification_id,
                )

            if actions and notification_id:
                # Best-effort look-up of an existing channel conversation
                # for this contact so the inbound matcher can find the
                # delivery by conversation_id without a contact-wide scan.
                conv_row = (await db.execute(
                    select(Conversation).where(
                        Conversation.entity_id == contact.entity_id,
                        Conversation.channel == contact.channel_type,
                        Conversation.meta["channel_contact_id"].astext == contact.id,
                    ).order_by(Conversation.updated_at.desc()).limit(1)
                )).scalar_one_or_none()

                expires_at = None
                if expires_in_seconds and expires_in_seconds > 0:
                    expires_at = datetime.now(timezone.utc) + timedelta(
                        seconds=expires_in_seconds,
                    )

                send_ok = bool(result.get("sent"))
                delivery = existing_delivery or NotificationDelivery(
                    notification_id=notification_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    channel_contact_id=contact.id,
                    channel_type=contact.channel_type,
                )
                delivery.conversation_id = conv_row.id if conv_row else None
                delivery.message_log_id = result.get("message_log_id")
                delivery.actions = actions
                delivery.callback_kind = callback_kind
                delivery.callback_payload = callback_payload
                delivery.status = (
                    NotificationDeliveryStatus.SENT.value
                    if send_ok
                    else NotificationDeliveryStatus.FAILED.value
                )
                delivery.error_message = None if send_ok else result.get("error")
                delivery.expires_at = expires_at
                if existing_delivery is None:
                    db.add(delivery)
            await db.commit()
            return bool(result.get("sent"))
    except Exception:
        logger.warning(
            "notify: channel dispatch failed for contact=%s",
            channel_contact_id, exc_info=True,
        )
        return False


def _render_for_external(
    *, title: str, body: str | None, link: str | None,
    actions: list[dict] | None = None,
) -> str:
    """Render a notification for an external channel.

    Adapters all take a plain string; we glue the title, body and link
    together into something readable on a phone. Channel-specific richer
    formatting (Telegram inline buttons, WhatsApp template) is a future
    polish — text fallback works everywhere today.

    When ``actions`` is supplied we append a "Reply with…" footer so the
    user knows what shortcuts to type — the inbound matcher accepts the
    action key, label, any listed synonym, or a 1-based numeric pick.
    """
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if body and body.strip():
        parts.append(body.strip())
    if link:
        parts.append(link.strip())
    if actions:
        choices: list[str] = []
        for idx, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                continue
            label = action.get("label") or action.get("key")
            if not isinstance(label, str) or not label:
                continue
            choices.append(f"{idx}. {label}")
        if choices:
            parts.append("Reply with:\n" + "\n".join(choices))
    return "\n\n".join(p for p in parts if p)


def _compose_meta(
    meta: dict | None,
    *,
    severity: str | None,
    workspace_id: str | None,
    actions: list[dict] | None = None,
    callback_kind: str | None = None,
) -> dict:
    """Stamp severity + workspace into Notification.meta for downstream UI."""
    out: dict = dict(meta or {})
    if severity and "severity" not in out:
        out["severity"] = severity
    if workspace_id and "workspace_id" not in out:
        out["workspace_id"] = workspace_id
    if actions and "actions" not in out:
        out["actions"] = actions
    if callback_kind and "callback_kind" not in out:
        out["callback_kind"] = callback_kind
    return out


# ── Legacy dispatch (back-compat shim) ─────────────────────────────────────

async def _legacy_dispatch(
    *,
    entity_id: str,
    user_id: str,
    type: str,
    title: str,
    body: str | None,
    link: str | None,
    meta: dict | None,
    channels: list[str],
    workspace_id: str | None,
    idempotency_key: str | None,
) -> None:
    """Back-compat dispatch for callers that pass legacy channel names."""
    active_channels = list(channels)

    if NotificationChannel.DATABASE.value in active_channels:
        try:
            from packages.core.database import async_session
            from packages.core.services.notification_service import create_notification_once

            async with async_session() as db:
                _notification, created = await create_notification_once(
                    db, entity_id, user_id,
                    type=type, title=title,
                    body=body, link=link, meta=meta,
                    workspace_id=workspace_id,
                    idempotency_key=idempotency_key,
                )
                await db.commit()
            # create_notification already does the WS push.
            active_channels = [
                channel
                for channel in active_channels
                if channel != NotificationChannel.WEBSOCKET.value
            ]
            if not created:
                return
        except Exception:
            logger.warning("notify: DB persist failed for type=%s", type, exc_info=True)
            raise

    if NotificationChannel.WEBSOCKET.value in active_channels:
        try:
            from packages.core.services.realtime import push_notification

            await push_notification(user_id, {
                "type": type,
                "title": title,
                "content": body,
                "link": link,
                "metadata": meta or {},
            }, entity_id=entity_id, workspace_id=workspace_id)
        except Exception:
            logger.debug("notify: WS push failed for user=%s", user_id)

    if NotificationChannel.BROADCAST.value in active_channels:
        try:
            broadcast_event = (meta or {}).get("broadcast_event") or type
            await _broadcast_notification(
                entity_id=entity_id,
                workspace_id=workspace_id,
                event=broadcast_event,
                data={
                    "title": title,
                    "body": body,
                    "link": link,
                    **(meta or {}),
                },
            )
        except Exception:
            logger.debug("notify: broadcast failed for entity=%s", entity_id)
