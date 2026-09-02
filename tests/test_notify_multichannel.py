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

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import NotificationChannel
from packages.core.models.channel import (
    ChannelConfig,
    ChannelContact,
    MessageLog,
)
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.user import User
from packages.core.services import notification_scheduler
from packages.core.services import notify as notify_module
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter
from packages.core.services.notification_targets import (
    NotificationDeliveryTargetFactory,
    NotificationTargetKind,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_notification_delivery_target_factory_uses_canonical_keys() -> None:
    contact = NotificationDeliveryTargetFactory.external(
        channel_type=NotificationChannel.TELEGRAM.value,
        contact_id="contact-1",
    )
    address = NotificationDeliveryTargetFactory.external(
        channel_type=NotificationChannel.EMAIL.value,
        address=" User@Example.COM ",
    )
    broadcast = NotificationDeliveryTargetFactory.broadcast("entity-1")
    workspace_broadcast = NotificationDeliveryTargetFactory.broadcast(
        "entity-1",
        workspace_id="workspace-1",
    )

    assert contact.kind is NotificationTargetKind.CONTACT
    assert contact.key == "telegram:contact:contact-1"
    assert address.key == "email:address:user@example.com"
    assert broadcast.key == "broadcast:entity:entity-1"
    assert workspace_broadcast.key == "broadcast:workspace:workspace-1"
    assert NotificationDeliveryTargetFactory.parse(address.key) == address



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
        self.result_status = "sent"

    async def send_text(self, cc, to, text, **kwargs):
        if self.fail_with:
            raise self.fail_with
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {
            "status": self.result_status,
            "external_id": f"ext-{len(self.sent)}",
        }

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
async def test_actionable_notification_skips_registered_email_fallback(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "actionable_email_fallback_user")
    sent: list[str] = []

    async def fake_send_notification_email(to: str, _title: str, _body: str) -> bool:
        sent.append(to)
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_notification_email",
        fake_send_notification_email,
    )
    response = await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["email"]},
    )
    assert response.status_code == 200

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve this request",
        actions=[{"key": "approve", "label": "Approve"}],
        callback_kind="workspace.hitl.resolve_message",
    )

    assert sent == []
    assert (await db_session.execute(select(MessageLog))).scalars().all() == []
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "delivered"


@pytest.mark.asyncio
async def test_explicit_actionable_email_requires_linked_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "explicit_actionable_email_user")
    sent: list[str] = []

    async def fake_send_notification_email(to: str, _title: str, _body: str) -> bool:
        sent.append(to)
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_notification_email",
        fake_send_notification_email,
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve this request",
        channels=["email"],
        actions=[{"key": "approve", "label": "Approve"}],
        callback_kind="workspace.hitl.resolve_message",
    )

    assert sent == []
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"
    assert "no active target" in str(outbox.last_error)


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
async def test_notify_idempotency_key_deduplicates_row_and_external_send(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    ctx = await _register(client, "idempotent_notify_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    kwargs = {
        "entity_id": ctx["entity_id"],
        "user_id": ctx["user_id"],
        "type": "task_succeeded",
        "title": "Exactly one logical notification",
        "idempotency_key": "task:example:completed",
    }
    await notify_module.notify(**kwargs)
    await notify_module.notify(**kwargs)

    rows = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == ctx["user_id"])
        )
    ).scalars().all()
    assert len(rows) == 1
    assert len(fake_telegram.sent) == 1


