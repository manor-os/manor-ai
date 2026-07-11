"""End-to-end: external channel inbound triggers a "new message" ping
to staff via their bound channel, with per-conversation throttling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.document import Channel
from packages.core.models.notification import Notification
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
    """Avoid hitting the real LLM. The inbound notify path runs *before*
    the agent in dispatch_inbound, but the agent still gets called when
    the matcher / inbound notification flow doesn't short-circuit."""
    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return channel_gateway.ChannelAgentRunResult(content="Thanks, looking into it.")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)


# ── Helpers ────────────────────────────────────────────────────────────────


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


async def _seed_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    inbound_recipients: list[str] | None = None,
) -> Workspace:
    settings: dict[str, Any] = {}
    if inbound_recipients is not None:
        settings = {
            "notification_policy": {
                "inbound_notify_user_ids": inbound_recipients,
            },
        }
    ws = Workspace(
        entity_id=entity_id,
        name="WS",
        status="active",
        settings=settings,
    )
    db.add(ws)
    await db.commit()
    return ws


async def _seed_cc(db: AsyncSession, *, entity_id: str, workspace_id: str) -> ChannelConfig:
    cc = ChannelConfig(
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_type="telegram",
        provider="telegram_bot",
        config={},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.commit()
    return cc


async def _seed_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str,
    cc: ChannelConfig,
) -> Channel:
    binding = Channel(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        type="telegram",
        name="Binding",
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


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_inbound_notifies_workspace_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Customer writes in via Telegram → workspace owner (entity owner
    fallback) gets a "new message" notification on their bound channel."""
    owner = await _register(client, "inbound_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(db_session, entity_id=owner["entity_id"], workspace_id=ws.id)
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )

    # Owner has their own Telegram contact + opted into telegram pushes
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_1",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_1",
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
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Curious Customer",
        chat_id=customer_contact.source_id,
        content="Hi, do you have availability next weekend?",
    )

    # In-app notification row exists for the owner
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "Curious Customer" in (rows[0].title or "")
    assert "next weekend" in (rows[0].content or "")
    meta = rows[0].meta or {}
    assert meta.get("channel_type") == "telegram"
    assert meta.get("channel_contact_id") == customer_contact.id

    # And a Telegram push landed on the owner's chat, not the customer's
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    customer_pushes = [s for s in fake_telegram.sent if s["to"] == customer_contact.source_id]
    assert len(owner_pushes) == 1
    assert "Curious Customer" in owner_pushes[0]["text"]
    # The customer also gets the agent's stubbed reply
    assert len(customer_pushes) == 1
    assert "looking into it" in customer_pushes[0]["text"]


@pytest.mark.asyncio
async def test_followup_inbound_within_window_is_throttled(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """A second inbound on the same conversation within the throttle
    window should NOT generate a second notification — only the first
    customer turn pings the operator."""
    owner = await _register(client, "throttle_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(db_session, entity_id=owner["entity_id"], workspace_id=ws.id)
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_2",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_2",
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
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="first ping",
    )
    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="second ping",
    )

    # Exactly one notification (throttled second time)
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1

    # Owner's Telegram got exactly one heads-up push (the customer still
    # gets their two agent replies — those are unrelated)
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert len(owner_pushes) == 1


@pytest.mark.asyncio
async def test_internal_member_inbound_does_not_notify(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """When a workspace member (role != "external") writes in via their
    own bound channel — e.g. testing the bot — no "new customer message"
    notification should fire. They're not a customer."""
    owner = await _register(client, "internal_inbound_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(db_session, entity_id=owner["entity_id"], workspace_id=ws.id)
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    # Owner has their own contact + role=admin (NOT external)
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_3",
        user_id=owner["user_id"],
        role="admin",
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=owner_contact.source_id,
        sender_name="Owner Self-Test",
        chat_id=owner_contact.source_id,
        content="hello bot",
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_workspace_inbound_recipients_override(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Explicit empty inbound_notify_user_ids suppresses all pings; the
    in-app row + bot reply still go through."""
    owner = await _register(client, "inbound_suppress_owner")
    ws = await _seed_workspace(
        db_session,
        entity_id=owner["entity_id"],
        inbound_recipients=[],
    )
    cc = await _seed_cc(db_session, entity_id=owner["entity_id"], workspace_id=ws.id)
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_4",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_4",
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
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="hello",
    )

    # No inbound-message notification row created for owner
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    # No owner-targeted telegram push (only the customer's agent reply went out)
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert owner_pushes == []
