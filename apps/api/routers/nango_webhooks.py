"""Inbound Nango webhook receiver.

Nango forwards two flavours of events to a single configured webhook URL:

  1. **Auth events** — connection created / updated / deleted. We don't
     dispatch these; we just log them so admins can see the connection
     lifecycle and potentially trigger a re-sync from the
     ``/connections/sync`` endpoint.

  2. **Forward events** — the raw webhook body the provider sent to
     Nango (Slack ``event_callback``, GitHub ``push``, etc.). We unwrap
     the envelope, look up which Manor entity owns this Nango
     connection, and route through the existing channel inbound
     pipeline (``adapter.parse_inbound`` → ``handle_inbound_message`` →
     ``dispatch_inbound_task``) so message events end up in the same
     code path as native webhooks.

Signature verification: Nango HMAC-signs the request body with the
admin secret. We accept either ``NANGO_WEBHOOK_SECRET`` (recommended,
isolates webhook auth from the API secret) or fall back to
``NANGO_SECRET_KEY``. Header: ``X-Nango-Signature`` (hex SHA-256).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Integration
from packages.core.models.nango_webhook_event import NangoWebhookEvent
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/nango", tags=["nango"])


def _resolve_webhook_secret() -> Optional[str]:
    """Resolve the secret Nango signs outbound webhooks with.

    Nango 0.36 signs every outbound webhook with the environment's
    ``secret_key`` (it has no separate webhook-secret field), so
    ``NANGO_SECRET_KEY`` is what we verify against. Future Nango
    versions that add a dedicated webhook secret can be wired by
    setting ``NANGO_WEBHOOK_SECRET`` — when present it takes priority.
    """
    return (
        os.environ.get("NANGO_WEBHOOK_SECRET", "").strip()
        or os.environ.get("NANGO_SECRET_KEY", "").strip()
        or None
    )


def _verify_signature(body: bytes, header_sig: Optional[str]) -> bool:
    """Constant-time HMAC compare. Nango docs: ``X-Nango-Signature`` is
    a hex-encoded HMAC-SHA-256 of the raw body using the admin secret."""
    secret = _resolve_webhook_secret()
    if not secret:
        # No secret configured — accept (dev-only). Production should
        # always set one; we log a loud warning per request so this
        # state is visible.
        logger.warning("Nango webhook received but NANGO_WEBHOOK_SECRET not set — accepting unverified")
        return True
    if not header_sig:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig.lower().strip())


@router.post("/webhook")
async def nango_webhook(request: Request) -> dict:
    body = await request.body()
    sig_header = (
        request.headers.get("X-Nango-Signature")
        or request.headers.get("x-nango-signature")
    )

    if not _verify_signature(body, sig_header):
        # Log the rejection too so admins can see signature mismatches
        # while debugging webhook setup.
        async with async_session() as db:
            db.add(NangoWebhookEvent(
                nango_type="unknown",
                processing_status="rejected",
                processing_detail="Signature verification failed",
                payload={"raw_len": len(body)},
            ))
            await db.commit()
        raise HTTPException(403, "Bad signature")

    try:
        envelope: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Body is not valid JSON")

    nango_type = str(envelope.get("type") or "unknown")
    provider = envelope.get("provider")
    provider_config_key = envelope.get("providerConfigKey") or provider
    connection_id = envelope.get("connectionId")
    inner_payload = envelope.get("payload") or {}

    # Look up the Integration row that holds this connection_id, so we
    # know which Manor entity owns the event.
    entity_id: Optional[str] = None
    integration_id: Optional[str] = None
    if connection_id:
        async with async_session() as db:
            integ = (await db.execute(
                select(Integration).where(
                    Integration.config["nango"]["connection_id"].astext == str(connection_id),
                )
            )).scalars().first()
            if integ:
                entity_id = integ.entity_id
                integration_id = integ.id

    # Always log the event first so admins see it even if dispatch fails.
    async with async_session() as db:
        evt = NangoWebhookEvent(
            nango_type=nango_type,
            provider=str(provider) if provider else None,
            provider_config_key=str(provider_config_key) if provider_config_key else None,
            connection_id=str(connection_id) if connection_id else None,
            entity_id=entity_id,
            integration_id=integration_id,
            processing_status="received",
            payload=envelope,
        )
        db.add(evt)
        await db.flush()
        evt_id = evt.id
        await db.commit()

    # ── Auth events: connection lifecycle. Just record; no dispatch. ──
    if nango_type in ("auth", "auth_connection"):
        await _mark_status(evt_id, "dispatched", f"Auth event ({nango_type}) recorded")
        return {"ok": True, "type": nango_type, "event_id": evt_id}

    # ── Forward / sync / other: try to route through the channel ──
    # pipeline if this provider has a registered adapter. Otherwise
    # log as unhandled — admin can see it in the events table and add
    # a handler later.
    if not entity_id:
        await _mark_status(
            evt_id, "unhandled",
            f"No Integration row matched connection_id={connection_id}",
        )
        return {"ok": True, "type": nango_type, "matched": False, "event_id": evt_id}

    handled = await _try_dispatch_to_channel(
        provider=str(provider or ""),
        entity_id=entity_id,
        inner_payload=inner_payload,
        envelope=envelope,
    )
    if handled.get("ok"):
        await _mark_status(
            evt_id, "dispatched",
            f"Routed to {handled['channel_type']} adapter "
            f"(channel_config_id={handled.get('channel_config_id')})",
        )
    else:
        await _mark_status(
            evt_id, "unhandled",
            handled.get("reason") or f"No dispatcher for provider={provider}",
        )

    return {
        "ok": True,
        "type": nango_type,
        "event_id": evt_id,
        "dispatched": bool(handled.get("ok")),
        "detail": handled.get("reason") or "ok",
    }


async def _mark_status(event_id: str, status: str, detail: str) -> None:
    async with async_session() as db:
        evt = (await db.execute(
            select(NangoWebhookEvent).where(NangoWebhookEvent.id == event_id)
        )).scalar_one_or_none()
        if evt:
            evt.processing_status = status
            evt.processing_detail = detail
            await db.commit()


# Map Nango provider keys → Manor channel adapter type. Add rows here
# as you wire up new providers; everything not in this map gets logged
# as "unhandled" so it's still visible.
_PROVIDER_TO_CHANNEL: dict[str, str] = {
    "slack": "slack",
    "discord": "discord",
    # Add: "github": "github_webhook" once a github inbound adapter exists.
}


async def _try_dispatch_to_channel(
    *,
    provider: str,
    entity_id: str,
    inner_payload: dict,
    envelope: dict,
) -> dict:
    """Forward the inner provider event through the existing channel
    inbound pipeline. Returns ``{"ok": True, "channel_type", "channel_config_id"}``
    on success, ``{"ok": False, "reason": ...}`` otherwise."""
    channel_type = _PROVIDER_TO_CHANNEL.get(provider.lower())
    if not channel_type:
        return {"ok": False, "reason": f"No channel adapter for provider={provider!r}"}

    # Lazy import to avoid a hard dependency on the channels package
    # at module load time.
    from packages.core.services.channels.registry import get_adapter
    adapter = get_adapter(channel_type)
    if adapter is None:
        return {"ok": False, "reason": f"Channel adapter {channel_type!r} not registered"}

    # Find the matching ChannelConfig for this entity + provider.
    # If multiple exist (multi-workspace), prefer the one whose
    # ``config.nango.connection_id`` matches the envelope's connection_id.
    target_cc: Optional[ChannelConfig] = None
    async with async_session() as db:
        rows = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.channel_type == channel_type,
                ChannelConfig.status == "active",
            )
        )).scalars().all()
        connection_id = envelope.get("connectionId")
        for cc in rows:
            cfg = (cc.config or {}) if isinstance(cc.config, dict) else {}
            nango_meta = cfg.get("nango") if isinstance(cfg.get("nango"), dict) else {}
            if connection_id and nango_meta.get("connection_id") == connection_id:
                target_cc = cc
                break
        if target_cc is None and rows:
            target_cc = rows[0]  # Fallback: first active channel of this type

    if target_cc is None:
        return {"ok": False, "reason": f"No ChannelConfig for {channel_type} on entity {entity_id}"}

    # Build the body the adapter expects — the inner payload re-encoded
    # as the raw provider webhook bytes.
    body_bytes = json.dumps(inner_payload).encode("utf-8")
    headers: dict[str, str] = {}
    query: dict[str, str] = {}

    try:
        parsed = await adapter.parse_inbound(
            target_cc, headers=headers, query=query, body=body_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Adapter %s parse_inbound crashed", channel_type)
        return {"ok": False, "reason": f"adapter.parse_inbound crashed: {exc}"}

    if not parsed:
        return {"ok": False, "reason": f"{channel_type} adapter rejected payload"}

    # Mirror the canonical pipeline: log → dispatch.
    try:
        async with async_session() as db:
            await handle_inbound_message(
                db,
                entity_id=target_cc.entity_id,
                channel_config_id=target_cc.id,
                payload={
                    "from": parsed.source_id,
                    "to": parsed.reply_to,
                    "content": parsed.content,
                    "message_id": parsed.external_message_id,
                    "channel_type": channel_type,
                    "metadata": {
                        "message_type": parsed.message_type,
                        "sender_name": parsed.sender_name,
                        "via": "nango",
                    },
                },
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("nango webhook: failed to log inbound %s", channel_type)

    dispatch_inbound_task.delay(
        entity_id=target_cc.entity_id,
        channel_config_id=target_cc.id,
        channel_type=channel_type,
        sender_id=parsed.source_id,
        sender_name=parsed.sender_name,
        chat_id=parsed.reply_to,
        content=parsed.content,
    )

    return {
        "ok": True,
        "channel_type": channel_type,
        "channel_config_id": target_cc.id,
    }
