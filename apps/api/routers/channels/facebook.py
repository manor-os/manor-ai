"""Facebook (Pages + Messenger) webhook endpoints.

GET  /api/v1/channels/facebook/webhook   — Meta hub.challenge handshake
POST /api/v1/channels/facebook/webhook   — feed events + Messenger events

Meta hits ONE callback URL per App; we use ``config_id`` query param to
disambiguate which entity / Page the event is for. The same path is
registered as the App-level Webhooks → Callback URL on
developers.facebook.com.

Verification:
  * GET handshake compares ``hub.verify_token`` against the
    ChannelConfig.credentials.verify_token (or env fallback).
  * POST body HMAC-SHA256 verified against ``app_secret`` in
    ChannelConfig.credentials (or ``FACEBOOK_APP_SECRET`` env).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels import get_adapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/facebook", tags=["channels"])


@router.get("/webhook", response_class=PlainTextResponse)
async def facebook_verify(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig id for the Facebook channel."),
):
    """Hub challenge handshake. Meta sends:
        hub.mode=subscribe
        hub.verify_token=<the token we set in App admin>
        hub.challenge=<random string>
    We must echo ``hub.challenge`` plain-text iff the token matches.
    """
    qp = request.query_params
    mode = qp.get("hub.mode", "")
    token_in = qp.get("hub.verify_token", "")
    challenge = qp.get("hub.challenge", "")

    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )).scalar_one_or_none()

    expected = ""
    if cc and cc.credentials:
        expected = cc.credentials.get("verify_token") or ""
    if not expected:
        expected = os.environ.get("FACEBOOK_WEBHOOK_VERIFY_TOKEN", "")

    if mode == "subscribe" and token_in and token_in == expected:
        return PlainTextResponse(challenge or "")
    logger.warning(
        "Facebook webhook verify FAILED config_id=%s mode=%s token_match=%s",
        config_id, mode, bool(token_in and token_in == expected),
    )
    raise HTTPException(403, "verify_token mismatch")


@router.post("/webhook")
async def facebook_callback(
    request: Request,
    background: BackgroundTasks,
    config_id: str = Query(..., description="ChannelConfig id for the Facebook channel."),
):
    """Receive a feed/messaging event batch and dispatch to the bound
    agent. Returns 200 immediately so Meta doesn't retry.
    """
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    query = dict(request.query_params)

    adapter = get_adapter("facebook")
    if adapter is None:
        raise HTTPException(500, "Facebook adapter not registered")

    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )).scalar_one_or_none()
    if not cc:
        raise HTTPException(404, f"ChannelConfig {config_id!r} not found")

    # Backstop: env-level app_secret if the row didn't have one (dev
    # mode / first boot).
    if cc.credentials and not cc.credentials.get("app_secret"):
        env_secret = os.environ.get("FACEBOOK_APP_SECRET", "")
        if env_secret:
            cc.credentials = {**cc.credentials, "app_secret": env_secret}

    if not await adapter.verify_inbound(cc, headers=headers, query=query, body=body):
        logger.warning("Facebook signature mismatch for cc=%s", config_id)
        raise HTTPException(401, "Bad X-Hub-Signature-256")

    try:
        normalized = await adapter.parse_inbound(
            cc, headers=headers, query=query, body=body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("facebook parse_inbound failed cc=%s: %s", config_id, exc)
        return {"ok": True, "ignored": True, "reason": str(exc)}

    if not normalized:
        # Status events / our own echoes / unsupported types — ack
        # without dispatching.
        return {"ok": True, "ignored": True}

    # Persist + dispatch async; 200 OK immediately.
    background.add_task(_persist_and_dispatch, normalized, cc.id)
    return {"ok": True}


async def _persist_and_dispatch(normalized, cc_id: str) -> None:
    try:
        async with async_session() as db:
            log_id = await handle_inbound_message(db, normalized)
            await db.commit()
        if log_id:
            try:
                dispatch_inbound_task.delay(log_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "facebook: dispatch_inbound_task enqueue failed for log_id=%s "
                    "(Celery unreachable?). Inbound saved, agent reply skipped.",
                    log_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception("facebook _persist_and_dispatch crashed cc_id=%s", cc_id)
