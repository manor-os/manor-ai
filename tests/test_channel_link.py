"""End-to-end: user mints a link token in Settings, sends /start <token>
to the bot, and their ChannelContact picks up user_id + role.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
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
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


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
    bot_username: str = "ManorTestBot",
) -> ChannelConfig:
    cc = ChannelConfig(
        entity_id=entity_id,
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


# ── Start endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_link_returns_token_and_deep_link(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _register(client, "link_starter")
    await _seed_telegram_cc(db_session, entity_id=user["entity_id"])

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
    cc = await _seed_telegram_cc(db_session, entity_id=user["entity_id"])

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
    cc = await _seed_telegram_cc(db_session, entity_id=user["entity_id"])
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
    cc = await _seed_telegram_cc(db_session, entity_id=user["entity_id"])

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
    cc = await _seed_telegram_cc(db_session, entity_id=user["entity_id"])

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


# ── Status polling endpoint ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_endpoint_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    user = await _register(client, "status_user")
    cc = await _seed_telegram_cc(db_session, entity_id=user["entity_id"])
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
    await _seed_telegram_cc(db_session, entity_id=a["entity_id"])

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
