"""Actual gateway and PostgreSQL persistence; only model/provider/delivery are synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_webchat_resident_scope import authorize, resident_case as inherited_resident_case

resident_case = inherited_resident_case

PRIVATE_REPLY = "SYNTHETIC PRIVATE RESIDENT REPAIR RESULT"


@pytest.fixture
async def gateway_case(resident_case, monkeypatch):
    c = resident_case
    from packages.core.services import channel_gateway as gateway
    from packages.core.services import notification_channel_inbound
    from packages.core.services.channel_agent_runtime import ChannelAgentRunResult
    factory = async_sessionmaker(c.db.bind, expire_on_commit=False)
    monkeypatch.setattr(gateway, "async_session", factory)
    # Real channel/contact/conversation resolution; avoid needing a live Agent
    # installation or a paid model to test the gateway's publication contract.
    monkeypatch.setattr(gateway, "resolve_subscription", AsyncMock(return_value=SimpleNamespace(
        source="legacy", id=None, agent_id=None, workspace_id=c.ws.id)))
    c.conv.channel = "webchat"
    c.conv.meta = {"channel_contact_id": c.contact.id, "channel_config_id": c.cc.id,
                   "sender_id": "session", "chat_id": "session", "session_id": "session"}
    await c.db.commit()
    monkeypatch.setattr(notification_channel_inbound, "notify_channel_inbound_recipients", AsyncMock())
    monkeypatch.setattr(gateway, "runtime_persist_channel_runtime_events", AsyncMock())
    monkeypatch.setattr(gateway, "runtime_persist_channel_hold_runtime_events", AsyncMock())
    hold = AsyncMock(wraps=gateway.maybe_hold_external_reply_for_approval)
    delivery = AsyncMock(return_value=True)
    monkeypatch.setattr(gateway, "maybe_hold_external_reply_for_approval", hold)
    monkeypatch.setattr(gateway, "send_channel_text_reply", delivery)
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow_model(**kwargs):
        started.set()
        await finish.wait()
        return ChannelAgentRunResult(content=PRIVATE_REPLY)
    monkeypatch.setattr(gateway, "run_channel_agent_turn", slow_model)
    c.gateway, c.hold, c.delivery = gateway, hold, delivery
    c.started, c.finish, c.factory = started, finish, factory
    return c


async def dispatch(c, scope):
    return await c.svc.run_with_scope(scope, c.gateway.dispatch_inbound(
        entity_id=c.cc.entity_id, channel_config_id=c.cc.id, channel_type="webchat",
        sender_id="session", sender_name="Synthetic resident", chat_id="session", content="Check my repair",
    ))


async def private_messages(c):
    from packages.core.models.task import Message
    async with c.factory() as db:
        return list((await db.scalars(select(Message).where(
            Message.conversation_id == c.conv.id, Message.content == PRIVATE_REPLY,
        ))).all())


async def private_outbound_logs(c):
    from packages.core.models.channel import MessageLog
    async with c.factory() as db:
        return list((await db.scalars(select(MessageLog).where(
            MessageLog.conversation_id == c.conv.id, MessageLog.content == PRIVATE_REPLY,
            MessageLog.direction == "outbound",
        ))).all())


def scope_for(c, row):
    return c.svc.ResidentRuntimeScope(row.id, row.generation, row.entity_id,
        row.conversation_id, row.channel_contact_id, row.session_id)


async def invalidate(c, row, kind):
    if kind == "expired":
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await c.db.commit()
    else:
        previous = c.provider.side_effect
        async def revoked(issuer, path, **kwargs):
            if path == "identity":
                raise c.svc.authentication_required()
            return await previous(issuer, path, **kwargs)
        c.provider.side_effect = revoked


@pytest.mark.parametrize("kind", ["expired", "provider_revoked"])
async def test_gateway_slow_model_cannot_persist_hold_or_deliver_after_resident_expires(gateway_case, kind):
    c = gateway_case
    row, _, _ = await authorize(c)
    task = asyncio.create_task(dispatch(c, scope_for(c, row)))
    try:
        await asyncio.wait_for(c.started.wait(), timeout=5)
        await invalidate(c, row, kind)
    finally:
        c.finish.set()
    result = await asyncio.wait_for(task, timeout=10)
    assert result["status"] == "error"
    assert not await private_messages(c)
    assert not await private_outbound_logs(c)
    c.hold.assert_not_awaited()
    c.delivery.assert_not_awaited()


@pytest.mark.parametrize("kind", ["expired", "provider_revoked"])
async def test_direct_channel_assistant_persistence_requires_current_resident_authority(gateway_case, kind):
    from packages.core.services.channel_conversations import add_channel_assistant_message
    c = gateway_case
    row, _, _ = await authorize(c)
    scope = scope_for(c, row)
    await invalidate(c, row, kind)
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.run_with_scope(scope, add_channel_assistant_message(c.db,
            conversation_id=c.conv.id, channel_type="webchat", chat_id="session", content=PRIVATE_REPLY))
    assert not await private_messages(c)


@pytest.mark.parametrize("resident", [True, False])
async def test_valid_resident_and_nonresident_gateway_keep_original_behavior(gateway_case, resident):
    c = gateway_case
    scope = None
    if resident:
        row, _, _ = await authorize(c)
        scope = scope_for(c, row)
    c.finish.set()
    result = await dispatch(c, scope)
    assert result["status"] == "ok", result
    assert len(await private_messages(c)) == 1
    assert len(await private_outbound_logs(c)) == 1
    c.hold.assert_awaited_once()
    c.delivery.assert_awaited_once()


async def test_private_persistence_rechecks_after_pre_hold_check(gateway_case):
    c = gateway_case
    row, _, _ = await authorize(c)
    async def invalidate_at_hold(**kwargs):
        await invalidate(c, row, "provider_revoked")
        return None
    c.hold.side_effect = invalidate_at_hold
    c.finish.set()
    result = await dispatch(c, scope_for(c, row))
    assert result["status"] == "error"
    assert not await private_messages(c)
    assert not await private_outbound_logs(c)
    c.delivery.assert_not_awaited()


async def test_resident_reply_delivery_rechecks_after_persistence(gateway_case, monkeypatch):
    c = gateway_case
    row, _, _ = await authorize(c)
    original = c.gateway.channel_reply_route_is_active
    async def invalidate_before_send(**kwargs):
        result = await original(**kwargs)
        await invalidate(c, row, "provider_revoked")
        return result
    monkeypatch.setattr(c.gateway, "channel_reply_route_is_active", invalidate_before_send)
    c.finish.set()
    result = await dispatch(c, scope_for(c, row))
    assert result["status"] == "error"
    assert len(await private_messages(c)) == 1  # Committed while authority was valid.
    c.delivery.assert_not_awaited()
