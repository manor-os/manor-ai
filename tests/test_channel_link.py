"""End-to-end channel claim flow for Telegram and Discord.

A user mints a link token in Settings, sends /start <token> through the
connected channel, and the ChannelContact picks up user_id + role.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import (
    ChannelConfig,
    ChannelContact,
    ChannelLinkToken,
)
from packages.core.models.document import Channel
from packages.core.models.user import User
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter
from tests.test_document_permissions import _auth, _create_entity_user


class _RecordingAdapter(ChannelAdapter):
    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


class _DiscordRecordingAdapter(_RecordingAdapter):
    channel_type = "discord"


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
    """Keep the LLM out of the test loop — claim path short-circuits the
    agent run, but we still want a clean stub in case a non-claim path
    fires (e.g. the unmatched-content tests).
    """
    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return None

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)


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


async def _seed_telegram_cc(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str | None = None,
    bot_username: str = "ManorTestBot",
) -> ChannelConfig:
    cc = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        channel_type="telegram",
        provider="telegram_bot",
        config={"bot_username": bot_username},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.flush()

    # Also need a Channel binding so dispatch_inbound has somewhere to route.
    from packages.core.models.document import Channel

    db.add(
        Channel(
            entity_id=entity_id,
            type="telegram",
            name="Test bot binding",
            status="active",
            config={"channel_config_id": cc.id},
        )
    )
    await db.commit()
    return cc


async def _seed_whatsapp_route(
    client: AsyncClient,
    db: AsyncSession,
    *,
    username: str,
    contact_override: bool = False,
    legacy_binding: bool = False,
) -> SimpleNamespace:
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    owner = await _register(client, username)
    workspace = Workspace(
        entity_id=owner["entity_id"],
        name=f"{username} Workspace",
        status="active",
    )
    agent = Agent(
        entity_id=owner["entity_id"],
        owner_user_id=owner["user_id"],
        name=f"{username} Agent",
        status="active",
    )
    channel_config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["user_id"],
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        whatsapp_phone_number_id=owner["user_id"],
        config={},
        credentials={},
        status="active",
    )
    db.add_all([workspace, agent, channel_config])
    await db.flush()

    subscription = None
    if not legacy_binding:
        subscription = AgentSubscription(
            entity_id=owner["entity_id"],
            agent_id=agent.id,
            workspace_id=workspace.id,
            status="active",
        )
        db.add(subscription)

    contact = None
    override_subscription = None
    if contact_override:
        override_workspace = Workspace(
            entity_id=owner["entity_id"],
            name=f"{username} Override Workspace",
            status="active",
        )
        override_agent = Agent(
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            name=f"{username} Override Agent",
            status="active",
        )
        db.add_all([override_workspace, override_agent])
        await db.flush()
        override_subscription = AgentSubscription(
            entity_id=owner["entity_id"],
            agent_id=override_agent.id,
            workspace_id=override_workspace.id,
            status="active",
        )
        db.add(override_subscription)

    await db.flush()
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["user_id"],
        workspace_id=workspace.id,
        type="whatsapp",
        name=f"{username} route",
        config={"channel_config_id": channel_config.id},
        agent_id=agent.id,
        agent_subscription_id=subscription.id if subscription else None,
        status="active",
    )
    db.add(binding)
    if override_subscription:
        contact = ChannelContact(
            entity_id=owner["entity_id"],
            channel_config_id=channel_config.id,
            channel_type="whatsapp",
            source_id="15550002222",
            agent_subscription_id=override_subscription.id,
            status="active",
        )
        db.add(contact)
    await db.commit()
    return SimpleNamespace(
        owner=owner,
        workspace=workspace,
        agent=agent,
        subscription=subscription,
        channel_config=channel_config,
        binding=binding,
        contact=contact,
    )


# ── Start endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_link_returns_token_and_deep_link(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _register(client, "link_starter")
    await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )

    resp = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "telegram"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_type"] == "telegram"
    assert len(body["token"]) >= 8
    assert body["deep_link"]
    assert "ManorTestBot" in body["deep_link"]
    assert body["token"] in body["deep_link"]
    assert body["bot_username"] == "ManorTestBot"

    # A row in the table with status pending
    row = (
        await db_session.execute(select(ChannelLinkToken).where(ChannelLinkToken.token == body["token"]))
    ).scalar_one()
    assert row.user_id == user["user_id"]
    assert row.entity_id == user["entity_id"]
    assert row.claimed_at is None


@pytest.mark.asyncio
async def test_start_link_rejects_unsupported_channel_type(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _register(client, "link_starter_unsupported")
    resp = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "smoke_signal"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_link_fails_without_channel_config(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """If the entity hasn't connected a Telegram bot yet, the start
    endpoint should refuse rather than mint a useless token."""
    user = await _register(client, "link_no_cc")
    resp = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "telegram"},
    )
    assert resp.status_code == 400
    assert "no active" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_discord_link_claims_contact_for_master_agent(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """A Discord installation must explicitly bind the sender before it
    receives the Manor user's internal runtime permissions."""
    user = await _register(client, "discord_link_user")
    cc = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["user_id"],
        channel_type="discord",
        provider="discord_app",
        credentials={"bot_token": "test-token"},
        config={"discord_application_id": "app", "discord_guild_id": "guild"},
        status="active",
    )
    db_session.add(cc)
    await db_session.flush()
    db_session.add(Channel(
        entity_id=user["entity_id"],
        user_id=user["user_id"],
        type="discord",
        name="Discord Master",
        config={"channel_config_id": cc.id},
        agent_id="manor-master",
        status="active",
    ))
    await db_session.commit()

    start = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "discord"},
    )
    assert start.status_code == 200, start.text
    token = start.json()["token"]
    assert "Discord" in start.json()["instructions"]

    from packages.core.services import channel_gateway

    fake = _DiscordRecordingAdapter()
    original = channel_gateway.ADAPTERS["discord"]
    channel_gateway.ADAPTERS["discord"] = fake
    try:
        result = await channel_gateway.dispatch_inbound(
            entity_id=user["entity_id"],
            channel_config_id=cc.id,
            channel_type="discord",
            sender_id="discord-user",
            sender_name="Discord User",
            chat_id="discord-channel",
            content=f"/start {token}",
        )
    finally:
        channel_gateway.ADAPTERS["discord"] = original

    assert result["status"] == "channel_link_claimed"
    contact = (await db_session.execute(
        select(ChannelContact).where(
            ChannelContact.channel_config_id == cc.id,
            ChannelContact.source_id == "discord-user",
        )
    )).scalar_one()
    assert contact.user_id == user["user_id"]
    user_row = await db_session.get(User, user["user_id"])
    assert user_row is not None
    assert contact.role == user_row.role
    assert any("Linked" in item["text"] for item in fake.sent)

    from packages.core.ai.runtime import (
        ChatSurface,
        runtime_request_for_channel_turn,
    )

    runtime_request = runtime_request_for_channel_turn(
        entity_id=user["entity_id"],
        user_id=contact.user_id,
        agent_id="manor-master",
        sender_context={
            "channel_type": "discord",
            "source_id": contact.source_id,
            "user_id": contact.user_id,
            "role": contact.role,
            "is_verified": True,
        },
    )
    assert runtime_request.surface == ChatSurface.AGENT_DM


