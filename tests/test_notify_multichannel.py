"""End-to-end tests for the multi-channel ``notify()`` dispatcher.

These tests exercise the full path:

  notify() → notification_routing.resolve_channel_targets
          → notification_service.create_notification (in-app row + WS push)
          → channel_gateway.send_outbound_to_contact
          → ChannelAdapter.send_text

Channel adapters are monkeypatched so we don't hit Telegram / WeChat APIs
but still go through the registry lookup the production code uses.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import (
    ChannelConfig,
    ChannelContact,
    MessageLog,
)
from packages.core.models.notification import Notification
from packages.core.models.user import User
from packages.core.services import notify as notify_module
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _register(client: AsyncClient, username: str = "notify_user") -> dict:
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


async def _link_telegram_contact(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source_id: str = "987654321",
) -> tuple[ChannelConfig, ChannelContact]:
    """Seed a ChannelConfig + ChannelContact pair so the routing resolver
    finds an active Telegram binding for this user."""
    cc = ChannelConfig(
        entity_id=entity_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Test Telegram",
        config={},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.flush()

    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id=source_id,
        display_name="Test Telegram User",
        user_id=user_id,
        role="member",
        status="active",
    )
    db.add(contact)
    await db.commit()
    return cc, contact


async def _link_webchat_contact(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source_id: str,
) -> tuple[ChannelConfig, ChannelContact]:
    cc = ChannelConfig(
        entity_id=entity_id,
        channel_type="webchat",
        provider="webchat",
        name="Public Webchat",
        config={},
        credentials={},
        status="active",
    )
    db.add(cc)
    await db.flush()

    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="webchat",
        source_id=source_id,
        display_name="Dou",
        user_id=user_id,
        role="member",
        status="active",
    )
    db.add(contact)
    await db.commit()
    return cc, contact


class _FakeAdapter(ChannelAdapter):
    """Drop-in replacement for ChannelAdapter — records every send_text
    so tests can assert on dispatch behaviour without touching the
    network. Inherits the base class so the actionable-message default
    (text + "Reply with…" footer) is exercised here without extra glue.
    """

    def __init__(self, channel_type: str = "telegram") -> None:
        self.channel_type = channel_type
        self.sent: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    async def send_text(self, cc, to, text, **kwargs):
        if self.fail_with:
            raise self.fail_with
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_telegram(monkeypatch):
    """Replace the live Telegram adapter with a recording fake for the
    duration of one test. The ADAPTERS registry is module-level, so we
    snapshot + restore around the test."""
    fake = _FakeAdapter("telegram")
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = fake  # type: ignore[assignment]
    yield fake
    if original is None:
        ADAPTERS.pop("telegram", None)
    else:
        ADAPTERS["telegram"] = original


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_falls_back_to_inapp_when_no_contact_linked(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """User has Telegram in default_channels but no linked contact yet —
    the in-app row should still land, and no channel dispatch attempt."""
    ctx = await _register(client, "no_contact_user")

    # Set the user's prefs to include telegram
    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Test event",
        body="Body text",
    )

    # In-app notification row should exist
    rows = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].type == "task_failed"

    # No MessageLog because no contact was linked
    logs = (
        (await db_session.execute(select(MessageLog).where(MessageLog.entity_id == ctx["entity_id"]))).scalars().all()
    )
    assert logs == []


@pytest.mark.asyncio
async def test_notify_sends_email_to_registered_address_by_default(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """Email does not need a separate claim flow: the user's account email
    is the default notification recipient."""
    ctx = await _register(client, "email_default_user")
    sent: list[dict[str, str]] = []

    async def fake_send_notification_email(to: str, title: str, body: str) -> bool:
        sent.append({"to": to, "title": title, "body": body})
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_notification_email",
        fake_send_notification_email,
    )

    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["email"]},
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="booking_confirmed",
        title="New booking: Personal meeting",
        body="Ada booked Monday at 10:30 AM.",
        link="/tasks?view=calendar",
    )

    assert sent == [
        {
            "to": "email_default_user@test.com",
            "title": "New booking: Personal meeting",
            "body": "New booking: Personal meeting\n\nAda booked Monday at 10:30 AM.\n\n/tasks?view=calendar",
        }
    ]

    inapp = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(inapp) == 1
    assert inapp[0].type == "booking_confirmed"

    logs = (
        (await db_session.execute(select(MessageLog).where(MessageLog.entity_id == ctx["entity_id"]))).scalars().all()
    )
    assert len(logs) == 1
    assert logs[0].channel_type == "email"
    assert logs[0].to_address == "email_default_user@test.com"
    assert logs[0].status == "sent"


@pytest.mark.asyncio
async def test_notify_fans_out_to_telegram_when_linked(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    """With a linked ChannelContact, notify() should dispatch through the
    Telegram adapter in addition to writing the in-app row."""
    ctx = await _register(client, "fanout_user")
    cc, contact = await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    # User opts into telegram for task_hitl_requested
    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={
            "default_channels": [],
            "by_kind": {
                "task_hitl_requested": {"channels": ["telegram"], "enabled": True},
            },
        },
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve external reply",
        body="A customer message needs review.",
        link="/tasks/abc",
    )

    # In-app row written
    inapp = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(inapp) == 1
    assert inapp[0].type == "task_hitl_requested"

    # Telegram adapter received the call
    assert len(fake_telegram.sent) == 1
    payload = fake_telegram.sent[0]
    assert payload["to"] == contact.source_id
    assert "Approve external reply" in payload["text"]
    assert "A customer message needs review." in payload["text"]
    assert "/tasks/abc" in payload["text"]

    # MessageLog recorded the outbound with the adapter's external_id
    logs = (
        (await db_session.execute(select(MessageLog).where(MessageLog.entity_id == ctx["entity_id"]))).scalars().all()
    )
    assert len(logs) == 1
    assert logs[0].direction == "outbound"
    assert logs[0].channel_type == "telegram"
    assert logs[0].to_address == contact.source_id
    assert logs[0].status == "sent"
    assert logs[0].external_id == "ext-1"


@pytest.mark.asyncio
async def test_notify_disabled_kind_keeps_only_inapp_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    """When a kind is explicitly disabled in by_kind, externals are dropped
    but the in-app audit row still lands."""
    ctx = await _register(client, "disabled_kind_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={
            "default_channels": ["telegram"],
            "by_kind": {"task_failed": {"enabled": False}},
        },
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Failed",
        body="A task failed.",
    )

    # No telegram send attempts
    assert fake_telegram.sent == []

    # But in-app row exists
    inapp = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(inapp) == 1


@pytest.mark.asyncio
async def test_adapter_failure_does_not_block_inapp(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    """A broken channel adapter must not crash the dispatcher — the in-app
    row should still be written and the MessageLog should record the
    failure."""
    ctx = await _register(client, "fail_user")
    cc, contact = await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    fake_telegram.fail_with = RuntimeError("boom")

    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Failed",
        body="A task failed.",
    )

    # In-app row still written
    inapp = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(inapp) == 1

    # MessageLog records the failure
    logs = (
        (await db_session.execute(select(MessageLog).where(MessageLog.entity_id == ctx["entity_id"]))).scalars().all()
    )
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].error_message and "boom" in logs[0].error_message


@pytest.mark.asyncio
async def test_legacy_channels_argument_still_works(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    """Callers passing ``channels=["db", "ws"]`` should preserve old
    behaviour — only in-app, no external dispatch even when contact exists."""
    ctx = await _register(client, "legacy_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )
    assert resp.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Legacy path",
        channels=["db", "ws"],
    )

    # No telegram fan-out: explicit channel list pinned in-app only
    assert fake_telegram.sent == []

    inapp = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(inapp) == 1


# ── Preferences API ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preferences_round_trip(client: AsyncClient):
    ctx = await _register(client, "prefs_user")

    # Initial GET returns the catalog with empty user prefs
    get1 = await client.get(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
    )
    assert get1.status_code == 200
    body = get1.json()
    assert body["default_channels"] == []
    assert body["by_kind"] == {}
    assert body["supported_channels"]
    assert body["configured_channels"] == []
    assert any(e["kind"] == "task_hitl_requested" for e in body["event_catalog"])
    assert body["connected_channels"] == [
        {
            "channel_type": "email",
            "channel_config_id": "registered_email",
            "contact_id": f"registered_email:{ctx['user_id']}",
            "display_name": "Account email",
            "source_id": "prefs_user@test.com",
            "last_seen_at": None,
        }
    ]

    # PUT some prefs
    put_resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={
            "default_channels": ["telegram", "email", "bogus_channel"],
            "by_kind": {
                "task_failed": {"channels": ["email"]},
            },
            "quiet_hours": {"tz": "Asia/Shanghai", "from": "22:00", "to": "08:00"},
        },
    )
    assert put_resp.status_code == 200
    after = put_resp.json()
    # Unknown channel dropped during normalisation
    assert after["default_channels"] == ["telegram", "email"]
    assert after["by_kind"]["task_failed"]["channels"] == ["email"]
    assert after["quiet_hours"] == {
        "tz": "Asia/Shanghai",
        "from": "22:00",
        "to": "08:00",
    }

    # PUT with null kind drops the override
    drop_resp = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"by_kind": {"task_failed": None}},
    )
    assert drop_resp.status_code == 200
    assert "task_failed" not in drop_resp.json()["by_kind"]


@pytest.mark.asyncio
async def test_preferences_lists_connected_channels(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "connected_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    resp = await client.get(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200
    connected = resp.json()["connected_channels"]
    assert resp.json()["configured_channels"] == ["telegram"]
    assert [c["channel_type"] for c in connected] == ["email", "telegram"]
    assert connected[0]["source_id"] == "connected_user@test.com"
    assert connected[1]["source_id"] == "987654321"


@pytest.mark.asyncio
async def test_preferences_excludes_webchat_session_contacts(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "connected_webchat_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _link_webchat_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        source_id="webchat-session-1",
    )
    await _link_webchat_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        source_id="webchat-session-2",
    )

    resp = await client.get(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200
    connected = resp.json()["connected_channels"]
    assert [c["channel_type"] for c in connected] == ["email", "telegram"]
    assert all(c["display_name"] != "Dou" for c in connected)


@pytest.mark.asyncio
async def test_user_isolation_on_preferences(client: AsyncClient):
    """User A's preferences must not leak to User B."""
    a = await _register(client, "prefs_a")
    b = await _register(client, "prefs_b")

    await client.put(
        "/api/v1/notifications/preferences",
        headers=a["headers"],
        json={"default_channels": ["telegram"]},
    )

    b_resp = await client.get(
        "/api/v1/notifications/preferences",
        headers=b["headers"],
    )
    assert b_resp.json()["default_channels"] == []


# ── Sanity: User.preferences gets the right shape persisted ───────────────


@pytest.mark.asyncio
async def test_preferences_persisted_on_user_row(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "persist_user")
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={
            "default_channels": ["telegram"],
            "by_kind": {"task_failed": {"channels": ["email"], "enabled": True}},
        },
    )

    user = (await db_session.execute(select(User).where(User.id == ctx["user_id"]))).scalar_one()
    notif_prefs = (user.preferences or {}).get("notifications") or {}
    assert notif_prefs.get("default_channels") == ["telegram"]
    assert notif_prefs.get("by_kind", {}).get("task_failed") == {
        "channels": ["email"],
        "enabled": True,
    }
