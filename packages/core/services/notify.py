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
      - "broadcast"   — entity-wide WS broadcast

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
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Used by callers that want the old "persist + WS" behaviour explicitly.
LEGACY_INAPP_CHANNELS: list[str] = ["db", "ws"]


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
    # Future delivery — persist a pending row and let the sweeper kick
    # off the real fan-out at deliver_at. Past timestamps fall through
    # to immediate dispatch (catching up after worker downtime is the
    # whole point of the sweeper).
    if deliver_at is not None:
        now = datetime.now(timezone.utc)
        if deliver_at.tzinfo is None:
            deliver_at = deliver_at.replace(tzinfo=timezone.utc)
        if deliver_at > now:
            await _schedule_for_later(
                entity_id=entity_id, user_id=user_id, type=type, title=title,
                body=body, link=link, meta=meta, severity=severity,
                workspace_id=workspace_id, actions=actions,
                callback_kind=callback_kind, callback_payload=callback_payload,
                expires_in_seconds=expires_in_seconds, deliver_at=deliver_at,
            )
            return

    if channels is not None:
        await _legacy_dispatch(
            entity_id=entity_id,
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            meta=meta,
            channels=list(channels),
        )
        return

    await _routed_dispatch(
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
    )


# ── Scheduled delivery ────────────────────────────────────────────────────

async def _schedule_for_later(
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
    deliver_at: datetime,
) -> None:
    """Persist a Notification row with dispatch_status='pending' so the
    sweeper picks it up at the requested time.

    All the producer's args are stashed under ``meta._scheduled`` so the
    sweeper can replay the call verbatim — including actions / callback
    / severity that would otherwise be lost when reloading the row.
    """
    from packages.core.database import async_session
    from packages.core.models.notification import Notification

    payload_meta = dict(meta or {})
    payload_meta["_scheduled"] = {
        "severity": severity,
        "workspace_id": workspace_id,
        "actions": actions,
        "callback_kind": callback_kind,
        "callback_payload": callback_payload,
        "expires_in_seconds": expires_in_seconds,
        "link": link,
    }

    async with async_session() as db:
        db.add(Notification(
            entity_id=entity_id,
            user_id=user_id,
            type=type,
            title=title,
            content=body,
            meta=payload_meta,
            deliver_at=deliver_at,
            dispatch_status="pending",
        ))
        await db.commit()


# ── Routed dispatch ────────────────────────────────────────────────────────

async def _routed_dispatch(
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
    actions: list[dict] | None = None,
    callback_kind: str | None = None,
    callback_payload: dict | None = None,
    expires_in_seconds: int | None = None,
) -> None:
    """User-preference-driven fan-out across in-app + external channels."""
    from packages.core.database import async_session
    from packages.core.services.notification_routing import (
        resolve_channel_targets,
    )

    notification_id: Optional[str] = None
    targets = []
    inapp_only = False

    # Phase 1: persist the in-app notification + resolve channels in one
    # transaction so a single DB session covers both lookups.
    async with async_session() as db:
        try:
            targets = await resolve_channel_targets(
                db,
                entity_id=entity_id,
                user_id=user_id,
                kind=type,
                severity=severity,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.warning(
                "notify: channel resolution failed for type=%s — falling back to in-app",
                type,
                exc_info=True,
            )
            inapp_only = True

        try:
            from packages.core.services.notification_service import create_notification

            notif = await create_notification(
                db,
                entity_id,
                user_id,
                type=type,
                title=title,
                body=body,
                link=link,
                meta=_compose_meta(
                    meta, severity=severity, workspace_id=workspace_id,
                    actions=actions, callback_kind=callback_kind,
                ),
            )
            notification_id = notif.id
            await db.commit()
        except Exception:
            logger.warning(
                "notify: in-app persist failed for type=%s user=%s",
                type, user_id, exc_info=True,
            )

    if inapp_only or not targets:
        return

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
        return

    for choice in targets:
        if choice.channel_type == "inapp":
            continue
        if choice.contact is None:
            if choice.channel_type == "email" and choice.address:
                await _deliver_via_registered_email(
                    to_address=choice.address,
                    title=title,
                    text=rendered,
                    entity_id=entity_id,
                    user_id=user_id,
                )
            continue
        await _deliver_via_channel_gateway(
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


async def _deliver_via_registered_email(
    *,
    to_address: str,
    title: str,
    text: str,
    entity_id: str,
    user_id: str,
) -> None:
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
                channel_type="email",
                to_address=to_address,
                subject=f"Manor AI: {title}",
                content=text,
                status="sent" if sent else "failed",
                error_message=None if sent else "email_service_failed",
            ))
            await db.commit()
    except Exception:
        logger.warning(
            "notify: registered-email dispatch failed for user=%s email=%s",
            user_id,
            to_address,
            exc_info=True,
        )