@pytest.mark.asyncio
async def test_notify_idempotency_rejects_different_delivery_time(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "idempotent_delivery_time_user")
    first_time = datetime.now(timezone.utc) + timedelta(hours=1)
    kwargs = {
        "entity_id": ctx["entity_id"],
        "user_id": ctx["user_id"],
        "type": "task_succeeded",
        "title": "One delivery intent",
        "idempotency_key": "task:delivery-time",
    }
    await notify_module.notify(**kwargs, deliver_at=first_time)

    with pytest.raises(ValueError, match="different content"):
        await notify_module.notify(
            **kwargs,
            deliver_at=first_time + timedelta(minutes=5),
        )

    assert (await db_session.execute(select(Notification))).scalars().all()
    assert len((await db_session.execute(select(NotificationOutboxEvent))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_notify_idempotency_rejects_different_callback_payload(
    client: AsyncClient,
):
    ctx = await _register(client, "idempotent_callback_user")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    kwargs = {
        "entity_id": ctx["entity_id"],
        "user_id": ctx["user_id"],
        "type": "task_hitl_requested",
        "title": "Approve once",
        "actions": [{"key": "approve", "label": "Approve"}],
        "callback_kind": "workspace_hitl",
        "deliver_at": future,
        "idempotency_key": "hitl:delivery-intent",
    }
    await notify_module.notify(**kwargs, callback_payload={"request_id": "first"})

    with pytest.raises(ValueError, match="delivery intent"):
        await notify_module.notify(
            **kwargs,
            callback_payload={"request_id": "second"},
        )


@pytest.mark.asyncio
async def test_legacy_idempotency_conflict_stops_broadcast(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from packages.core.services import realtime

    ctx = await _register(client, "legacy_idempotency_conflict_user")
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(entity_id: str, event: str, data: dict) -> None:
        broadcasts.append({"entity_id": entity_id, "event": event, "data": data})

    monkeypatch.setattr(realtime, "_broadcast", fake_broadcast)

    kwargs = {
        "entity_id": ctx["entity_id"],
        "user_id": ctx["user_id"],
        "type": "system",
        "channels": ["db", "broadcast"],
        "idempotency_key": "legacy:one-logical-notification",
    }
    await notify_module.notify(**kwargs, title="First payload")

    with pytest.raises(ValueError, match="different content"):
        await notify_module.notify(**kwargs, title="Conflicting payload")

    rows = (await db_session.execute(select(Notification))).scalars().all()
    assert [row.title for row in rows] == ["First payload"]
    assert [item["data"]["title"] for item in broadcasts] == ["First payload"]


@pytest.mark.asyncio
async def test_workspace_legacy_broadcast_uses_workspace_scope(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.services import realtime

    ctx = await _register(client, "workspace_legacy_broadcast_user")
    created = await client.post(
        "/api/v1/workspaces",
        headers=ctx["headers"],
        json={"name": "Private Broadcast"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    entity_broadcasts: list[dict[str, Any]] = []
    workspace_broadcasts: list[dict[str, Any]] = []

    async def fake_entity_broadcast(entity_id: str, event: str, data: dict) -> None:
        entity_broadcasts.append({"entity_id": entity_id, "event": event, "data": data})

    async def fake_workspace_broadcast(
        entity_id: str,
        target_workspace_id: str,
        event: str,
        data: dict,
    ) -> None:
        workspace_broadcasts.append({
            "entity_id": entity_id,
            "workspace_id": target_workspace_id,
            "event": event,
            "data": data,
        })

    monkeypatch.setattr(realtime, "_broadcast", fake_entity_broadcast)
    monkeypatch.setattr(realtime, "_broadcast_workspace", fake_workspace_broadcast)

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Private Workspace event",
        channels=["broadcast"],
        workspace_id=workspace_id,
    )

    assert entity_broadcasts == []
    assert workspace_broadcasts == [{
        "entity_id": ctx["entity_id"],
        "workspace_id": workspace_id,
        "event": "system",
        "data": {
            "title": "Private Workspace event",
            "body": None,
            "link": None,
            "workspace_id": workspace_id,
        },
    }]


@pytest.mark.asyncio
async def test_notify_honors_explicit_external_channel(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    ctx = await _register(client, "explicit_channel_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Explicit Telegram",
        channels=["telegram"],
    )

    assert len(fake_telegram.sent) == 1
    assert "Explicit Telegram" in fake_telegram.sent[0]["text"]


@pytest.mark.asyncio
async def test_notify_rejects_empty_user_visible_payload(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "empty_notification_payload_user")

    with pytest.raises(ValueError, match="user-visible content"):
        await notify_module.notify(
            entity_id=ctx["entity_id"],
            user_id=ctx["user_id"],
            type="system",
            title="  ",
            channels=["telegram"],
        )

    assert (await db_session.execute(select(Notification))).scalars().all() == []
    assert (
        await db_session.execute(select(NotificationOutboxEvent))
    ).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("link_target", [True, False])
async def test_dispatch_rejects_persisted_empty_external_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
    link_target: bool,
):
    ctx = await _register(client, "persisted_empty_notification_user")
    if link_target:
        await _link_telegram_contact(
            db_session,
            entity_id=ctx["entity_id"],
            user_id=ctx["user_id"],
        )

    error_match = "no user-visible content" if link_target else "no active target"
    with pytest.raises(RuntimeError, match=error_match):
        await notify_module.dispatch_persisted_notification(
            notification_id="persisted_notification",
            entity_id=ctx["entity_id"],
            user_id=ctx["user_id"],
            type="system",
            title="",
            body=None,
            meta={},
            workspace_id=None,
            payload={"channels": ["telegram"]},
        )

    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_dispatch_rechecks_authorization_before_every_external_target(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "notification_target_fence_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    events: list[str] = []
    guard_calls = 0

    async def guard() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        events.append(f"guard:{guard_calls}")
        return guard_calls == 1

    async def deliver_channel(**_kwargs) -> bool:
        events.append("send:telegram")
        return True

    async def deliver_email(**_kwargs) -> bool:
        events.append("send:email")
        return True

    async def delivered(target_key: str) -> None:
        events.append(f"delivered:{target_key.split(':', 1)[0]}")

    monkeypatch.setattr(
        notify_module,
        "_deliver_via_channel_gateway",
        deliver_channel,
    )
    monkeypatch.setattr(
        notify_module,
        "_deliver_via_registered_email",
        deliver_email,
    )

    await notify_module.dispatch_persisted_notification(
        notification_id="persisted-notification",
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Recheck each target",
        body=None,
        meta={},
        workspace_id=None,
        payload={"channels": ["telegram", "email"]},
        before_external_target=guard,
        on_target_delivered=delivered,
    )

    assert events == [
        "guard:1",
        "send:telegram",
        "delivered:telegram",
        "guard:2",
    ]


@pytest.mark.asyncio
async def test_notify_retries_explicit_external_channel_without_target(
    client: AsyncClient,
    db_session: AsyncSession,
):
    ctx = await _register(client, "explicit_channel_missing_target_user")

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Explicit Telegram needs a target",
        channels=["telegram"],
    )

    outbox = (
        await db_session.execute(select(NotificationOutboxEvent))
    ).scalar_one()
    assert outbox.status == "pending"
    assert outbox.attempt_count == 1
    assert "no active target" in str(outbox.last_error)
    assert (
        await db_session.execute(select(MessageLog))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_notify_retries_when_explicit_channel_config_is_inactive(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    ctx = await _register(client, "inactive_channel_config_user")
    config, _contact = await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    config.status = "inactive"
    await db_session.commit()

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Inactive Telegram must not receive",
        channels=["telegram"],
    )

    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"
    assert outbox.attempt_count == 1
    assert "no active target" in str(outbox.last_error)
    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_channel_delivery_revalidates_contact_before_send(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    ctx = await _register(client, "blocked_channel_contact_user")
    _config, contact = await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    contact.status = "blocked"
    await db_session.commit()

    delivered = await notify_module._deliver_via_channel_gateway(
        channel_contact_id=contact.id,
        text="Must not be sent",
        notification_id=None,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )

    assert delivered is False
    assert fake_telegram.sent == []


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
async def test_adapter_failed_status_retries_outbox(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
):
    ctx = await _register(client, "adapter_failed_status_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    fake_telegram.result_status = "failed"

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Provider rejected this message",
        channels=["telegram"],
    )

    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"
    assert outbox.attempt_count == 1
    assert "channel delivery failed" in str(outbox.last_error)

    log = (await db_session.execute(select(MessageLog))).scalar_one()
    assert log.status == "failed"
    assert log.error_message == "adapter_status_failed"


@pytest.mark.asyncio
async def test_outbox_retry_skips_external_targets_that_already_succeeded(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
    monkeypatch,
):
    ctx = await _register(client, "partial_external_retry_user")
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    sent_email: list[str] = []

    async def fake_send_notification_email(to: str, _title: str, _body: str) -> bool:
        sent_email.append(to)
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_notification_email",
        fake_send_notification_email,
    )
    fake_telegram.result_status = "failed"

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Retry only the failed target",
        channels=["telegram", "email"],
    )

    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"
    assert sent_email == ["partial_external_retry_user@test.com"]
    assert len(fake_telegram.sent) == 1

    fake_telegram.result_status = "sent"
    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=outbox.available_at + timedelta(seconds=1),
    )

    assert result["dispatched"] == 1
    assert sent_email == ["partial_external_retry_user@test.com"]
    assert len(fake_telegram.sent) == 2


@pytest.mark.asyncio
async def test_workspace_broadcast_is_scoped_and_not_repeated_on_external_retry(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _FakeAdapter,
    monkeypatch,
):
    from packages.core.services import realtime

    ctx = await _register(client, "workspace_broadcast_retry_user")
    created = await client.post(
        "/api/v1/workspaces",
        headers=ctx["headers"],
        json={"name": "Retry Broadcast"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    await _link_telegram_contact(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    entity_broadcasts: list[dict[str, Any]] = []
    workspace_broadcasts: list[dict[str, Any]] = []

    async def fake_entity_broadcast(entity_id: str, event: str, data: dict) -> None:
        entity_broadcasts.append({"entity_id": entity_id, "event": event, "data": data})

    async def fake_workspace_broadcast(
        entity_id: str,
        target_workspace_id: str,
        event: str,
        data: dict,
    ) -> None:
        workspace_broadcasts.append({
            "entity_id": entity_id,
            "workspace_id": target_workspace_id,
            "event": event,
            "data": data,
        })

    monkeypatch.setattr(realtime, "_broadcast", fake_entity_broadcast)
    monkeypatch.setattr(realtime, "_broadcast_workspace", fake_workspace_broadcast)
    fake_telegram.result_status = "failed"

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Scoped retry",
        channels=["broadcast", "telegram"],
        workspace_id=workspace_id,
    )
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"

    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=outbox.available_at + timedelta(seconds=1),
    )

    assert result["retried"] == 1
    assert entity_broadcasts == []
    assert len(workspace_broadcasts) == 1
    assert workspace_broadcasts[0]["workspace_id"] == workspace_id


@pytest.mark.asyncio
async def test_media_job_notification_propagates_workspace_scope(monkeypatch) -> None:
    from packages.core.models.media_job import MediaJobStatus
    from packages.core.tasks import media_tasks

    captured: dict[str, Any] = {}

    async def fake_notify(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("packages.core.services.notify.notify", fake_notify)
    job = SimpleNamespace(
        id="media-job-1",
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        prompt="Private launch video",
        status=MediaJobStatus.COMPLETED,
        error=None,
        result_url="/files/private.mp4",
        duration_seconds=5,
        model="bytedance/seedance-2.0",
        params={
            "workspace_id": "workspace-1",
            "result_document_id": "document-1",
            "resolution": "720p",
        },
    )

    await media_tasks._push_notification(job)

    assert captured["workspace_id"] == "workspace-1"


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
