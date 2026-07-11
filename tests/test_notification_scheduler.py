"""End-to-end tests for scheduled notification delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.notification import Notification
from packages.core.services import notify as notify_module
from packages.core.services import notification_scheduler
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


class _Adapter(ChannelAdapter):
    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_telegram():
    fake = _Adapter()
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = fake  # type: ignore[assignment]
    yield fake
    if original is None:
        ADAPTERS.pop("telegram", None)
    else:
        ADAPTERS["telegram"] = original


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


async def _link_telegram(db: AsyncSession, *, entity_id: str, user_id: str) -> ChannelContact:
    cc = ChannelConfig(
        entity_id=entity_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Test",
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
        source_id="tg_user_1",
        display_name="Tester",
        user_id=user_id,
        role="member",
        status="active",
    )
    db.add(contact)
    await db.commit()
    return contact


# ── Scheduling ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_future_deliver_at_persists_pending_row_without_dispatch(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """notify(deliver_at=future) should write a pending row and SKIP the
    external dispatch — the user shouldn't get a Telegram push yet."""
    ctx = await _register(client, "sched_pending_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Daily report",
        body="Your report is ready",
        deliver_at=future,
    )

    # Row exists with dispatch_status='pending'
    rows = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].dispatch_status == "pending"
    assert rows[0].deliver_at is not None
    # Producer args stashed under meta._scheduled
    sch = rows[0].meta.get("_scheduled") if isinstance(rows[0].meta, dict) else None
    assert isinstance(sch, dict)

    # No telegram push yet
    assert fake_telegram.sent == []

    # Bell-icon list endpoint hides pending rows
    list_resp = await client.get("/api/v1/notifications", headers=ctx["headers"])
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 0
    assert body["unread_count"] == 0


@pytest.mark.asyncio
async def test_past_deliver_at_dispatches_immediately(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """A deliver_at in the past should fall through to immediate
    dispatch — catching up after worker downtime is exactly when this
    happens in production."""
    ctx = await _register(client, "sched_past_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Late report",
        deliver_at=past,
    )
    # Telegram push went through immediately
    assert len(fake_telegram.sent) == 1
    # Notification row is dispatched (not pending)
    rows = (
        (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].dispatch_status == "dispatched"


@pytest.mark.asyncio
async def test_sweeper_dispatches_due_notifications(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """Once deliver_at passes, ``dispatch_due_notifications`` should flip
    the pending row, fan it out to external channels, and increment the
    bell unread count."""
    ctx = await _register(client, "sched_sweeper_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    # Schedule in the near future
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Scheduled report",
        body="Your report is ready",
        deliver_at=future,
    )
    assert fake_telegram.sent == []

    # Simulate time passing by running the sweeper with a now=future+1m
    when = future + timedelta(minutes=1)
    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=when,
    )
    assert result["due"] == 1
    assert result["dispatched"] == 1
    assert result["failed"] == 0

    # External channel got the push
    assert len(fake_telegram.sent) == 1
    assert "Scheduled report" in fake_telegram.sent[0]["text"]

    # The original pending row is now dispatched. A SECOND notification
    # row was also written by the recursive notify() during dispatch —
    # that's the audit row the user actually sees on the bell.
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(Notification)
                .where(
                    Notification.user_id == ctx["user_id"],
                )
                .order_by(Notification.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    # Two rows: the original (now dispatched scheduled row) + the
    # immediate audit row from the recursive notify().
    assert len(rows) == 2
    statuses = {r.dispatch_status for r in rows}
    assert "dispatched" in statuses

    # Bell list now shows the dispatched rows
    list_resp = await client.get("/api/v1/notifications", headers=ctx["headers"])
    assert list_resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_sweeper_skips_future_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """The sweeper must not pick up rows whose deliver_at hasn't elapsed."""
    ctx = await _register(client, "sched_skip_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    later = datetime.now(timezone.utc) + timedelta(hours=2)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Far future",
        deliver_at=later,
    )

    # Sweep "now" — nothing due yet
    result = await notification_scheduler.dispatch_due_notifications(db_session)
    assert result["due"] == 0
    assert result["dispatched"] == 0
    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_cancel_scheduled_drops_pending_row(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """``cancel_scheduled`` should mark a pending row canceled so the
    sweeper never dispatches it."""
    ctx = await _register(client, "sched_cancel_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Will be cancelled",
        deliver_at=future,
    )
    row = (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalar_one()

    notif_id = row.id
    ok = await notification_scheduler.cancel_scheduled(db_session, notification_id=notif_id)
    assert ok is True
    await db_session.commit()

    # Now run the sweeper past the deliver_at; the canceled row should
    # NOT be picked up.
    when = future + timedelta(minutes=1)
    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=when,
    )
    assert result["due"] == 0
    assert fake_telegram.sent == []

    # Re-read via a fresh statement; pulling the existing in-memory
    # instance can collide with attribute expiry on async sessions.
    await db_session.refresh(row)
    assert row.dispatch_status == "canceled"


@pytest.mark.asyncio
async def test_cancel_scheduled_returns_false_for_already_dispatched(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """Trying to cancel an already-dispatched row should be a no-op."""
    ctx = await _register(client, "sched_cancel_late_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    # Fire an immediate notification (status=dispatched)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Already done",
    )
    db_session.expire_all()
    row = (await db_session.execute(select(Notification).where(Notification.user_id == ctx["user_id"]))).scalar_one()
    assert row.dispatch_status == "dispatched"

    ok = await notification_scheduler.cancel_scheduled(
        db_session,
        notification_id=row.id,
    )
    assert ok is False