async def _deliver_via_channel_gateway(
    *, channel_contact_id: str, text: str, notification_id: str | None,
    entity_id: str, user_id: str,
    actions: list[dict] | None = None,
    callback_kind: str | None = None,
    callback_payload: dict | None = None,
    expires_in_seconds: int | None = None,
) -> None:
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
    from packages.core.models.channel import ChannelContact
    from packages.core.models.notification import NotificationDelivery
    from packages.core.models.task import Conversation
    from packages.core.services.channel_outbound_delivery import (
        send_actionable_outbound_to_contact,
        send_outbound_to_contact,
    )

    try:
        async with async_session() as db:
            contact = (await db.execute(
                select(ChannelContact).where(ChannelContact.id == channel_contact_id)
            )).scalar_one_or_none()
            if not contact:
                logger.debug(
                    "notify: contact %s vanished before dispatch",
                    channel_contact_id,
                )
                return
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
                db.add(NotificationDelivery(
                    notification_id=notification_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    channel_contact_id=contact.id,
                    channel_type=contact.channel_type,
                    conversation_id=conv_row.id if conv_row else None,
                    message_log_id=result.get("message_log_id"),
                    actions=actions,
                    callback_kind=callback_kind,
                    callback_payload=callback_payload,
                    status="sent" if send_ok else "failed",
                    error_message=None if send_ok else result.get("error"),
                    expires_at=expires_at,
                ))
            await db.commit()
    except Exception:
        logger.warning(
            "notify: channel dispatch failed for contact=%s",
            channel_contact_id, exc_info=True,
        )


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
) -> None:
    """The original notify() body — kept verbatim for callers that pass
    explicit ``channels=`` lists. Behaviour is unchanged."""
    active_channels = list(channels)

    if "db" in active_channels:
        try:
            from packages.core.database import async_session
            from packages.core.services.notification_service import (
                create_notification,
            )

            async with async_session() as db:
                await create_notification(
                    db, entity_id, user_id,
                    type=type, title=title,
                    body=body, link=link, meta=meta,
                )
                await db.commit()
            # create_notification already does the WS push.
            active_channels = [c for c in active_channels if c != "ws"]
        except Exception:
            logger.warning("notify: DB persist failed for type=%s", type, exc_info=True)

    if "ws" in active_channels:
        try:
            from packages.core.services.realtime import push_notification

            await push_notification(user_id, {
                "type": type,
                "title": title,
                "content": body,
                "link": link,
                "metadata": meta or {},
            })
        except Exception:
            logger.debug("notify: WS push failed for user=%s", user_id)

    if "broadcast" in active_channels:
        try:
            from packages.core.services.realtime import _broadcast

            broadcast_event = (meta or {}).get("broadcast_event") or type
            await _broadcast(entity_id, broadcast_event, {
                "title": title,
                "body": body,
                "link": link,
                **(meta or {}),
            })
        except Exception:
            logger.debug("notify: broadcast failed for entity=%s", entity_id)
