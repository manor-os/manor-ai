"""Fixed Slack Events API endpoint for user-owned Slack installations."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_credentials import (
    channel_credential_source_is_available,
)
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.channels.slack_adapter import (
    SlackChannelAdapter,
    verify_slack_request,
)
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/slack", tags=["channels"])
_adapter = SlackChannelAdapter()


async def _resolve_channel_config(
    db: AsyncSession,
    payload: dict,
) -> ChannelConfig | None:
    app_id = str(payload.get("api_app_id") or "").strip()
    team_id = str(payload.get("team_id") or "").strip()
    enterprise_id = str(payload.get("enterprise_id") or "").strip()
    if not app_id or not (team_id or enterprise_id):
        return None

    async def _available_for(*conditions) -> list[ChannelConfig]:
        rows = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.channel_type == "slack",
                    ChannelConfig.status == "active",
                    ChannelConfig.owner_user_id.is_not(None),
                    ChannelConfig.config["slack_app_id"].astext == app_id,
                    *conditions,
                )
            )
        ).scalars().all()
        return [
            row
            for row in rows
            if await channel_credential_source_is_available(db, row)
        ]

    candidate_stages: list[list[ChannelConfig]] = []
    if team_id:
        candidate_stages.append(await _available_for(
            ChannelConfig.config["slack_team_id"].astext == team_id,
        ))
    if enterprise_id:
        candidate_stages.append(await _available_for(
            ChannelConfig.config["slack_team_id"].astext.is_(None),
            ChannelConfig.config["slack_enterprise_id"].astext == enterprise_id,
        ))

    for available in candidate_stages:
        if len(available) == 1:
            return available[0]
        if len(available) > 1:
            logger.warning(
                "Ambiguous Slack installation route app=%s team=%s enterprise=%s",
                app_id,
                team_id,
                enterprise_id,
            )
            return None
    return None


@router.post("/events")
async def slack_events(request: Request):
    body = await request.body()
    headers = {key: value for key, value in request.headers.items()}
    if not verify_slack_request(headers=headers, body=body):
        raise HTTPException(403, "Signature verification failed")

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid Slack event payload")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Slack event payload")

    if payload.get("type") == "url_verification":
        return PlainTextResponse(str(payload.get("challenge") or ""))
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True, "noop": True, "reason": "unsupported_envelope"})

    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        return JSONResponse({"ok": True, "noop": True, "reason": "missing_event_id"})

    async with async_session() as db:
        config = await _resolve_channel_config(db, payload)
        if config is None:
            return JSONResponse({
                "ok": True,
                "noop": True,
                "reason": "connection_not_found",
            })

        parsed = await _adapter.parse_inbound(
            config,
            headers=headers,
            query=dict(request.query_params),
            body=body,
        )
        if parsed is None:
            return JSONResponse({
                "ok": True,
                "noop": True,
                "reason": "unsupported_event",
            })

        entity_id = config.entity_id
        config_id = config.id
        try:
            inbound_log = await handle_inbound_message(
                db,
                entity_id=entity_id,
                channel_config_id=config_id,
                payload={
                    "from": parsed.source_id,
                    "to": parsed.reply_to,
                    "content": parsed.content,
                    # Slack's envelope event_id, rather than the message ts,
                    # is stable across delivery retries.
                    "external_id": event_id,
                    "channel_type": "slack",
                },
            )
        except IntegrityError:
            await db.rollback()
            duplicate = await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.channel_config_id == config_id,
                    MessageLog.direction == "inbound",
                    MessageLog.channel_type == "slack",
                    MessageLog.external_id == event_id,
                )
            )
            if duplicate is None:
                raise
            return JSONResponse({
                "ok": True,
                "noop": True,
                "reason": "duplicate_event",
            })

        try:
            dispatch_inbound_task.delay(
                entity_id=entity_id,
                channel_config_id=config_id,
                channel_type="slack",
                sender_id=parsed.source_id,
                sender_name=parsed.sender_name,
                chat_id=parsed.reply_to,
                content=parsed.content,
                thread_ts=parsed.thread_ts,
                inbound_message_log_id=inbound_log.id,
            )
        except Exception:
            await db.rollback()
            logger.exception("Could not enqueue Slack event %s", event_id)
            raise HTTPException(503, "Slack event queue unavailable")
        await db.commit()

    return JSONResponse({"ok": True})
