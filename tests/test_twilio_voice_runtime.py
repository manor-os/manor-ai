"""Twilio Voice runtime contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, TwilioVoiceCallSession
from packages.core.models.document import Channel
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Agent, AgentSubscription, Workspace


async def _create_bound_voice_call(
    db_session,
    *,
    suffix: str,
    agent_id: str | None = None,
    create_agent_row: bool = True,
):
    from packages.core.services.voice.call_sessions import create_call_session

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    resolved_agent_id = agent_id or generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name=f"Voice workspace {suffix}",
        status="active",
    )
    agent = Agent(
        id=resolved_agent_id,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        name=f"Voice Agent {suffix}",
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=resolved_agent_id,
        workspace_id=workspace.id,
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="twilio_voice",
        name="Twilio Voice",
        agent_id=resolved_agent_id,
        agent_subscription_id=subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    rows = [workspace, subscription, config, binding]
    if create_agent_row:
        rows.insert(1, agent)
    db_session.add_all(rows)
    await db_session.flush()
    call_session, raw_token = await create_call_session(
        db_session,
        entity_id=entity_id,
        channel_config_id=config.id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        direction="inbound",
        call_sid=f"CA-{suffix}",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=resolved_agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    await db_session.commit()
    return call_session, raw_token, binding, subscription


@pytest.mark.asyncio
async def test_voice_session_token_is_hashed_and_single_use(db_session):
    from packages.core.services.voice.call_sessions import (
        consume_call_session_token,
        create_call_session,
        hash_call_session_token,
    )

    session, raw_token = await create_call_session(
        db_session,
        entity_id="entity-voice",
        channel_config_id="config-voice",
        owner_user_id="user-voice",
        workspace_id="workspace-voice",
        direction="inbound",
        call_sid="CA-token-test",
        from_number="+14155550199",
        to_number="+14155550110",
    )
    await db_session.commit()

    assert session.session_token_hash == hash_call_session_token(raw_token)
    assert raw_token not in session.session_token_hash
    assert session.owner_user_id == "user-voice"
    assert session.workspace_id == "workspace-voice"

    consumed = await consume_call_session_token(db_session, raw_token)
    assert consumed is not None
    await db_session.commit()

    assert await consume_call_session_token(db_session, raw_token) is None


@pytest.mark.asyncio
async def test_twilio_call_binding_scope_does_not_migrate_after_rebind(db_session):
    from packages.core.services.voice.binding import resolve_twilio_call_binding_scope
    from packages.core.services.voice.call_sessions import create_call_session

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    original_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Original voice workspace",
        status="active",
    )
    replacement_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Replacement voice workspace",
        status="active",
    )
    original_agent = Agent(
        id="agent-original",
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        name="Original voice Agent",
        status="active",
    )
    replacement_agent = Agent(
        id="agent-replacement",
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        name="Replacement voice Agent",
        status="active",
    )
    original_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id="agent-original",
        workspace_id=original_workspace.id,
        status="active",
    )
    replacement_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id="agent-replacement",
        workspace_id=replacement_workspace.id,
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=original_workspace.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=original_workspace.id,
        type="twilio_voice",
        name="Twilio Voice",
        agent_id=original_subscription.agent_id,
        agent_subscription_id=original_subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([
        original_workspace,
        replacement_workspace,
        original_agent,
        replacement_agent,
        original_subscription,
        replacement_subscription,
        config,
        binding,
    ])
    await db_session.flush()
    call_session, _ = await create_call_session(
        db_session,
        entity_id=entity_id,
        channel_config_id=config.id,
        owner_user_id=owner_user_id,
        workspace_id=original_workspace.id,
        direction="inbound",
        call_sid="CA-binding-snapshot",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=original_subscription.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": original_subscription.id,
        },
    )
    await db_session.commit()

    assert await resolve_twilio_call_binding_scope(db_session, call_session)

    binding.workspace_id = replacement_workspace.id
    binding.agent_id = replacement_subscription.agent_id
    binding.agent_subscription_id = replacement_subscription.id
    await db_session.commit()

    assert await resolve_twilio_call_binding_scope(db_session, call_session) is None


@pytest.mark.asyncio
async def test_twilio_call_binding_scope_requires_an_active_agent(db_session):
    from packages.core.services.voice.binding import resolve_twilio_call_binding_scope

    call_session, _raw_token, _binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="inactive-agent",
    )
    assert await resolve_twilio_call_binding_scope(db_session, call_session)

    agent = await db_session.get(Agent, subscription.agent_id)
    assert agent is not None
    agent.status = "inactive"
    await db_session.commit()

    assert await resolve_twilio_call_binding_scope(db_session, call_session) is None


@pytest.mark.asyncio
async def test_twilio_call_binding_scope_accepts_virtual_master_without_agent_row(
    db_session,
):
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.services.voice.binding import resolve_twilio_call_binding_scope

    call_session, _raw_token, _binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="master-agent",
        agent_id=MANOR_AGENT_ID,
        create_agent_row=False,
    )

    assert await db_session.get(Agent, MANOR_AGENT_ID) is None
    scope = await resolve_twilio_call_binding_scope(db_session, call_session)

    assert scope is not None
    assert scope.agent_id == MANOR_AGENT_ID
    assert scope.agent_subscription_id == subscription.id


@pytest.mark.asyncio
async def test_twilio_voice_prepares_conversation_before_durable_admission(db_session):
    from apps.api.routers.channels.voice_stream import _prepare_voice_conversation
    from packages.core.services.voice.call_sessions import create_call_session

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Voice workspace",
        status="active",
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        name="Voice Agent",
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace.id,
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="twilio_voice",
        name="Twilio Voice",
        agent_id=subscription.agent_id,
        agent_subscription_id=subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([workspace, agent, subscription, config, binding])
    await db_session.flush()
    call_session, _ = await create_call_session(
        db_session,
        entity_id=entity_id,
        channel_config_id=config.id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        direction="inbound",
        call_sid="CA-prepared-conversation",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=subscription.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    await db_session.commit()

    conversation_id = await _prepare_voice_conversation(db_session, call_session)
    await db_session.commit()

    assert conversation_id
    assert call_session.conversation_id == conversation_id
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    assert conversation.agent_id == subscription.agent_id
    assert conversation.workspace_id == workspace.id
    assert conversation.channel == "twilio_voice"


@pytest.mark.asyncio
async def test_twilio_voice_rejects_outbound_call_without_destination(db_session):
    from apps.api.routers.channels.voice_stream import _prepare_voice_conversation

    call_session, _raw_token, _binding, _subscription = await _create_bound_voice_call(
        db_session,
        suffix="outbound-missing-destination",
    )
    call_session.direction = "outbound"
    call_session.from_number = "+14155550110"
    call_session.to_number = None
    await db_session.commit()

    with pytest.raises(RuntimeError, match="remote number is missing"):
        await _prepare_voice_conversation(db_session, call_session)


@pytest.mark.asyncio
async def test_twilio_voice_admission_is_scoped_to_the_frozen_call_binding(db_session):
    from apps.api.routers.channels.voice_stream import (
        _admit_twilio_call_work,
        _prepare_voice_conversation,
    )
    from packages.core.services.voice.call_sessions import create_call_session
    from packages.core.services.voice.work_queue import VOICE_WORK_META_KEY

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Voice admission workspace",
        status="active",
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        name="Voice admission Agent",
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace.id,
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="twilio_voice",
        name="Twilio Voice",
        agent_id=agent.id,
        agent_subscription_id=subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([workspace, agent, subscription, config, binding])
    await db_session.flush()
    call_session, _ = await create_call_session(
        db_session,
        entity_id=entity_id,
        channel_config_id=config.id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        direction="inbound",
        call_sid="CA-admit-frozen-binding",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=agent.id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    await _prepare_voice_conversation(db_session, call_session)
    await db_session.commit()

    receipt = await _admit_twilio_call_work(
        db_session,
        call_session,
        "Check the deployment status",
    )

    message = await db_session.get(Message, receipt.message_id)
    assert receipt.scope_id == call_session.id
    assert message is not None
    assert message.meta[VOICE_WORK_META_KEY]["scope_id"] == call_session.id

    binding.status = "inactive"
    await db_session.commit()

    with pytest.raises(RuntimeError, match="binding changed"):
        await _admit_twilio_call_work(
            db_session,
            call_session,
            "This must not be admitted",
        )


@pytest.mark.asyncio
async def test_twilio_voice_agent_reuses_the_durable_origin_message(monkeypatch):
    from apps.api.routers.channels import voice_stream

    captured = {}

    async def fake_dispatch_inbound(**kwargs):
        captured.update(kwargs)
        return {
            "status": "ok",
            "reply": "Deployment is healthy.",
            "conversation_id": "conversation-voice",
            "agent_id": "agent-voice",
        }

    monkeypatch.setattr(
        "packages.core.services.channel_gateway.dispatch_inbound",
        fake_dispatch_inbound,
    )

    outcome = await voice_stream._voice_agent_call(
        entity_id="entity-voice",
        channel_config_id="config-voice",
        channel_binding_id="binding-voice",
        agent_subscription_id="subscription-voice",
        agent_id="agent-voice",
        workspace_id="workspace-voice",
        call_sid="",
        from_number="+14155550199",
        text="Check the deployment status",
        origin_message_id="message-voice",
        call_session_id="call-session-voice",
    )

    assert outcome.status == "ok"
    assert captured["runtime_metadata"] == {
        "voice_session_mode": "chat_gateway",
        "voice_origin_message_id": "message-voice",
        "twilio_call_session_id": "call-session-voice",
    }


@pytest.mark.asyncio
async def test_twilio_voice_executes_one_durable_receipt_on_the_frozen_scope(
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import voice_stream
    from packages.core.services.voice.work_queue import VOICE_WORK_META_KEY
    from packages.core.services.voice.realtime import VoiceAgentOutcome

    call_session, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="durable-execution",
    )
    conversation_id = await voice_stream._prepare_voice_conversation(
        db_session,
        call_session,
    )
    await db_session.commit()
    receipt = await voice_stream._admit_twilio_call_work(
        db_session,
        call_session,
        "Check the deployment status",
    )
    captured = {}

    async def fake_voice_agent_call(**kwargs):
        captured.update(kwargs)
        return VoiceAgentOutcome(
            status="ok",
            spoken_reply="Deployment is healthy.",
            conversation_id=conversation_id,
            agent_id=subscription.agent_id,
        )

    monkeypatch.setattr(voice_stream, "_voice_agent_call", fake_voice_agent_call)

    outcome = await voice_stream._execute_twilio_call_work(
        call_session.id,
        receipt,
    )

    assert outcome.status == "ok"
    assert captured["channel_binding_id"] == binding.id
    assert captured["agent_subscription_id"] == subscription.id
    assert captured["agent_id"] == subscription.agent_id
    assert captured["workspace_id"] == call_session.workspace_id
    assert captured["origin_message_id"] == receipt.message_id
    assert captured["call_session_id"] == call_session.id
    db_session.expire_all()
    messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation_id)
        )
    ).scalars().all()
    user_messages = [message for message in messages if message.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].id == receipt.message_id
    assert user_messages[0].meta[VOICE_WORK_META_KEY]["state"] == "completed"


@pytest.mark.asyncio
async def test_twilio_voice_drops_agent_result_after_the_call_binding_changes(
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import voice_stream
    from packages.core.services.voice.realtime import VoiceAgentOutcome
    from packages.core.services.voice.work_queue import VOICE_WORK_META_KEY

    call_session, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="drop-stale-binding-result",
    )
    await voice_stream._prepare_voice_conversation(db_session, call_session)
    await db_session.commit()
    receipt = await voice_stream._admit_twilio_call_work(
        db_session,
        call_session,
        "Run the report",
    )
    binding_id = binding.id

    async def fake_voice_agent_call(**_kwargs):
        async with db_module.async_session() as mutation_db:
            current_binding = await mutation_db.get(Channel, binding_id)
            assert current_binding is not None
            current_binding.status = "inactive"
            await mutation_db.commit()
        return VoiceAgentOutcome(
            status="ok",
            spoken_reply="This stale result must not be spoken.",
            conversation_id=call_session.conversation_id,
            agent_id=subscription.agent_id,
        )

    monkeypatch.setattr(voice_stream, "_voice_agent_call", fake_voice_agent_call)

    outcome = await voice_stream._execute_twilio_call_work(
        call_session.id,
        receipt,
    )

    assert outcome.status == "cancelled"
    assert outcome.spoken_reply == ""
    db_session.expire_all()
    message = await db_session.get(Message, receipt.message_id)
    assert message is not None
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"


@pytest.mark.asyncio
async def test_twilio_voice_interrupts_pending_receipt_when_binding_changes_before_claim(
    db_session,
):
    from apps.api.routers.channels import voice_stream
    from packages.core.services.voice.work_queue import VOICE_WORK_META_KEY

    call_session, _raw_token, binding, _subscription = await _create_bound_voice_call(
        db_session,
        suffix="binding-change-before-claim",
    )
    conversation_id = await voice_stream._prepare_voice_conversation(
        db_session,
        call_session,
    )
    receipt = await voice_stream._admit_twilio_call_work(
        db_session,
        call_session,
        "Run the report",
    )
    binding.status = "inactive"
    await db_session.commit()

    outcome = await voice_stream._execute_twilio_call_work(
        call_session.id,
        receipt,
    )

    assert outcome.status == "cancelled"
    assert outcome.spoken_reply == ""
    assert outcome.conversation_id == conversation_id
    db_session.expire_all()
    message = await db_session.get(Message, receipt.message_id)
    assert message is not None
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"


@pytest.mark.asyncio
async def test_twilio_voice_skips_receipt_interrupted_before_claim(db_session):
    from apps.api.routers.channels import voice_stream
    from packages.core.services.voice.work_queue import interrupt_voice_work

    call_session, _raw_token, _binding, _subscription = await _create_bound_voice_call(
        db_session,
        suffix="interrupted-before-claim",
    )
    conversation_id = await voice_stream._prepare_voice_conversation(
        db_session,
        call_session,
    )
    receipt = await voice_stream._admit_twilio_call_work(
        db_session,
        call_session,
        "Run the obsolete report",
    )
    assert await interrupt_voice_work(
        db_session,
        receipt,
        conversation_id=conversation_id,
        reason="Replaced by a newer Twilio Voice instruction.",
    )

    outcome = await voice_stream._execute_twilio_call_work(
        call_session.id,
        receipt,
    )

    assert outcome.status == "cancelled"
    assert outcome.spoken_reply == ""
    assert outcome.conversation_id == conversation_id


@pytest.mark.asyncio
async def test_twilio_rebind_cancels_unconnected_calls_but_preserves_active_history(
    db_session,
):
    from packages.core.services.integration_service import upsert_channel_binding
    from packages.core.services.voice.call_sessions import create_call_session

    pending, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="rebind-call-lifecycle",
    )
    active, _ = await create_call_session(
        db_session,
        entity_id=pending.entity_id,
        channel_config_id=pending.channel_config_id,
        owner_user_id=pending.owner_user_id,
        workspace_id=pending.workspace_id,
        direction="inbound",
        call_sid="CA-rebind-active-history",
        from_number="+14155550200",
        to_number="+14155550110",
        agent_id=subscription.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    active.status = "in_progress"
    active.connected_at = datetime.now(timezone.utc)
    await db_session.commit()
    pending_id = pending.id
    active_id = active.id

    await upsert_channel_binding(
        db_session,
        entity_id=pending.entity_id,
        user_id=pending.owner_user_id,
        channel_config_id=pending.channel_config_id,
        agent_id=None,
    )
    await db_session.commit()

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    saved_active = await db_session.get(TwilioVoiceCallSession, active_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"
    assert saved_active is not None
    assert saved_active.status == "in_progress"


@pytest.mark.asyncio
async def test_twilio_reactivating_binding_cancels_unconnected_calls(db_session):
    from packages.core.services.integration_service import upsert_channel_binding

    pending, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="reactivate-binding",
    )
    binding.status = "inactive"
    await db_session.commit()
    pending_id = pending.id
    binding_id = binding.id

    await upsert_channel_binding(
        db_session,
        entity_id=pending.entity_id,
        user_id=pending.owner_user_id,
        user_role="owner",
        channel_config_id=pending.channel_config_id,
        agent_id=subscription.agent_id,
        agent_subscription_id=subscription.id,
    )
    await db_session.commit()

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    saved_binding = await db_session.get(Channel, binding_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"
    assert saved_binding is not None
    assert saved_binding.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_legacy_twilio_channel_writers_cancel_unconnected_calls(
    db_session,
    operation,
):
    from packages.core.services.integration_service import (
        delete_channel,
        update_channel,
    )

    pending, _raw_token, binding, _subscription = await _create_bound_voice_call(
        db_session,
        suffix=f"legacy-channel-{operation}",
    )
    pending_id = pending.id
    if operation == "update":
        updated = await update_channel(
            db_session,
            binding.id,
            pending.entity_id,
            pending.owner_user_id,
            status="inactive",
        )
        assert updated is not None
    else:
        assert await delete_channel(
            db_session,
            binding.id,
            pending.entity_id,
            pending.owner_user_id,
        )
    await db_session.commit()

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["attach", "update", "remove"])
async def test_workspace_twilio_binding_writers_cancel_only_unconnected_calls(
    db_session,
    monkeypatch,
    operation,
):
    from apps.api.routers import workspaces as routes
    from packages.core.services.voice.call_sessions import create_call_session

    pending, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix=f"workspace-{operation}",
    )
    active, _ = await create_call_session(
        db_session,
        entity_id=pending.entity_id,
        channel_config_id=pending.channel_config_id,
        owner_user_id=pending.owner_user_id,
        workspace_id=pending.workspace_id,
        direction="inbound",
        call_sid=f"CA-workspace-{operation}-active",
        from_number="+14155550200",
        to_number="+14155550110",
        agent_id=subscription.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    active.status = "in_progress"
    active.connected_at = datetime.now(timezone.utc)
    replacement_agent = Agent(
        id=generate_ulid(),
        entity_id=pending.entity_id,
        owner_user_id=pending.owner_user_id,
        name=f"Replacement Agent {operation}",
        status="active",
    )
    replacement_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=pending.entity_id,
        agent_id=replacement_agent.id,
        workspace_id=pending.workspace_id,
        status="active",
    )
    db_session.add_all([replacement_agent, replacement_subscription])
    await db_session.commit()
    pending_id = pending.id
    active_id = active.id
    workspace = await db_session.get(Workspace, pending.workspace_id)
    assert workspace is not None
    user = SimpleNamespace(id=pending.owner_user_id, entity_id=pending.entity_id)
    monkeypatch.setattr(
        routes,
        "_require_workspace_manage",
        AsyncMock(return_value=workspace),
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_service.record_activity",
        AsyncMock(),
    )

    if operation == "attach":
        await routes.attach_workspace_channel(
            workspace.id,
            routes.WorkspaceChannelRequest(
                channel_config_id=pending.channel_config_id,
                agent_subscription_id=replacement_subscription.id,
            ),
            user,
            db_session,
        )
    elif operation == "update":
        await routes.update_workspace_channel(
            workspace.id,
            binding.id,
            routes.WorkspaceChannelUpdateRequest(
                agent_subscription_id=replacement_subscription.id,
            ),
            user,
            db_session,
        )
    else:
        await routes.remove_workspace_channel(
            workspace.id,
            binding.id,
            user,
            db_session,
        )

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    saved_active = await db_session.get(TwilioVoiceCallSession, active_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"
    assert saved_active is not None
    assert saved_active.status == "in_progress"


@pytest.mark.asyncio
async def test_workspace_operation_twilio_rebind_cancels_unconnected_call(
    db_session,
):
    from packages.core.services.workspace_operation_service import (
        _sync_channels_from_operation_state,
    )

    pending, _raw_token, binding, subscription = await _create_bound_voice_call(
        db_session,
        suffix="workspace-operation-rebind",
    )
    subscription.service_key = "original"
    replacement_agent = Agent(
        id=generate_ulid(),
        entity_id=pending.entity_id,
        owner_user_id=pending.owner_user_id,
        name="Workspace operation replacement Agent",
        status="active",
    )
    replacement_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=pending.entity_id,
        agent_id=replacement_agent.id,
        workspace_id=pending.workspace_id,
        service_key="replacement",
        status="active",
    )
    db_session.add_all([replacement_agent, replacement_subscription])
    await db_session.commit()
    pending_id = pending.id
    binding_id = binding.id
    replacement_agent_id = replacement_agent.id
    replacement_subscription_id = replacement_subscription.id
    workspace = await db_session.get(Workspace, pending.workspace_id)
    assert workspace is not None

    await _sync_channels_from_operation_state(
        db_session,
        workspace,
        {
            "channels": [
                {
                    "channel_type": "twilio_voice",
                    "provider": "twilio",
                    "channel_config_id": pending.channel_config_id,
                    "role": "primary_external",
                    "linked_service_key": "replacement",
                }
            ]
        },
    )
    await db_session.commit()

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    saved_binding = await db_session.get(Channel, binding_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"
    assert saved_binding is not None
    assert saved_binding.agent_id == replacement_agent_id
    assert saved_binding.agent_subscription_id == replacement_subscription_id


@pytest.mark.asyncio
async def test_voice_stream_prepares_and_wires_durable_twilio_work(
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import voice_stream

    call_session, raw_token, _binding, _subscription = await _create_bound_voice_call(
        db_session,
        suffix="stream-wiring",
    )
    call_session_id = call_session.id
    captured = {}

    class FakeWebSocket:
        def __init__(self):
            self.accepted = False
            self.closed = False

        async def accept(self):
            self.accepted = True

        async def close(self, **_kwargs):
            self.closed = True

    class FakeVoiceSession:
        error_message = None

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return None

    async def fake_resolve_realtime_route(*_args, **_kwargs):
        return SimpleNamespace(byok=True)

    monkeypatch.setattr(voice_stream, "TwilioVoiceSession", FakeVoiceSession)
    monkeypatch.setattr(
        "packages.core.services.voice.realtime.resolve_realtime_route",
        fake_resolve_realtime_route,
    )
    ws = FakeWebSocket()

    await voice_stream.voice_stream(ws, raw_token)

    assert ws.accepted is True
    assert ws.closed is True
    assert callable(captured["admit_work"])
    assert callable(captured["execute_work"])
    assert callable(captured["route_followup"])
    assert callable(captured["cancel_work"])
    assert callable(captured["record_control_turn"])
    db_session.expire_all()
    saved = await db_session.get(TwilioVoiceCallSession, call_session_id)
    assert saved is not None
    assert saved.conversation_id
    assert captured["conversation_id"] == saved.conversation_id


@pytest.mark.asyncio
async def test_expired_voice_session_token_is_rejected(db_session):
    from packages.core.services.voice.call_sessions import (
        consume_call_session_token,
        create_call_session,
    )

    session, raw_token = await create_call_session(
        db_session,
        entity_id="entity-voice",
        channel_config_id="config-voice",
        direction="outbound",
        call_sid=None,
        from_number="+14155550110",
        to_number="+14155550199",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await db_session.commit()

    assert await consume_call_session_token(db_session, raw_token) is None
    assert session.status == "expired"


@pytest.mark.asyncio
async def test_terminal_voice_session_status_is_not_rewritten(db_session):
    from packages.core.services.voice.call_sessions import (
        create_call_session,
        finish_call_session,
    )

    session, _ = await create_call_session(
        db_session,
        entity_id="entity-voice-terminal",
        channel_config_id="config-voice-terminal",
        direction="inbound",
        call_sid="CA-terminal-order",
        from_number="+14155550199",
        to_number="+14155550110",
    )
    await finish_call_session(db_session, session, status="failed", error_message="provider error")
    await finish_call_session(db_session, session, status="completed")

    assert session.status == "failed"
    assert session.error_message == "provider error"


@pytest.mark.asyncio
async def test_stale_voice_session_cannot_revive_canceled_call(db_session):
    import packages.core.database as db_module
    from packages.core.services.voice.call_sessions import (
        create_call_session,
        finish_call_session,
        mark_call_connected,
    )

    session, _ = await create_call_session(
        db_session,
        entity_id="entity-voice-canceled-race",
        channel_config_id="config-voice-canceled-race",
        direction="inbound",
        call_sid="CA-canceled-race",
        from_number="+14155550199",
        to_number="+14155550110",
    )
    await db_session.commit()
    session_id = session.id

    async with db_module.async_session() as stale_db:
        stale = await stale_db.get(TwilioVoiceCallSession, session_id)
        assert stale is not None
        async with db_module.async_session() as cancel_db:
            current = await cancel_db.get(TwilioVoiceCallSession, session_id)
            assert current is not None
            await finish_call_session(cancel_db, current, status="canceled")
            await cancel_db.commit()

        with pytest.raises(ValueError, match="terminal"):
            await mark_call_connected(stale_db, stale, stream_sid="stream-stale")
        await stale_db.rollback()

    db_session.expire_all()
    refreshed = await db_session.get(TwilioVoiceCallSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "canceled"
    assert refreshed.stream_sid is None


@pytest.mark.asyncio
async def test_outbound_twiml_transition_cannot_revive_call_canceled_after_lookup(
    db_session,
):
    import packages.core.database as db_module
    from packages.core.services.voice.call_sessions import (
        cancel_pending_call_sessions,
        create_call_session,
        get_call_session_by_token,
        mark_outbound_call_connecting,
    )

    session, raw_token = await create_call_session(
        db_session,
        entity_id="entity-outbound-twiml-race",
        channel_config_id="config-outbound-twiml-race",
        direction="outbound",
        call_sid="CA-outbound-twiml-race",
        from_number="+14155550110",
        to_number="+14155550199",
    )
    await db_session.commit()
    session_id = session.id

    async with db_module.async_session() as stale_db:
        assert await get_call_session_by_token(stale_db, raw_token) is not None
        async with db_module.async_session() as cancel_db:
            assert await cancel_pending_call_sessions(
                cancel_db,
                channel_config_ids=["config-outbound-twiml-race"],
                reason="integration disconnected",
            ) == 1
            await cancel_db.commit()

        assert await mark_outbound_call_connecting(stale_db, raw_token) is None
        await stale_db.commit()

    db_session.expire_all()
    refreshed = await db_session.get(TwilioVoiceCallSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "canceled"


@pytest.mark.asyncio
async def test_stale_voice_session_cannot_overwrite_terminal_failure(db_session):
    import packages.core.database as db_module
    from packages.core.services.voice.call_sessions import (
        create_call_session,
        finish_call_session,
    )

    session, _ = await create_call_session(
        db_session,
        entity_id="entity-voice-terminal-race",
        channel_config_id="config-voice-terminal-race",
        direction="outbound",
        call_sid="CA-terminal-race",
        from_number="+14155550110",
        to_number="+14155550199",
    )
    await db_session.commit()
    session_id = session.id

    async with db_module.async_session() as stale_db:
        stale = await stale_db.get(TwilioVoiceCallSession, session_id)
        assert stale is not None
        async with db_module.async_session() as failure_db:
            current = await failure_db.get(TwilioVoiceCallSession, session_id)
            assert current is not None
            await finish_call_session(
                failure_db,
                current,
                status="failed",
                error_message="provider failed",
            )
            await failure_db.commit()

        await finish_call_session(stale_db, stale, status="completed")
        await stale_db.commit()

    db_session.expire_all()
    refreshed = await db_session.get(TwilioVoiceCallSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error_message == "provider failed"


def test_voice_stream_twiml_uses_session_token_and_xml_escapes_values():
    from packages.core.services.voice.twiml import build_stream_twiml

    twiml = build_stream_twiml(
        stream_url="wss://staging.example/stream/token-1",
        from_number="+14155550199&bad",
        to_number="+14155550110",
    )

    assert "token-1" in twiml
    assert "config_id" not in twiml
    assert "+14155550199&amp;bad" in twiml
    assert "<Connect><Stream" in twiml


@pytest.mark.asyncio
async def test_twilio_make_call_adds_manor_status_callback(monkeypatch):
    from urllib.parse import parse_qs

    import httpx as real_httpx

    from packages.core.services.channels import twilio_adapter
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    captured: dict[str, object] = {}

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {
                "sid": "CA-outbound",
                "status": "queued",
                "from": "+14155550110",
                "to": "+14155550199",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, data, auth):
            captured["data"] = data
            captured["auth"] = auth
            request = real_httpx.Request("POST", _url, data=data)
            captured["encoded_form"] = parse_qs(request.read().decode())
            return Response()

    monkeypatch.setattr(
        twilio_adapter,
        "httpx",
        type("Httpx", (), {"AsyncClient": lambda **_kwargs: Client()}),
    )

    result = await TwilioAdapter(
        "AC-outbound",
        "auth-token",
        "+14155550110",
    ).make_call(
        "+14155550199",
        "https://manor.example/api/v1/channels/twilio/voice/outbound/token-1",
        status_callback_url="https://manor.example/api/v1/channels/twilio/status?config_id=config-voice",
    )

    assert result["external_id"] == "CA-outbound"
    assert captured["data"] == {
        "To": "+14155550199",
        "From": "+14155550110",
        "Url": "https://manor.example/api/v1/channels/twilio/voice/outbound/token-1",
        "StatusCallback": (
            "https://manor.example/api/v1/channels/twilio/status?config_id=config-voice"
        ),
        "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": [
            "initiated",
            "ringing",
            "answered",
            "completed",
        ],
    }
    assert captured["encoded_form"]["StatusCallbackEvent"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]


@pytest.mark.asyncio
async def test_twilio_make_call_commits_intent_before_provider_io(
    db_session,
    monkeypatch,
):
    from types import SimpleNamespace

    import packages.core.database as db_module
    from packages.core.ai.mcp import twilio
    from packages.core.models.channel import MessageLog
    from packages.core.services.voice.call_sessions import get_call_session_by_token

    integration_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=owner_user_id,
        channel_type="twilio_voice",
        provider="twilio",
        credential_source_kind="integration",
        credential_source_id=integration_id,
        config={"integration_id": integration_id},
        credentials={},
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=config.entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=config.entity_id,
        user_id=owner_user_id,
        workspace_id=workspace_id,
        type="twilio_voice",
        agent_subscription_id=subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([config, subscription, binding])
    await db_session.commit()
    binding_id = binding.id
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )

    class Adapter:
        from_number = "+14155550110"

        async def make_call(self, _to, twiml_url, *, status_callback_url):
            raw_token = twiml_url.rsplit("/", 1)[-1]
            async with db_module.async_session() as probe_db:
                visible_session = await get_call_session_by_token(probe_db, raw_token)
                visible_log = (await probe_db.execute(
                    select(MessageLog).where(
                        MessageLog.channel_config_id == config.id,
                        MessageLog.direction == "outbound",
                    )
                )).scalar_one_or_none()
            assert visible_session is not None
            assert visible_log is not None
            assert visible_log.status == "queued"
            assert f"session_id={visible_session.id}" in status_callback_url
            return {
                "external_id": "CA-durable-intent",
                "status": "queued",
                "from_address": self.from_number,
                "to_address": "+14155550199",
            }

    twilio.set_call_context({
        "entity_id": config.entity_id,
        "integration_account_id": integration_id,
        "user_id": owner_user_id,
        "workspace_id": workspace_id,
    })
    try:
        result = await twilio._make_manor_voice_call(
            Adapter(),
            credentials={},
            to="+14155550199",
        )
    finally:
        twilio.clear_call_context()

    assert result["external_id"] == "CA-durable-intent"
    db_session.expire_all()
    saved_session = await db_session.get(
        TwilioVoiceCallSession,
        result["session_id"],
    )
    assert saved_session is not None
    assert saved_session.call_sid == "CA-durable-intent"
    assert saved_session.status == "connecting"
    assert saved_session.workspace_id == workspace_id
    assert saved_session.agent_id == agent_id
    assert saved_session.metadata_json["channel_binding_id"] == binding_id


@pytest.mark.asyncio
async def test_twilio_provider_error_preserves_authoritative_callback_log(
    db_session,
    monkeypatch,
):
    from types import SimpleNamespace

    from sqlalchemy import update

    from packages.core.ai.mcp import twilio
    from packages.core.models.channel import MessageLog
    from packages.core.services.voice import call_sessions

    integration_id = generate_ulid()
    owner_user_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=owner_user_id,
        channel_type="twilio_voice",
        provider="twilio",
        credential_source_kind="integration",
        credential_source_id=integration_id,
        config={"integration_id": integration_id},
        credentials={},
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=config.entity_id,
        user_id=owner_user_id,
        type="twilio_voice",
        agent_id=generate_ulid(),
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([config, binding])
    await db_session.commit()
    config_id = config.id
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )

    original_finish = call_sessions.finish_call_session

    async def callback_wins_before_failure_settlement(db, session, **_kwargs):
        log_id = str((session.metadata_json or {}).get("message_log_id") or "")
        await db.execute(
            update(TwilioVoiceCallSession)
            .where(TwilioVoiceCallSession.id == session.id)
            .values(status="completed")
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(MessageLog)
            .where(MessageLog.id == log_id)
            .values(status="delivered", external_id="CA-callback-won")
            .execution_options(synchronize_session=False)
        )
        await db.refresh(session)

    monkeypatch.setattr(
        call_sessions,
        "finish_call_session",
        callback_wins_before_failure_settlement,
    )

    class Adapter:
        from_number = "+14155550110"

        async def make_call(self, _to, _twiml_url, *, status_callback_url):
            raise RuntimeError("provider connection reset")

    twilio.set_call_context({
        "entity_id": config.entity_id,
        "integration_account_id": integration_id,
        "user_id": owner_user_id,
    })
    try:
        with pytest.raises(RuntimeError, match="connection reset"):
            await twilio._make_manor_voice_call(
                Adapter(),
                credentials={},
                to="+14155550199",
            )
    finally:
        twilio.clear_call_context()
        monkeypatch.setattr(call_sessions, "finish_call_session", original_finish)

    db_session.expire_all()
    log = (await db_session.execute(
        select(MessageLog).where(
            MessageLog.channel_config_id == config_id,
            MessageLog.direction == "outbound",
        )
    )).scalar_one()
    assert log.status == "delivered"
    assert log.external_id == "CA-callback-won"


@pytest.mark.asyncio
async def test_voice_webhook_creates_tokenized_call_session(client, db_session, monkeypatch):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    owner_user_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="entity-voice-webhook",
        owner_user_id=owner_user_id,
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=config.entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=config.entity_id,
        user_id=owner_user_id,
        workspace_id=workspace_id,
        type="twilio_voice",
        agent_subscription_id=subscription.id,
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add_all([config, subscription, binding])
    await db_session.commit()
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    async def fake_get(_config_id, **_kwargs):
        return TwilioAdapter("AC-voice", "auth-token", "+14155550110"), config

    async def fake_validate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(twilio_router, "_get_adapter_and_config", fake_get)
    monkeypatch.setattr(twilio_router, "_validate_twilio_signature", fake_validate)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://manor.example")

    response = await client.post(
        f"/api/v1/channels/twilio/voice?config_id={config.id}",
        data={
            "CallSid": "CA-inbound-tokenized",
            "From": "+14155550199",
            "To": "+14155550110",
            "CallStatus": "ringing",
        },
    )

    assert response.status_code == 200
    assert "/api/v1/channels/twilio_voice/stream/" in response.text
    assert "config_id=" not in response.text
    sessions = (await db_session.execute(
        select(TwilioVoiceCallSession).where(
            TwilioVoiceCallSession.call_sid == "CA-inbound-tokenized",
        )
    )).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].status == "pending"
    assert sessions[0].owner_user_id == owner_user_id
    assert sessions[0].workspace_id == workspace_id
    assert sessions[0].agent_id == agent_id
    assert sessions[0].metadata_json["channel_binding_id"] == binding.id


@pytest.mark.asyncio
async def test_voice_webhook_rolls_back_session_when_initial_log_fails(
    client, db_session, monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="entity-voice-atomic",
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    async def fake_get(_config_id, **_kwargs):
        return TwilioAdapter("AC-voice", "auth-token", "+14155550110"), config

    async def fake_validate(*_args, **_kwargs):
        return None

    async def fail_initial_log(*_args, **_kwargs):
        raise RuntimeError("message log unavailable")

    monkeypatch.setattr(twilio_router, "_get_adapter_and_config", fake_get)
    monkeypatch.setattr(twilio_router, "_validate_twilio_signature", fake_validate)
    monkeypatch.setattr(twilio_router, "handle_inbound_message", fail_initial_log)

    response = await client.post(
        f"/api/v1/channels/twilio/voice?config_id={config.id}",
        data={
            "CallSid": "CA-inbound-atomic",
            "From": "+14155550199",
            "To": "+14155550110",
            "CallStatus": "ringing",
        },
    )

    assert response.status_code == 503
    rows = (await db_session.execute(
        select(TwilioVoiceCallSession).where(
            TwilioVoiceCallSession.call_sid == "CA-inbound-atomic",
        )
    )).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_status_callback_commits_call_session_without_message_log(
    client, db_session, monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channels.twilio_adapter import TwilioAdapter
    from packages.core.services.voice.call_sessions import create_call_session

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="entity-status-session-only",
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    session, _token = await create_call_session(
        db_session,
        entity_id=config.entity_id,
        channel_config_id=config.id,
        direction="outbound",
        call_sid="CA-status-session-only",
        from_number="+14155550110",
        to_number="+14155550199",
    )
    await db_session.commit()
    session_id = session.id
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    async def fake_get(_config_id, **_kwargs):
        return TwilioAdapter("AC-voice", "auth-token", "+14155550110"), config

    async def fake_validate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(twilio_router, "_get_adapter_and_config", fake_get)
    monkeypatch.setattr(twilio_router, "_validate_twilio_signature", fake_validate)

    response = await client.post(
        f"/api/v1/channels/twilio/status?config_id={config.id}",
        data={
            "CallSid": "CA-status-session-only",
            "CallStatus": "completed",
            "CallDuration": "42",
        },
    )

    assert response.status_code == 200
    db_session.expire_all()
    refreshed = await db_session.get(TwilioVoiceCallSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "completed"
    assert refreshed.duration_seconds == 42
    assert refreshed.ended_at is not None


@pytest.mark.asyncio
async def test_status_callback_correlates_committed_outbound_intent_before_sid_attach(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.models.channel import MessageLog
    from packages.core.services.channel_message_logs import create_channel_outbound_log
    from packages.core.services.channels.twilio_adapter import TwilioAdapter
    from packages.core.services.voice.call_sessions import create_call_session

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.flush()
    session, _token = await create_call_session(
        db_session,
        entity_id=config.entity_id,
        channel_config_id=config.id,
        direction="outbound",
        call_sid=None,
        from_number="+14155550110",
        to_number="+14155550199",
    )
    log = await create_channel_outbound_log(
        db_session,
        entity_id=config.entity_id,
        channel_config_id=config.id,
        channel_type="twilio_voice",
        to_address="+14155550199",
        content="Outbound Manor voice call",
    )
    session.metadata_json = {"message_log_id": log.id}
    await db_session.commit()
    session_id = session.id
    log_id = log.id
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    async def fake_get(_config_id, **_kwargs):
        return TwilioAdapter("AC-voice", "auth-token", "+14155550110"), config

    async def fake_validate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(twilio_router, "_get_adapter_and_config", fake_get)
    monkeypatch.setattr(twilio_router, "_validate_twilio_signature", fake_validate)

    response = await client.post(
        f"/api/v1/channels/twilio/status?config_id={config.id}&session_id={session_id}",
        data={
            "CallSid": "CA-status-before-attach",
            "CallStatus": "completed",
            "CallDuration": "9",
        },
    )

    assert response.status_code == 200
    db_session.expire_all()
    refreshed_session = await db_session.get(TwilioVoiceCallSession, session_id)
    refreshed_log = await db_session.get(MessageLog, log_id)
    assert refreshed_session is not None
    assert refreshed_session.call_sid == "CA-status-before-attach"
    assert refreshed_session.status == "completed"
    assert refreshed_log is not None
    assert refreshed_log.external_id == "CA-status-before-attach"
    assert refreshed_log.status == "delivered"


@pytest.mark.asyncio
async def test_outbound_voice_twiml_endpoint_reuses_session_stream_token(client, db_session, monkeypatch):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channels.twilio_adapter import TwilioAdapter
    from packages.core.services.voice.call_sessions import create_call_session

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    async def fake_get(_config_id, **_kwargs):
        return TwilioAdapter("AC-voice", "auth-token", "+14155550110"), None

    async def fake_validate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(twilio_router, "_get_adapter_and_config", fake_get)
    monkeypatch.setattr(twilio_router, "_validate_twilio_signature", fake_validate)
    config = ChannelConfig(
        id="config-voice-outbound",
        entity_id="entity-voice-outbound",
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    session, raw_token = await create_call_session(
        db_session,
        entity_id="entity-voice-outbound",
        channel_config_id="config-voice-outbound",
        direction="outbound",
        call_sid="CA-outbound-twiml",
        from_number="+14155550110",
        to_number="+14155550199",
    )
    await db_session.commit()

    response = await client.post(
        f"/api/v1/channels/twilio/voice/outbound/{raw_token}",
    )

    assert response.status_code == 200
    assert f"/api/v1/channels/twilio_voice/stream/{raw_token}" in response.text
    assert session.token_used_at is None


def test_twilio_make_call_schema_only_requires_phone_number():
    from packages.core.ai.mcp import twilio

    spec = next(item for item in twilio.list_tools() if item["name"] == "make_call")
    assert spec["inputSchema"]["required"] == ["to"]
    assert "twiml_url" not in spec["inputSchema"]["properties"]