@pytest.mark.asyncio
async def test_channel_claim_uses_target_entity_membership_role(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _register(client, "discord_link_role_user")
    cc = await _seed_telegram_cc(
        db_session,
        entity_id=user["entity_id"],
        owner_user_id=user["user_id"],
    )

    from packages.core.models.user import UserMembership

    membership = (await db_session.execute(
        select(UserMembership).where(
            UserMembership.user_id == user["user_id"],
            UserMembership.entity_id == user["entity_id"],
        )
    )).scalar_one()
    membership.role = "admin"
    user_row = await db_session.get(User, user["user_id"])
    assert user_row is not None
    user_row.role = "member"
    await db_session.commit()
    from packages.core.services.notification_channel_linking import (
        claim_token,
        start_link,
    )
    start = await start_link(
        db_session,
        user_id=user["user_id"],
        entity_id=user["entity_id"],
        channel_type="telegram",
    )
    contact = ChannelContact(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id="tg_role_user",
    )
    db_session.add(contact)
    await db_session.commit()

    result = await claim_token(
        db_session,
        token=start.token,
        contact=contact,
    )

    assert result.ok is True
    assert contact.role == "admin"


@pytest.mark.asyncio
async def test_start_link_does_not_expose_another_members_telegram_bot(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.user import UserMembership

    owner_headers = await _auth(client, "link_private_bot_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert owner_response.status_code == 200, owner_response.text
    owner = owner_response.json()
    member = await _create_entity_user(
        owner["entity_id"], "link_private_bot_member", role="member",
    )
    db_session.add(UserMembership(
        user_id=member["id"],
        entity_id=owner["entity_id"],
        role="member",
        status="active",
    ))
    await _seed_telegram_cc(
        db_session,
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        bot_username="OwnerOnlyBot",
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=member["headers"],
        json={"channel_type": "telegram"},
    )

    assert response.status_code == 400
    assert "no active" in response.json()["detail"]


# ── Claim via dispatch_inbound ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_token_claim_sets_contact_user_id(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Round trip: user mints token, the bot's webhook delivers /start
    <token>, contact.user_id is set, agent doesn't run."""
    user = await _register(client, "link_full_user")
    cc = await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )

    # Mint via API
    start = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "telegram"},
    )
    token = start.json()["token"]

    # Simulate the inbound from Telegram — a brand-new external contact
    # arrives with content "/start <token>".
    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_user_xyz",
        sender_name="Alice via Telegram",
        chat_id="tg_user_xyz",
        content=f"/start {token}",
    )
    assert result["status"] == "channel_link_claimed"
    assert result["user_id"] == user["user_id"]

    # Contact now has user_id set + role bumped from "external"
    contact = (
        await db_session.execute(
            select(ChannelContact).where(
                ChannelContact.channel_config_id == cc.id,
                ChannelContact.source_id == "tg_user_xyz",
            )
        )
    ).scalar_one()
    assert contact.user_id == user["user_id"]
    assert contact.role != "external"

    # Token row marked claimed
    token_row = (await db_session.execute(select(ChannelLinkToken).where(ChannelLinkToken.token == token))).scalar_one()
    assert token_row.claimed_at is not None
    assert token_row.claimed_contact_id == contact.id

    # Confirmation ack sent back to the user
    assert any("Linked" in s["text"] for s in fake_telegram.sent)


