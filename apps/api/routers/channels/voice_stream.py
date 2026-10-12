"""Twilio Voice Media Streams websocket endpoint.

Twilio dials our ``/voice`` TwiML → TwiML returns a ``<Connect><Stream>``
pointing to this endpoint → Twilio opens a websocket here and starts
streaming μ-law 8 kHz audio both ways.

Authentication: a short-lived, single-use call-session token is in the URL
path. The raw token is never stored in the database and cannot be replayed.
"""
from __future__ import annotations

import asyncio
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


async def _prepare_voice_conversation(db, call_session: TwilioVoiceCallSession) -> str:
    """Create the exact channel conversation before any Voice work is accepted."""

    from packages.core.services.channel_contacts import upsert_channel_contact
    from packages.core.services.channel_conversations import (
        get_or_create_channel_conversation,
    )
    from packages.core.services.voice.binding import (
        resolve_twilio_call_binding_scope,
    )

    binding_scope = await resolve_twilio_call_binding_scope(db, call_session)
    if binding_scope is None:
        raise RuntimeError("Twilio Voice Agent binding changed")
    remote_number = str(
        (
            call_session.to_number
            if call_session.direction == "outbound"
            else call_session.from_number
        )
        or ""
    ).strip()
    if not remote_number:
        raise RuntimeError("Twilio Voice remote number is missing")
    contact = await upsert_channel_contact(
        db,
        entity_id=call_session.entity_id,
        channel_type="twilio_voice",
        channel_config_id=call_session.channel_config_id,
        source_id=remote_number,
        sender_name=None,
    )
    if contact.status == "blocked":
        raise RuntimeError("Twilio Voice caller is blocked")
    conversation = await get_or_create_channel_conversation(
        db,
        entity_id=call_session.entity_id,
        channel_type="twilio_voice",
        channel_config_id=call_session.channel_config_id,
        channel_contact_id=contact.id,
        sender_id=remote_number,
        sender_name=None,
        chat_id=remote_number,
        agent_id=binding_scope.agent_id,
        user_id=call_session.owner_user_id,
        workspace_id=binding_scope.workspace_id,
        agent_subscription_id=binding_scope.agent_subscription_id,
    )
    call_session.conversation_id = conversation.id
    await db.flush()
    return conversation.id


async def _admit_twilio_call_work(
    db,
    call_session: TwilioVoiceCallSession,
    text: str,
):
    """Persist one instruction only while the Call's original binding is valid."""

    from packages.core.services.voice.binding import (
        resolve_twilio_call_binding_scope,
    )
    from packages.core.services.voice.work_queue import admit_voice_work

    if await resolve_twilio_call_binding_scope(db, call_session) is None:
        raise RuntimeError("Twilio Voice Agent binding changed")
    if not call_session.conversation_id:
        raise RuntimeError("Twilio Voice conversation is missing")
    return await admit_voice_work(
        db,
        conversation_id=call_session.conversation_id,
        text=text,
        user_id=call_session.owner_user_id,
        public_channel=True,
        scope_id=call_session.id,
    )


