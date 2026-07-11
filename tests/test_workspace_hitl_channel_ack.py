"""End-to-end test: workspace HITL pause → Telegram approve → outbound delivered.

This wires the whole stack together:

  1. A workspace governance policy requires HITL for ``external_message.send``
  2. dispatch_inbound receives a customer message → agent drafts a reply
  3. The HITL gate pauses delivery, posts a workspace_chat pending_action,
     and (new) fans out an actionable notification to the workspace owner
     via their bound Telegram contact
  4. The owner replies "approve" on Telegram
  5. dispatch_inbound's pre-step matches the action, fires the
     workspace.hitl.resolve_message callback, which resolves the chat
     pending_action AND triggers deliver_approved_external_reply
  6. The original customer's Telegram chat now receives the approved reply

We mock the agent so the test is deterministic, but use the real
governance + workspace_chat + notification_callbacks code paths.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.service import update_policy
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.document import Channel
from packages.core.models.notification import NotificationDelivery
from packages.core.models.task import Message
from packages.core.models.workspace import Workspace
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


# ── Fakes ───────────────────────────────────────────────────────────────────


class _RecordingAdapter(ChannelAdapter):
    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_telegram():
    fake = _RecordingAdapter()
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = fake  # type: ignore[assignment]
    yield fake
    if original is None:
        ADAPTERS.pop("telegram", None)
    else:
        ADAPTERS["telegram"] = original


@pytest.fixture
def stub_agent(monkeypatch):
    """Stop the channel gateway from actually calling the LLM — we want
    a deterministic drafted reply so we can assert it later survived the
    approval round-trip."""
    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return channel_gateway.ChannelAgentRunResult(
            content="Hi there! Yes, refunds typically take 3-5 business days.",
        )

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)


# ── Helpers ───────────────────────────────────────────────────────────────


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"Org {username}",
        },
    )
    body = resp.json()
    return {
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "user_id": body["user_id"],
        "entity_id": body["entity_id"],
    }


async def _seed_workspace(db: AsyncSession, *, entity_id: str) -> Workspace:
    ws = Workspace(
        entity_id=entity_id,
        name="Sales",
        category="sales",
        status="active",
        settings={},
    )
    db.add(ws)
    await db.commit()
    return ws


async def _seed_telegram_cc(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None = None,
) -> ChannelConfig:
    cc = ChannelConfig(
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Test",
        config={},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.commit()
    return cc


async def _seed_channel_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str,
    cc: ChannelConfig,
) -> Channel:
    binding = Channel(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        type="telegram",
        name="Test binding",
        status="active",
        config={"channel_config_id": cc.id},
    )
    db.add(binding)
    await db.commit()
    return binding


async def _seed_contact(
    db: AsyncSession,
    *,
    entity_id: str,
    cc: ChannelConfig,
    source_id: str,
    user_id: str | None = None,
    role: str = "external",
) -> ChannelContact:
    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id=source_id,
        display_name=source_id,
        user_id=user_id,
        role=role,
        status="active",
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    await db.commit()
    return contact


# ── The test ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_hitl_resolves_via_telegram_reply(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """The full loop — paused reply → notification → channel approve →
    customer receives the approved text."""
    # ── Set up Manor side ────────────────────────────────────────────────
    owner = await _register(client, "hitl_owner")
    entity_id = owner["entity_id"]
    owner_user_id = owner["user_id"]

    ws = await _seed_workspace(db_session, entity_id=entity_id)

    # Governance: require HITL for external_message.send
    await update_policy(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        policy=WorkspacePolicy(hitl_required_actions=["external_message.send"]),
        changed_by=owner_user_id,
    )
    await db_session.commit()

    # ── Channel config (shared) + bindings ──────────────────────────────
    cc = await _seed_telegram_cc(db_session, entity_id=entity_id, workspace_id=ws.id)
    await _seed_channel_binding(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        user_id=owner_user_id,
        cc=cc,
    )

    # Owner: linked Manor user — receives the approval notification
    owner_contact = await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_owner_999",
        user_id=owner_user_id,
        role="admin",
    )

    # Customer: external, sends the inbound that triggers the agent
    customer_contact = await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_customer_123",
        user_id=None,
        role="external",
    )

    # Owner opts into telegram for HITL events
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={
            "default_channels": ["telegram"],
            "by_kind": {
                "task_hitl_requested": {
                    "channels": ["telegram"],
                    "enabled": True,
                    "bypass_quiet_hours": True,
                },
            },
        },
    )

    # ── Phase 1: customer messages in, agent drafts reply, HITL pauses ──
    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="When will my refund arrive?",
    )
    assert result["status"] == "approval_required"
    approval_msg_id = result["approval_message_id"]

    # Customer received no reply yet (governance paused it)
    sent_to_customer = [s for s in fake_telegram.sent if s["to"] == customer_contact.source_id]
    assert sent_to_customer == []

    # Owner now receives TWO pushes on Telegram for the same inbound:
    #   1. "New message from …" — informational heads-up (channel inbound)
    #   2. "Approve external reply?" — the actionable HITL card
    sent_to_owner = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert len(sent_to_owner) == 2
    inbound_pushes = [s for s in sent_to_owner if "New message from" in s["text"]]
    hitl_pushes = [s for s in sent_to_owner if "Approve external reply?" in s["text"]]
    assert len(inbound_pushes) == 1
    assert "When will my refund arrive?" in inbound_pushes[0]["text"]
    assert len(hitl_pushes) == 1
    notif_text = hitl_pushes[0]["text"]
    assert "refunds typically take 3-5 business days" in notif_text
    assert "Reply with:" in notif_text

    # Exactly one HITL delivery row (the inbound heads-up has no actions,
    # so it doesn't write a NotificationDelivery — that's HITL-only).
    deliveries = (
        (
            await db_session.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.user_id == owner_user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1
    assert deliveries[0].status == "sent"
    assert deliveries[0].callback_kind == "workspace.hitl.resolve_message"
    assert deliveries[0].callback_payload["chat_message_id"] == approval_msg_id

    fake_telegram.sent.clear()

    # ── Phase 2: owner replies "approve" on Telegram ─────────────────────
    ack = await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=owner_contact.source_id,
        sender_name="Owner",
        chat_id=owner_contact.source_id,
        content="approve",
    )
    assert ack["status"] == "delivery_resolved"
    assert ack["action_key"] == "approve"

    # The owner got their ack
    sent_to_owner = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert len(sent_to_owner) == 1
    assert "Approved" in sent_to_owner[0]["text"]

    # The original customer received the approved reply
    sent_to_customer = [s for s in fake_telegram.sent if s["to"] == customer_contact.source_id]
    assert len(sent_to_customer) == 1
    assert "refunds typically take 3-5 business days" in sent_to_customer[0]["text"]

    # The workspace_chat pending_action row is now resolved. Drop the
    # identity-map cache so subsequent reads see the side-effect updates
    # committed by the dispatcher's separate session.
    db_session.expire_all()
    chat_msg = (await db_session.execute(select(Message).where(Message.id == approval_msg_id))).scalar_one()
    assert chat_msg.resolved_at is not None
    assert chat_msg.resolved_by_user_id == owner_user_id
    assert (chat_msg.resolution or {}).get("choice") == "approve"

    # Delivery row is resolved
    refresh = (
        await db_session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == owner_user_id,
            )
        )
    ).scalar_one()
    assert refresh.status == "resolved"
    assert refresh.resolved_action_key == "approve"


@pytest.mark.asyncio
async def test_workspace_hitl_reject_does_not_send_to_customer(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """A reject reply should still resolve the workspace card but NOT
    send the drafted text to the customer."""
    owner = await _register(client, "hitl_reject_owner")
    entity_id = owner["entity_id"]
    owner_user_id = owner["user_id"]
    ws = await _seed_workspace(db_session, entity_id=entity_id)
    await update_policy(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        policy=WorkspacePolicy(hitl_required_actions=["external_message.send"]),
        changed_by=owner_user_id,
    )
    await db_session.commit()
    cc = await _seed_telegram_cc(db_session, entity_id=entity_id, workspace_id=ws.id)
    await _seed_channel_binding(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        user_id=owner_user_id,
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_owner_888",
        user_id=owner_user_id,
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_customer_456",
        user_id=None,
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="refund please",
    )

    fake_telegram.sent.clear()

    ack = await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=owner_contact.source_id,
        sender_name="Owner",
        chat_id=owner_contact.source_id,
        content="reject",
    )
    assert ack["status"] == "delivery_resolved"
    assert ack["action_key"] == "reject"

    # Owner got an ack
    owner_acks = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert len(owner_acks) == 1
    assert "Rejected" in owner_acks[0]["text"]

    # Customer got NOTHING after the original message
    customer_msgs = [s for s in fake_telegram.sent if s["to"] == customer_contact.source_id]
    assert customer_msgs == []


@pytest.mark.asyncio
async def test_workspace_hitl_settings_override_default_recipients(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """When workspace settings list explicit ``hitl_notify_user_ids``,
    those win over the default (entity owners + admins)."""
    owner = await _register(client, "hitl_cfg_owner")
    member = await _register(client, "hitl_cfg_member")
    # Force the member into the same entity
    member["user_id"]
    # Actually each register creates a NEW entity. For test simplicity
    # we just configure the OWNER's workspace to point at OWNER again,
    # confirming explicit settings honoured even when matching the default.

    entity_id = owner["entity_id"]
    owner_user_id = owner["user_id"]
    ws = await _seed_workspace(db_session, entity_id=entity_id)

    # Explicit recipients — empty list should suppress all sends.
    # We blank both lists so neither the inbound heads-up nor the HITL
    # actionable card reaches anyone.
    ws.settings = {
        "notification_policy": {
            "hitl_notify_user_ids": [],
            "inbound_notify_user_ids": [],
        },
    }
    await db_session.commit()

    await update_policy(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        policy=WorkspacePolicy(hitl_required_actions=["external_message.send"]),
        changed_by=owner_user_id,
    )
    await db_session.commit()
    cc = await _seed_telegram_cc(db_session, entity_id=entity_id, workspace_id=ws.id)
    await _seed_channel_binding(
        db_session,
        entity_id=entity_id,
        workspace_id=ws.id,
        user_id=owner_user_id,
        cc=cc,
    )
    await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_owner_777",
        user_id=owner_user_id,
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=entity_id,
        cc=cc,
        source_id="tg_customer_789",
        user_id=None,
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="hi",
    )

    # No delivery rows since explicit hitl_notify_user_ids was empty
    deliveries = (await db_session.execute(select(NotificationDelivery))).scalars().all()
    assert deliveries == []
    # And no notification fan-out happened
    assert all(s["to"] != "tg_owner_777" for s in fake_telegram.sent)