@pytest.mark.asyncio
async def test_already_claimed_token_refuses_second_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Single-use: a second /start with the same token from a different
    contact must NOT claim that contact."""
    user = await _register(client, "single_use_user")
    cc = await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )
    start = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "telegram"},
    )
    token = start.json()["token"]

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_first",
        sender_name="First",
        chat_id="tg_first",
        content=f"/start {token}",
    )
    # Second user tries to claim the same token
    result = await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_second",
        sender_name="Second",
        chat_id="tg_second",
        content=f"/start {token}",
    )
    assert result["status"] == "channel_link_failed"
    assert result["reason"] == "token_already_used"

    # tg_second's contact is still unlinked
    second = (
        await db_session.execute(
            select(ChannelContact).where(
                ChannelContact.channel_config_id == cc.id,
                ChannelContact.source_id == "tg_second",
            )
        )
    ).scalar_one()
    assert second.user_id is None


@pytest.mark.asyncio
async def test_expired_token_refuses_claim(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    user = await _register(client, "expired_user")
    cc = await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )

    # Insert an already-expired token manually
    expired = ChannelLinkToken(
        token="EXPIREDXYZ23",  # all in the no-confusables alphabet
        user_id=user["user_id"],
        entity_id=user["entity_id"],
        channel_type="telegram",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(expired)
    await db_session.commit()

    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_late",
        sender_name="Late",
        chat_id="tg_late",
        content=f"/start {expired.token}",
    )
    assert result["status"] == "channel_link_failed"
    assert result["reason"] == "token_expired"

    # Refresh-style ack sent
    assert any("expired" in s["text"].lower() for s in fake_telegram.sent)


@pytest.mark.asyncio
async def test_non_start_content_falls_through_to_agent(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Plain inbound text without /start should NOT touch the link
    machinery — it must continue down the normal agent path."""
    user = await _register(client, "non_start_user")
    cc = await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_chatty",
        sender_name="Chatty",
        chat_id="tg_chatty",
        content="hello bot",
    )
    # Not a claim
    assert result.get("status") not in {"channel_link_claimed", "channel_link_failed"}


