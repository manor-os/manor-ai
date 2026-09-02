"""Exact Agent binding scope for one durable Twilio Voice call."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.models.channel import ChannelConfig, TwilioVoiceCallSession
from packages.core.models.workspace import Agent
from packages.core.services.agent_subscription_service import resolve_subscription
from packages.core.services.channel_bindings import (
    channel_workspace_is_routable,
    load_channel_binding_by_id_for_config,
)


@dataclass(frozen=True)
class TwilioCallBindingScope:
    channel_binding_id: str
    agent_subscription_id: str | None
    agent_id: str
    workspace_id: str | None


async def resolve_twilio_call_binding_scope(
    db: AsyncSession,
    call_session: TwilioVoiceCallSession,
) -> TwilioCallBindingScope | None:
    """Return the original Call scope only while every binding edge still matches."""

    metadata = call_session.metadata_json if isinstance(call_session.metadata_json, dict) else {}
    binding_id = str(metadata.get("channel_binding_id") or "").strip()
    if not binding_id or not call_session.agent_id:
        return None
    config = await db.get(ChannelConfig, call_session.channel_config_id)
    if (
        config is None
        or config.entity_id != call_session.entity_id
        or config.channel_type != "twilio_voice"
        or config.status != "active"
        or config.owner_user_id != call_session.owner_user_id
    ):
        return None
    binding = await load_channel_binding_by_id_for_config(db, config, binding_id)
    if binding is None or binding.user_id != call_session.owner_user_id:
        return None
    resolved = await resolve_subscription(db, binding=binding, contact=None)
    expected_subscription_id = str(
        metadata.get("agent_subscription_id") or ""
    ).strip() or None
    if (
        resolved.source == "invalid"
        or resolved.agent_id != call_session.agent_id
        or resolved.workspace_id != call_session.workspace_id
        or resolved.id != expected_subscription_id
        or binding.workspace_id != call_session.workspace_id
    ):
        return None
    if not is_master_agent(resolved.agent_id):
        active_agent_id = await db.scalar(
            select(Agent.id).where(
                Agent.id == resolved.agent_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )
        if active_agent_id is None:
            return None
    if not await channel_workspace_is_routable(
        db,
        call_session.workspace_id,
        entity_id=call_session.entity_id,
    ):
        return None
    return TwilioCallBindingScope(
        channel_binding_id=binding.id,
        agent_subscription_id=resolved.id,
        agent_id=resolved.agent_id,
        workspace_id=resolved.workspace_id,
    )


async def twilio_call_binding_is_valid(
    db: AsyncSession,
    call_session_id: str,
    *,
    conversation_id: str,
) -> bool:
    """Revalidate the frozen binding at an Agent cooperative-cancel checkpoint."""

    call_session = await db.get(TwilioVoiceCallSession, call_session_id)
    return bool(
        call_session is not None
        and call_session.conversation_id == conversation_id
        and await resolve_twilio_call_binding_scope(db, call_session) is not None
    )
