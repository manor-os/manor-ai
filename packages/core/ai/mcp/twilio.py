"""Twilio SMS, Voice, and account operations exposed through MCP.

Credentials arrive as the JSON blob leased by ``mcp_builtin``::

    {"account_sid": "AC...", "auth_token": "...", "phone_number": "+1..."}

``make_call`` creates a durable, short-lived Voice session and asks Twilio to
connect the callee to Manor's authenticated Media Stream. The same bound
Twilio Voice channel is used for SMS and Voice, but each operation keeps its
own transport and lifecycle records.
"""
from __future__ import annotations

import json
import contextvars
import os
from typing import Any, Dict, List

from packages.core.ai.mcp._http import mcp_err, mcp_ok
from packages.core.services.channels.twilio_adapter import TwilioAdapter


_call_context_var: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "twilio_mcp_call_context",
    default={},
)


def set_call_context(ctx: Dict[str, Any]) -> None:
    _call_context_var.set(dict(ctx or {}))


def clear_call_context() -> None:
    _call_context_var.set({})


def _call_context() -> Dict[str, Any]:
    return _call_context_var.get()


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_phone_numbers": {
        "description": "List purchased Twilio phone numbers and their capabilities.",
        "required": [],
        "properties": {},
    },
    "send_sms": {
        "description": "Send an SMS from the connected Twilio number.",
        "required": ["to", "body"],
        "properties": {
            "to": {"type": "string", "description": "Recipient E.164 phone number."},
            "body": {"type": "string", "description": "SMS body text."},
        },
    },
    "make_call": {
        "description": (
            "Place a Twilio voice call that connects the recipient to the bound "
            "Manor Agent over a live voice session."
        ),
        "required": ["to"],
        "properties": {
            "to": {"type": "string", "description": "Recipient E.164 phone number."},
        },
    },
    "get_usage": {
        "description": "Read Twilio usage records for a date range.",
        "required": ["start_date", "end_date"],
        "properties": {
            "start_date": {"type": "string", "description": "Start date YYYY-MM-DD."},
            "end_date": {"type": "string", "description": "End date YYYY-MM-DD."},
        },
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "required": spec["required"],
                "properties": spec["properties"],
            },
        }
        for name, spec in _TOOLS.items()
    ]


def _credentials(bearer_token: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(bearer_token) if bearer_token else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Twilio credentials are malformed; reconnect the integration.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Twilio credentials must be a JSON object.")
    return parsed


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _adapter(credentials: Dict[str, Any]) -> TwilioAdapter:
    account_sid = str(credentials.get("account_sid") or credentials.get("sid") or "").strip()
    auth_token = str(credentials.get("auth_token") or credentials.get("token") or "").strip()
    from_number = str(
        credentials.get("phone_number")
        or credentials.get("from_number")
        or credentials.get("from")
        or ""
    ).strip()
    if not account_sid or not auth_token:
        raise ValueError("Twilio is not configured — account_sid and auth_token are required.")
    if not from_number:
        raise ValueError("Twilio is not configured — phone_number is required for outbound SMS.")
    return TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
    )


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if spec is None:
        return mcp_err(f"Unknown Twilio tool: {name!r}")
    if not isinstance(arguments, dict):
        return mcp_err("arguments must be an object")
    args = dict(arguments)
    missing = [field for field in spec["required"] if _is_blank(args.get(field))]
    if missing:
        return mcp_err(f"Missing required params: {', '.join(missing)}")

    for field in spec["required"]:
        value = args.get(field)
        if isinstance(value, str):
            args[field] = value.strip()

    try:
        credentials = _credentials(bearer_token)
        adapter = _adapter(credentials)
        if name == "list_phone_numbers":
            result = await adapter.list_phone_numbers()
        elif name == "send_sms":
            result = await adapter.send_sms(str(args["to"]), str(args["body"]))
        elif name == "make_call":
            result = await _make_manor_voice_call(
                adapter,
                credentials=credentials,
                to=str(args["to"]),
            )
        elif name == "get_usage":
            result = await adapter.get_usage(str(args["start_date"]), str(args["end_date"]))
        else:  # pragma: no cover - guarded by _TOOLS
            return mcp_err(f"Unhandled Twilio tool: {name!r}")
    except (ValueError, RuntimeError) as exc:
        return mcp_err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return mcp_err(f"Twilio tool {name} failed: {exc}")
    return mcp_ok(result)


__all__ = ["call_tool", "list_tools"]