@pytest.mark.asyncio
async def test_whatsapp_contact_cannot_claim_manor_user_identity(monkeypatch):
    from packages.core.services.channel_inbound_actions import (
        maybe_claim_channel_link_token,
    )

    def unexpected_session():
        raise AssertionError("WhatsApp channel-link claim reached the database")

    monkeypatch.setattr(
        "packages.core.services.channel_inbound_actions.async_session",
        unexpected_session,
    )

    outcome = await maybe_claim_channel_link_token(
        channel_contact_id="contact-1",
        channel_type="whatsapp",
        content="/start historical-whatsapp-token",
    )

    assert outcome is None


@pytest.mark.asyncio
async def test_whatsapp_ambiguous_bindings_do_not_run_or_reply(
    db_session: AsyncSession,
    monkeypatch,
):
    from packages.core.services import channel_gateway

    channel_config = ChannelConfig(
        entity_id="whatsapp-ambiguous-entity",
        owner_user_id="whatsapp-ambiguous-owner",
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        whatsapp_phone_number_id="whatsapp-ambiguous-phone",
        config={},
        credentials={},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.commit()

    async def ambiguous_scope(*_args, **_kwargs):
        raise ValueError("Channel has ambiguous active Agent bindings")

    agent_runs: list[dict] = []
    provider_sends: list[dict] = []

    async def run_agent(**kwargs):
        agent_runs.append(kwargs)
        return None

    async def send_reply(**kwargs):
        provider_sends.append(kwargs)
        return True

    monkeypatch.setattr(
        channel_gateway,
        "resolve_unique_channel_binding_scope",
        ambiguous_scope,
        raising=False,
    )
    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", run_agent)
    monkeypatch.setattr(channel_gateway, "send_channel_text_reply", send_reply)

    result = await channel_gateway.dispatch_inbound(
        entity_id=channel_config.entity_id,
        channel_config_id=channel_config.id,
        channel_type="whatsapp",
        sender_id="15550001111",
        sender_name="Customer",
        chat_id="15550001111",
        content="hello",
    )

    assert result["status"] == "unbound"
    assert result["reason"] == "ambiguous_bindings"
    assert agent_runs == []
    assert provider_sends == []


@pytest.mark.asyncio
async def test_whatsapp_contact_subscription_cannot_override_agent_binding(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from packages.core.services import channel_gateway

    route = await _seed_whatsapp_route(
        client,
        db_session,
        username="whatsapp_contact_override_owner",
        contact_override=True,
    )
    assert route.contact is not None
    assert route.subscription is not None

    agent_runs: list[dict] = []

    async def run_agent(**kwargs):
        agent_runs.append(kwargs)
        return channel_gateway.ChannelAgentRunResult(content="bound reply")

    async def route_is_active(**_kwargs):
        return True

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", run_agent)
    monkeypatch.setattr(
        channel_gateway,
        "channel_reply_route_is_active",
        route_is_active,
    )

    result = await channel_gateway.dispatch_inbound(
        entity_id=route.owner["entity_id"],
        channel_config_id=route.channel_config.id,
        channel_type="whatsapp",
        sender_id=route.contact.source_id,
        sender_name="Customer",
        chat_id=route.contact.source_id,
        content="route me to the bound agent",
        deliver_reply=False,
    )

    assert result["status"] == "ok", result
    assert result["agent_id"] == route.agent.id
    assert result["workspace_id"] == route.workspace.id
    assert result["agent_subscription_id"] == route.subscription.id
    assert agent_runs[0]["subscription"].id == route.subscription.id


@pytest.mark.asyncio
async def test_whatsapp_binding_owner_mismatch_does_not_run_agent(
    db_session: AsyncSession,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.services import channel_gateway

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    channel_config = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        whatsapp_phone_number_id="whatsapp-owner-mismatch-phone",
        config={},
        credentials={},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.flush()
    binding = Channel(
        entity_id=channel_config.entity_id,
        user_id=generate_ulid(),
        type="whatsapp",
        name="Wrong-owner WhatsApp route",
        config={"channel_config_id": channel_config.id},
        agent_id=generate_ulid(),
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()

    async def mismatched_scope(*_args, **_kwargs):
        return SimpleNamespace(
            binding=binding,
            agent_id=binding.agent_id,
            workspace_id=generate_ulid(),
            agent_subscription_id=generate_ulid(),
        )

    agent_runs: list[dict] = []

    async def run_agent(**kwargs):
        agent_runs.append(kwargs)
        return None

    monkeypatch.setattr(
        channel_gateway,
        "resolve_unique_channel_binding_scope",
        mismatched_scope,
    )
    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", run_agent)

    result = await channel_gateway.dispatch_inbound(
        entity_id=channel_config.entity_id,
        channel_config_id=channel_config.id,
        channel_type="whatsapp",
        sender_id="15550003333",
        sender_name="Customer",
        chat_id="15550003333",
        content="do not route across owners",
    )

    assert result["status"] == "unbound"
    assert result["reason"] == "binding_owner_mismatch"
    assert agent_runs == []


@pytest.mark.asyncio
async def test_whatsapp_legacy_agent_only_binding_does_not_run(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from packages.core.services import channel_gateway

    route = await _seed_whatsapp_route(
        client,
        db_session,
        username="whatsapp_legacy_binding_owner",
        legacy_binding=True,
    )

    agent_runs: list[dict] = []

    async def run_agent(**kwargs):
        agent_runs.append(kwargs)
        return None

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", run_agent)

    result = await channel_gateway.dispatch_inbound(
        entity_id=route.owner["entity_id"],
        channel_config_id=route.channel_config.id,
        channel_type="whatsapp",
        sender_id="15550004444",
        sender_name="Customer",
        chat_id="15550004444",
        content="legacy binding must not run",
    )

    assert result["status"] == "unbound"
    assert result["reason"] == "agent_not_bound"
    assert agent_runs == []


# ── Status polling endpoint ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_endpoint_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    user = await _register(client, "status_user")
    cc = await _seed_telegram_cc(
        db_session, entity_id=user["entity_id"], owner_user_id=user["user_id"],
    )
    start = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=user["headers"],
        json={"channel_type": "telegram"},
    )
    token = start.json()["token"]

    # Pending before any inbound
    pending = await client.get(
        f"/api/v1/notifications/preferences/link/{token}",
        headers=user["headers"],
    )
    assert pending.json()["status"] == "pending"

    # Claim via webhook simulation
    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=user["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id="tg_status",
        sender_name="Status",
        chat_id="tg_status",
        content=f"/start {token}",
    )

    # Now claimed
    claimed = await client.get(
        f"/api/v1/notifications/preferences/link/{token}",
        headers=user["headers"],
    )
    body = claimed.json()
    assert body["status"] == "claimed"
    assert body["contact_id"]
    assert body["claimed_at"]


@pytest.mark.asyncio
async def test_status_endpoint_isolates_users(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """User A's token must not be queryable by user B."""
    a = await _register(client, "iso_a")
    b = await _register(client, "iso_b")
    await _seed_telegram_cc(
        db_session, entity_id=a["entity_id"], owner_user_id=a["user_id"],
    )

    start = await client.post(
        "/api/v1/notifications/preferences/link/start",
        headers=a["headers"],
        json={"channel_type": "telegram"},
    )
    token = start.json()["token"]

    # B can't see A's token — they get not_found, not pending
    leaked = await client.get(
        f"/api/v1/notifications/preferences/link/{token}",
        headers=b["headers"],
    )
    assert leaked.json()["status"] == "not_found"