async def _execute_twilio_call_work(
    call_session_id: str,
    receipt,
) -> VoiceAgentOutcome:
    """Run one admitted receipt through the Call's frozen channel scope."""

    from packages.core.services.voice.binding import (
        resolve_twilio_call_binding_scope,
    )
    from packages.core.services.voice.work_queue import (
        VoiceWorkNotPendingError,
        claim_voice_work,
        finish_voice_work,
    )

    async with async_session() as db:
        call_session = await db.get(TwilioVoiceCallSession, call_session_id)
        conversation_id = (
            call_session.conversation_id if call_session is not None else None
        )
        binding_scope = (
            await resolve_twilio_call_binding_scope(db, call_session)
            if call_session is not None
            else None
        )
        if (
            call_session is None
            or binding_scope is None
            or not conversation_id
            or receipt.scope_id != call_session.id
        ):
            if (
                call_session is not None
                and conversation_id
                and receipt.scope_id == call_session.id
            ):
                await finish_voice_work(
                    db,
                    receipt,
                    conversation_id=conversation_id,
                    state="interrupted",
                    error="Twilio Voice Agent binding changed.",
                )
            return VoiceAgentOutcome(
                status="cancelled",
                spoken_reply="",
                conversation_id=conversation_id,
                agent_id=call_session.agent_id if call_session is not None else None,
            )
        try:
            await claim_voice_work(
                db,
                receipt,
                conversation_id=conversation_id,
            )
        except VoiceWorkNotPendingError:
            return VoiceAgentOutcome(
                status="cancelled",
                spoken_reply="",
                conversation_id=conversation_id,
                agent_id=call_session.agent_id,
            )
        remote_number = str(
            (
                call_session.to_number
                if call_session.direction == "outbound"
                else call_session.from_number
            )
            or ""
        ).strip()
        agent_kwargs = {
            "entity_id": call_session.entity_id,
            "channel_config_id": call_session.channel_config_id,
            "channel_binding_id": binding_scope.channel_binding_id,
            "agent_subscription_id": binding_scope.agent_subscription_id,
            "agent_id": binding_scope.agent_id,
            "workspace_id": binding_scope.workspace_id,
            "call_sid": "",
            "from_number": remote_number,
            "text": receipt.text,
            "origin_message_id": receipt.message_id,
            "call_session_id": call_session.id,
        }

    try:
        outcome = await _voice_agent_call(**agent_kwargs)
    except asyncio.CancelledError:
        async with async_session() as db:
            await finish_voice_work(
                db,
                receipt,
                conversation_id=conversation_id,
                state="interrupted",
                error="Twilio Voice execution was cancelled.",
            )
        raise
    except BaseException as exc:
        async with async_session() as db:
            await finish_voice_work(
                db,
                receipt,
                conversation_id=conversation_id,
                state="failed",
                error=str(exc),
            )
        raise

    async with async_session() as db:
        current = await db.get(TwilioVoiceCallSession, call_session_id)
        binding_scope = (
            await resolve_twilio_call_binding_scope(db, current)
            if current is not None
            else None
        )
        binding_changed = (
            current is None
            or binding_scope is None
            or current.conversation_id != conversation_id
        )
        await finish_voice_work(
            db,
            receipt,
            conversation_id=conversation_id,
            state=(
                "interrupted"
                if binding_changed or outcome.status == "cancelled"
                else "failed"
                if outcome.status == "error"
                else "completed"
            ),
            error=(
                "Twilio Voice Agent binding changed."
                if binding_changed
                else outcome.spoken_reply
                if outcome.status == "error"
                else None
            ),
        )
    if binding_changed:
        return VoiceAgentOutcome(
            status="cancelled",
            spoken_reply="",
            conversation_id=conversation_id,
            agent_id=agent_kwargs["agent_id"],
        )
    return outcome


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
        try:
            conversation_id = await _prepare_voice_conversation(db, call_session)
        except Exception as exc:
            await finish_call_session(
                db,
                call_session,
                status="failed",
                error_message=str(exc),
            )
            await db.commit()
            await ws.close(code=4409)
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

    async def _admit_work(text: str):
        async with async_session() as db:
            current = await db.get(TwilioVoiceCallSession, call_session_id)
            if current is None or current.conversation_id != conversation_id:
                raise RuntimeError("Twilio Voice call session changed")
            return await _admit_twilio_call_work(db, current, text)

    async def _execute_work(receipt):
        return await _execute_twilio_call_work(call_session_id, receipt)

    async def _route_followup(text, active_receipt):
        from packages.core.services.voice.binding import (
            resolve_twilio_call_binding_scope,
        )
        from packages.core.services.voice.work_queue import voice_work_context
        from packages.core.services.voice.work_router import (
            classify_voice_work_followup,
        )

        async with async_session() as db:
            current = await db.get(TwilioVoiceCallSession, call_session_id)
            binding_scope = (
                await resolve_twilio_call_binding_scope(db, current)
                if current is not None
                else None
            )
            if (
                current is None
                or binding_scope is None
                or current.conversation_id != conversation_id
                or active_receipt.scope_id != current.id
            ):
                raise RuntimeError("Twilio Voice Agent binding changed")
            context = await voice_work_context(
                db,
                active_receipt,
                conversation_id=conversation_id,
            )
        return classify_voice_work_followup(text, context)

    async def _cancel_work(active_receipt, replacement_receipt=None):
        from packages.core.services.voice.work_queue import interrupt_voice_work

        async with async_session() as db:
            current = await db.get(TwilioVoiceCallSession, call_session_id)
            if (
                current is None
                or current.conversation_id != conversation_id
                or active_receipt.scope_id != current.id
                or (
                    replacement_receipt is not None
                    and replacement_receipt.scope_id != current.id
                )
            ):
                return False
            return await interrupt_voice_work(
                db,
                active_receipt,
                conversation_id=conversation_id,
                reason=(
                    "Replaced by a newer Twilio Voice instruction."
                    if replacement_receipt is not None
                    else "Cancelled by an explicit Twilio Voice instruction."
                ),
                superseded_by=(
                    replacement_receipt.id
                    if replacement_receipt is not None
                    else None
                ),
            )

    async def _record_control_turn(user_text: str, assistant_text: str) -> None:
        from packages.core.models.task import Conversation
        from packages.core.services.channel_conversations import (
            add_channel_assistant_message,
            add_channel_inbound_message,
        )

        async with async_session() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None:
                raise RuntimeError("Twilio Voice conversation is missing")
            conversation_meta = dict(conversation.meta or {})
            remote_number = str(
                conversation_meta.get("sender_id")
                or conversation_meta.get("chat_id")
                or ""
            )
            chat_id = conversation_meta.get("chat_id") or remote_number
            if user_text:
                await add_channel_inbound_message(
                    db,
                    conversation_id=conversation_id,
                    channel_type="twilio_voice",
                    sender_id=remote_number,
                    sender_name=conversation_meta.get("sender_name"),
                    chat_id=chat_id,
                    content=user_text,
                    meta={"voice_control": True},
                )
            if assistant_text:
                assistant = await add_channel_assistant_message(
                    db,
                    conversation_id=conversation_id,
                    channel_type="twilio_voice",
                    chat_id=chat_id,
                    content=assistant_text,
                    runtime_meta={"voice_control": True},
                    author_kind="agent",
                    author_subscription_id=agent_subscription_id,
                )
                assistant.meta = {
                    **dict(assistant.meta or {}),
                    "voice_control": True,
                }
            await db.commit()

    session = TwilioVoiceSession(
        ws=ws,
        agent_callable=_voice_agent_call,
        call_session_id=call_session_id,
        channel_config_id=channel_config_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        billing_user_id=billing_user_id,
        billing_workspace_id=billing_workspace_id,
        billing_agent_id=billing_agent_id,
        channel_binding_id=channel_binding_id,
        agent_subscription_id=agent_subscription_id,
        realtime_route=realtime_route,
        on_connected=_on_connected,
        admit_work=_admit_work,
        execute_work=_execute_work,
        route_followup=_route_followup,
        cancel_work=_cancel_work,
        record_control_turn=_record_control_turn,
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
    origin_message_id: str | None = None,
    call_session_id: str | None = None,
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
        runtime_metadata={
            "voice_session_mode": "chat_gateway",
            **(
                {"voice_origin_message_id": origin_message_id}
                if origin_message_id
                else {}
            ),
            **(
                {"twilio_call_session_id": call_session_id}
                if call_session_id
                else {}
            ),
        },
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
    if status == "cancelled":
        return VoiceAgentOutcome(
            status="cancelled",
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