async def _make_manor_voice_call(
    adapter: TwilioAdapter,
    *,
    credentials: Dict[str, Any],
    to: str,
) -> Dict[str, Any]:
    """Create a durable Manor call session before invoking Twilio."""
    from sqlalchemy import or_, select

    from packages.core.config import get_settings
    from packages.core.database import async_session
    from packages.core.models.channel import (
        ChannelConfig,
        MessageLog,
        TwilioVoiceCallSession,
    )
    from packages.core.services.channel_message_logs import create_channel_outbound_log
    from packages.core.services.voice.call_sessions import (
        create_call_session,
        finish_call_session,
        get_call_session_for_update,
    )
    from packages.core.services.channel_bindings import (
        resolve_unique_channel_binding_scope,
    )

    context = _call_context()
    entity_id = str(context.get("entity_id") or "").strip()
    integration_id = str(context.get("integration_account_id") or "").strip()
    user_id = str(context.get("user_id") or "").strip()
    workspace_id = str(context.get("workspace_id") or "").strip() or None
    if not entity_id or not integration_id:
        raise ValueError("Twilio voice calls require a bound Manor integration context")

    base = str(get_settings().PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise ValueError("PUBLIC_BASE_URL is required for Manor voice calls")
    if not base.startswith("https://") and os.getenv("MANOR_ENV", "local").lower() not in {
        "local", "dev", "development", "test",
    }:
        raise ValueError("PUBLIC_BASE_URL must use HTTPS for Twilio voice calls")

    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.channel_type == "twilio_voice",
                ChannelConfig.status == "active",
                or_(
                    ChannelConfig.credential_source_id == integration_id,
                    ChannelConfig.config["integration_id"].astext == integration_id,
                ),
            ).limit(1)
        )).scalar_one_or_none()
        if cc is None or not cc.owner_user_id or cc.owner_user_id != user_id:
            raise ValueError("No active Twilio Voice channel is bound to this user")
        binding_scope = await resolve_unique_channel_binding_scope(
            db,
            cc,
            workspace_id=workspace_id,
        )
        if binding_scope is None:
            raise ValueError("No active Twilio Voice Agent binding matches this call")

        session, raw_token = await create_call_session(
            db,
            entity_id=entity_id,
            channel_config_id=cc.id,
            owner_user_id=cc.owner_user_id,
            workspace_id=binding_scope.workspace_id,
            direction="outbound",
            call_sid=None,
            from_number=adapter.from_number,
            to_number=to,
            agent_id=binding_scope.agent_id,
            metadata={
                "integration_id": integration_id,
                "requested_by_user_id": user_id,
                "channel_binding_id": binding_scope.binding.id,
                "agent_subscription_id": binding_scope.agent_subscription_id,
            },
        )
        log = await create_channel_outbound_log(
            db,
            entity_id=entity_id,
            channel_config_id=cc.id,
            channel_type="twilio_voice",
            to_address=to,
            content="Outbound Manor voice call",
            status="queued",
        )
        session.metadata_json = {
            **(session.metadata_json or {}),
            "message_log_id": log.id,
        }
        session_id = session.id
        log_id = log.id
        channel_config_id = cc.id
        twiml_url = f"{base}/api/v1/channels/twilio/voice/outbound/{raw_token}"
        status_url = (
            f"{base}/api/v1/channels/twilio/status"
            f"?config_id={channel_config_id}&session_id={session_id}"
        )
        await db.commit()

    try:
        result = await adapter.make_call(
            to,
            twiml_url,
            status_callback_url=status_url,
        )
        call_sid = str(result.get("external_id") or "").strip()
        if not call_sid:
            raise RuntimeError("Twilio accepted no CallSid for the outbound call")
    except Exception as exc:
        async with async_session() as db:
            session = await get_call_session_for_update(db, session_id)
            if session is not None:
                await finish_call_session(
                    db,
                    session,
                    status="failed",
                    error_message=str(exc),
                )
            log = (await db.execute(
                select(MessageLog)
                .where(MessageLog.id == log_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )).scalar_one_or_none()
            if (
                session is not None
                and session.status == "failed"
                and log is not None
                and log.status == "queued"
            ):
                log.status = "failed"
                log.error_message = str(exc)[:500]
            await db.commit()
        raise

    async with async_session() as db:
        session = (await db.execute(
            select(TwilioVoiceCallSession)
            .where(TwilioVoiceCallSession.id == session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )).scalar_one_or_none()
        log = (await db.execute(
            select(MessageLog)
            .where(MessageLog.id == log_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )).scalar_one_or_none()
        if session is None or log is None:
            raise RuntimeError("Outbound Twilio call intent disappeared after provider acceptance")
        if session.call_sid and session.call_sid != call_sid:
            raise RuntimeError("Twilio CallSid conflicts with the durable call session")
        session.call_sid = call_sid
        if session.status in {"pending", "connecting"}:
            session.status = "connecting"
        if log.external_id and log.external_id != call_sid:
            raise RuntimeError("Twilio CallSid conflicts with the outbound message log")
        log.external_id = call_sid
        if log.status == "queued":
            log.status = "queued"
        await db.commit()

    return {
        **result,
        "session_id": session_id,
        "channel_config_id": channel_config_id,
    }
