"""Twilio Voice Media Streams websocket endpoint.

Twilio dials our ``/voice`` TwiML → TwiML returns a ``<Connect><Stream>``
pointing to this endpoint → Twilio opens a websocket here and starts
streaming μ-law 8 kHz audio both ways.

Authentication: the ``config_id`` is in the URL path and validated against
the ChannelConfig table. For defense-in-depth, the Connect+Stream TwiML
includes a ``customParameter`` with a per-call nonce that we check on
``start`` (omitted here for brevity — add if spoofing becomes a concern).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.voice.session import TwilioVoiceSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/twilio_voice", tags=["channels"])


@router.websocket("/stream/{config_id}")
async def voice_stream(ws: WebSocket, config_id: str):
    # Verify the ChannelConfig exists before accepting
    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )).scalar_one_or_none()

    # Backward compatibility: older installs may only have twilio_sms
    # ChannelConfig rows even when /twilio/voice points at this stream.
    if not cc or cc.channel_type not in {"twilio_voice", "twilio_sms"}:
        await ws.close(code=4404)
        return

    await ws.accept()

    # Resolve the entity's chosen voice model up-front so the TTS engine
    # used for this whole call respects the user's Account → Voice pick.
    from packages.core.services.voice.tts import get_tts_engine, resolve_voice_model
    voice_model = await resolve_voice_model(cc.entity_id)
    session = TwilioVoiceSession(
        ws=ws,
        agent_callable=_voice_agent_call,
        channel_config_id=cc.id,
        entity_id=cc.entity_id,
        tts=get_tts_engine(model=voice_model),
    )
    try:
        await session.run()
    except WebSocketDisconnect:
        logger.info("Twilio Media Streams websocket disconnected")
    except Exception:
        logger.exception("Voice session crashed")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ── Agent dispatch for voice (synchronous — we need the reply text) ────────

async def _voice_agent_call(
    *,
    entity_id: str,
    channel_config_id: str,
    call_sid: str,
    from_number: str,
    text: str,
) -> str:
    """Run the gateway's agent inline and return the reply text.

    Unlike text channels (which enqueue on Celery and send the reply via
    adapter.send_text asynchronously), voice needs the reply BEFORE we
    can synthesise and stream audio back on the same websocket. So we
    call the gateway directly and wait.
    """
    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        channel_type="twilio_voice",
        sender_id=from_number,
        sender_name=None,
        chat_id=from_number,
        content=text,
    )
    # dispatch_inbound returns the reply text directly. The
    # TwilioVoiceChannelAdapter.send_text is a deferred-no-op for voice
    # (it can't push audio on an HTTP path), so we synthesise + stream
    # back on the caller's websocket instead.
    return (result or {}).get("reply") or ""
