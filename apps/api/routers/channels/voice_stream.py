"""Twilio Voice Media Streams websocket endpoint.

Twilio dials our ``/voice`` TwiML → TwiML returns a ``<Connect><Stream>``
pointing to this endpoint → Twilio opens a websocket here and starts
streaming μ-law 8 kHz audio both ways.

Authentication: a short-lived, single-use call-session token is in the URL
path. The raw token is never stored in the database and cannot be replayed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, TwilioVoiceCallSession
from packages.core.services.voice.call_sessions import (
    consume_call_session_token,
    finish_call_session,
    mark_call_connected,
)
from packages.core.services.voice.realtime import VoiceAgentOutcome
from packages.core.services.voice.session import TwilioVoiceSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/twilio_voice", tags=["channels"])

_VOICE_APPROVAL_REPLY = (
    "This action requires approval in Manor before it can continue."
)
_VOICE_ROUTE_ERROR_REPLY = "Sorry, I cannot complete that request right now."


@router.websocket("/stream/{session_token}")
async def voice_stream(ws: WebSocket, session_token: str):
    """Accept one authenticated Twilio Media Stream for a call session."""
    # Claim the token before accepting the socket. ``with_for_update`` in the
    # service makes two simultaneous Twilio connections deterministic.
    async with async_session() as db:
        call_session = await consume_call_session_token(db, session_token)
        if call_session is None:
            await ws.close(code=4401)
            return
        cc = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == call_session.channel_config_id,
                ChannelConfig.status == "active",
                ChannelConfig.channel_type == "twilio_voice",
            )
        )).scalar_one_or_none()
        if cc is None:
            await db.rollback()
            await ws.close(code=4404)
            return
        call_session_id = call_session.id
        entity_id = call_session.entity_id
        channel_config_id = cc.id
        billing_user_id = call_session.owner_user_id
        billing_workspace_id = call_session.workspace_id
        billing_agent_id = call_session.agent_id
        channel_binding_id = str(
            (call_session.metadata_json or {}).get("channel_binding_id") or ""
        ).strip() or None
        agent_subscription_id = str(
            (call_session.metadata_json or {}).get("agent_subscription_id") or ""
        ).strip() or None
        await db.commit()

    await ws.accept()

    from packages.core.services.voice.realtime import resolve_realtime_route

    try:
        realtime_route = await resolve_realtime_route(
            entity_id,
            user_id=billing_user_id,
        )
    except Exception as exc:
        logger.exception("Failed to initialize Twilio Voice engines")
        async with async_session() as db:
            current = await db.get(TwilioVoiceCallSession, call_session_id)
            if current is not None and current.status not in {
                "failed", "busy", "no_answer", "canceled", "expired",
            }:
                await finish_call_session(db, current, status="failed", error_message=str(exc))
                await db.commit()
        await ws.close(code=1011)
        return
    async def _on_connected(*, stream_sid: str | None, call_sid: str | None) -> None:
        async with async_session() as db:
            current = await db.get(TwilioVoiceCallSession, call_session_id)
            if current is None:
                return
            if current.call_sid and call_sid and current.call_sid != call_sid:
                await finish_call_session(
                    db,
                    current,
                    status="failed",
                    error_message="Twilio stream CallSid does not match call session",
                )
                await db.commit()
                raise ValueError("Twilio stream CallSid mismatch")
            await mark_call_connected(db, current, stream_sid=stream_sid)
            await db.commit()
    session = TwilioVoiceSession(
        ws=ws,
        agent_callable=_voice_agent_call,
        call_session_id=call_session_id,
        channel_config_id=channel_config_id,
        entity_id=entity_id,
        billing_user_id=billing_user_id,
        billing_workspace_id=billing_workspace_id,
        billing_agent_id=billing_agent_id,
        channel_binding_id=channel_binding_id,
        agent_subscription_id=agent_subscription_id,
        realtime_route=realtime_route,
        on_connected=_on_connected,
    )
    terminal_status = "completed"
    error_message: str | None = None
    try:
        await session.run()
    except WebSocketDisconnect:
        logger.info("Twilio Media Streams websocket disconnected")
    except Exception as exc:
        terminal_status = "failed"
        error_message = str(exc)
        logger.exception("Voice session crashed")
    finally:
        if getattr(session, "error_message", None):
            terminal_status = "failed"
            error_message = session.error_message
        try:
            async with async_session() as db:
                current = await db.get(TwilioVoiceCallSession, call_session_id)
                if current is not None and current.status not in {
                    "failed", "busy", "no_answer", "canceled", "expired",
                }:
                    duration = None
                    if current.connected_at:
                        ended_at = current.ended_at or datetime.now(timezone.utc)
                        duration = max(0, int((ended_at - current.connected_at).total_seconds()))
                    await finish_call_session(
                        db,
                        current,
                        status=terminal_status,
                        duration_seconds=duration,
                        error_message=error_message,
                    )
                    await db.commit()
        except Exception:
            logger.exception("Failed to persist Twilio Voice session completion")
        try:
            await ws.close()
        except Exception:
            pass


# ── Agent dispatch for voice (synchronous — we need the reply text) ────────

async def _voice_agent_call(
    *,
    entity_id: str,
    channel_config_id: str,
    channel_binding_id: str | None,
    agent_subscription_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    call_sid: str,
    from_number: str,
    text: str,
) -> VoiceAgentOutcome:
    """Run the gateway inline and return the constrained Voice outcome.

    Unlike text channels (which enqueue on Celery and send the reply via
    adapter.send_text asynchronously), voice needs the reply BEFORE we
    can synthesise and stream audio back on the same websocket. So we
    call the gateway directly and wait.
    """
    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        channel_binding_id=channel_binding_id,
        channel_agent_subscription_id=agent_subscription_id,
        channel_agent_id=agent_id,
        channel_workspace_id=workspace_id,
        channel_type="twilio_voice",
        sender_id=from_number,
        sender_name=None,
        chat_id=from_number,
        content=text,
        deliver_reply=False,
    )
    # The conversation/agent are resolved by the normal channel gateway on
    # the first spoken turn. Link them back to the durable call row so status
    # and cost views can follow a call into its Manor conversation.
    conversation_id = (result or {}).get("conversation_id")
    if conversation_id and call_sid:
        from packages.core.services.voice.call_sessions import get_call_session_by_sid

        async with async_session() as db:
            call_session = await get_call_session_by_sid(
                db,
                channel_config_id=channel_config_id,
                call_sid=call_sid,
            )
            if call_session is not None:
                call_session.conversation_id = str(conversation_id)
                if (result or {}).get("agent_id"):
                    call_session.agent_id = str(result["agent_id"])
                await db.commit()
    result = result or {}
    conversation_value = result.get("conversation_id")
    agent_value = result.get("agent_id")
    conversation_id = str(conversation_value) if conversation_value else None
    resolved_agent_id = str(agent_value) if agent_value else None
    status = str(result.get("status") or "error")
    ack_message = str(result.get("ack_message") or "").strip()
    if ack_message:
        return VoiceAgentOutcome(
            status="action_handled",
            spoken_reply=ack_message,
            conversation_id=conversation_id,
            agent_id=resolved_agent_id,
        )
    if status == "approval_required":
        return VoiceAgentOutcome(
            status="approval_required",
            spoken_reply=_VOICE_APPROVAL_REPLY,
            conversation_id=conversation_id,
            agent_id=resolved_agent_id,
        )
    if status == "ok":
        reply = str(result.get("reply") or "").strip()
        return VoiceAgentOutcome(
            status="ok" if reply else "no_reply",
            spoken_reply=reply,
            conversation_id=conversation_id,
            agent_id=resolved_agent_id,
        )
    if status == "no_reply":
        return VoiceAgentOutcome(
            status="no_reply",
            spoken_reply="",
            conversation_id=conversation_id,
            agent_id=resolved_agent_id,
        )
    return VoiceAgentOutcome(
        status="error",
        spoken_reply=_VOICE_ROUTE_ERROR_REPLY,
        conversation_id=conversation_id,
        agent_id=resolved_agent_id,
    )
