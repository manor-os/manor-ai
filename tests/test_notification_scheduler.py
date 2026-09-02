"""End-to-end tests for scheduled notification delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.workspace import Workspace
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
    outbox = (
        await db_session.execute(
            select(NotificationOutboxEvent).where(
                NotificationOutboxEvent.notification_id == rows[0].id,
            )
        )
    ).scalar_one()
    assert outbox.status == "pending"
    assert outbox.available_at == rows[0].deliver_at

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

    # The original pending row is the one durable notification. Dispatch
    # must not create a second bell/audit row.
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
    assert len(rows) == 1
    assert rows[0].dispatch_status == "dispatched"

    outbox = (
        await db_session.execute(
            select(NotificationOutboxEvent).where(
                NotificationOutboxEvent.notification_id == rows[0].id,
            )
        )
    ).scalar_one()
    assert outbox.status == "delivered"
    assert outbox.attempt_count == 1

    # Bell list now shows the dispatched rows
    list_resp = await client.get("/api/v1/notifications", headers=ctx["headers"])
    assert list_resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_sweeper_honors_explicit_external_channel(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_explicit_channel_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Scheduled explicit Telegram",
        channels=["telegram"],
        deliver_at=future,
    )

    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=future + timedelta(seconds=1),
    )

    assert result["dispatched"] == 1
    assert len(fake_telegram.sent) == 1
    assert "Scheduled explicit Telegram" in fake_telegram.sent[0]["text"]


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
async def test_sweeper_cancels_pending_delivery_when_workspace_paused(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    """A pending external delivery must not escape after its Workspace is paused."""
    ctx = await _register(client, "sched_paused_workspace_user")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=ctx["headers"],
        json={"name": "Paused notification workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
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
        title="Must not escape pause",
        workspace_id=workspace_id,
        deliver_at=future,
    )
    notification = (
        await db_session.execute(
            select(Notification).where(Notification.workspace_id == workspace_id)
        )
    ).scalar_one()
    outbox = (
        await db_session.execute(
            select(NotificationOutboxEvent).where(
                NotificationOutboxEvent.notification_id == notification.id,
            )
        )
    ).scalar_one()

    workspace = await db_session.get(Workspace, workspace_id)
    assert workspace is not None
    workspace.status = "paused"
    await db_session.commit()

    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=future + timedelta(minutes=1),
    )

    assert result["canceled"] == 1
    assert result["dispatched"] == 0
    assert fake_telegram.sent == []
    await db_session.refresh(notification)
    await db_session.refresh(outbox)
    assert notification.dispatch_status == "canceled"
    assert outbox.status == "canceled"


@pytest.mark.asyncio
async def test_claimed_notification_rechecks_workspace_before_provider_dispatch(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
    monkeypatch,
):
    """A pause committed after claim but before provider dispatch blocks send."""
    ctx = await _register(client, "sched_pause_race_user")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=ctx["headers"],
        json={"name": "Pause race workspace"},
    )
    workspace_id = workspace_response.json()["id"]
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Must not send after race",
        workspace_id=workspace_id,
        channels=["telegram"],
        deliver_at=future,
    )
    notification = (await db_session.execute(select(Notification))).scalar_one()
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    claims, _ = await notification_scheduler._claim_outbox_events(
        db_session,
        when=future + timedelta(seconds=1),
        batch_size=1,
    )
    event_id, claim_token = claims[0]

    original_authorize = notify_module.notification_recipient_is_authorized

    async def pause_before_provider(**kwargs):
        workspace = await db_session.get(Workspace, workspace_id)
        workspace.status = "paused"
        await db_session.commit()
        return await original_authorize(**kwargs)

    monkeypatch.setattr(
        notify_module,
        "notification_recipient_is_authorized",
        pause_before_provider,
    )
    outcome = await notification_scheduler._process_claimed_event(
        db_session,
        event_id=event_id,
        claim_token=claim_token,
        when=future + timedelta(seconds=1),
    )

    assert outcome == "canceled"
    assert fake_telegram.sent == []
    await db_session.refresh(notification)
    await db_session.refresh(outbox)
    assert notification.dispatch_status == "canceled"
    assert outbox.status == "canceled"


@pytest.mark.asyncio
async def test_claimed_notification_rechecks_workspace_between_external_targets(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """A commit after one target must not let later targets bypass a pause."""
    ctx = await _register(client, "sched_pause_between_targets_user")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=ctx["headers"],
        json={"name": "Pause between targets workspace"},
    )
    workspace_id = workspace_response.json()["id"]
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="system",
        title="Only the first target may send",
        workspace_id=workspace_id,
        channels=["telegram", "email"],
        deliver_at=future,
    )
    notification = (await db_session.execute(select(Notification))).scalar_one()
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    claims, _ = await notification_scheduler._claim_outbox_events(
        db_session,
        when=future + timedelta(seconds=1),
        batch_size=1,
    )
    event_id, claim_token = claims[0]
    sent: list[str] = []

    async def dispatch_two_targets(**kwargs):
        assert await kwargs["before_external_target"]() is True
        sent.append("first")
        await kwargs["on_target_delivered"]("telegram:contact-1")

        workspace = await db_session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.status = "paused"
        await db_session.commit()

        if await kwargs["before_external_target"]():
            sent.append("second")

    monkeypatch.setattr(
        notify_module,
        "dispatch_persisted_notification",
        dispatch_two_targets,
    )

    outcome = await notification_scheduler._process_claimed_event(
        db_session,
        event_id=event_id,
        claim_token=claim_token,
        when=future + timedelta(seconds=1),
    )

    assert outcome == "canceled"
    assert sent == ["first"]
    await db_session.refresh(notification)
    await db_session.refresh(outbox)
    assert notification.dispatch_status == "canceled"
    assert outbox.status == "canceled"


@pytest.mark.asyncio
async def test_sweeper_reclaims_stale_outbox_lease(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_stale_lease_user")
    await _link_telegram(db_session, entity_id=ctx["entity_id"], user_id=ctx["user_id"])
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    due = datetime.now(timezone.utc) - timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Recover me",
        deliver_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    row = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == ctx["user_id"])
        )
    ).scalar_one()
    outbox = (
        await db_session.execute(
            select(NotificationOutboxEvent).where(
                NotificationOutboxEvent.notification_id == row.id,
            )
        )
    ).scalar_one()
    outbox.status = "processing"
    outbox.available_at = due
    outbox.locked_until = due
    await db_session.commit()

    result = await notification_scheduler.dispatch_due_notifications(db_session)

    assert result["dispatched"] == 1
    assert len(fake_telegram.sent) == 1
    await db_session.refresh(outbox)
    assert outbox.status == "delivered"


@pytest.mark.asyncio
async def test_sweeper_retries_transient_outbox_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "sched_retry_user")
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Retry me",
        deliver_at=future,
    )

    async def fail_dispatch(**_kwargs):
        raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(notify_module, "dispatch_persisted_notification", fail_dispatch)
    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=future + timedelta(seconds=1),
    )

    assert result["retried"] == 1
    assert result["failed"] == 0
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert outbox.status == "pending"
    assert outbox.attempt_count == 1
    assert outbox.available_at > future
    assert "temporary provider outage" in (outbox.last_error or "")


@pytest.mark.asyncio
async def test_sweeper_retries_authorization_resolution_error(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "sched_auth_retry_user")
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Retry authorization",
        deliver_at=future,
    )

    async def fail_authorization(**_kwargs):
        raise RuntimeError("temporary authorization store outage")

    monkeypatch.setattr(
        notify_module,
        "notification_recipient_is_authorized",
        fail_authorization,
    )
    result = await notification_scheduler.dispatch_due_notifications(
        db_session,
        now=future + timedelta(seconds=1),
    )

    notification = (await db_session.execute(select(Notification))).scalar_one()
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    assert result["retried"] == 1
    assert result["canceled"] == 0
    assert notification.dispatch_status == "pending"
    assert outbox.status == "pending"
    assert "authorization store outage" in (outbox.last_error or "")


@pytest.mark.asyncio
async def test_sweeper_marks_outbox_failed_after_bounded_retries(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    ctx = await _register(client, "sched_retry_exhausted_user")
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Eventually stop retrying",
        deliver_at=future,
    )

    async def fail_dispatch(**_kwargs):
        raise RuntimeError("persistent provider outage")

    monkeypatch.setattr(notify_module, "dispatch_persisted_notification", fail_dispatch)

    when = future + timedelta(seconds=1)
    for attempt in range(1, 6):
        result = await notification_scheduler.dispatch_due_notifications(
            db_session,
            now=when,
        )
        outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
        assert outbox.attempt_count == attempt
        if attempt < 5:
            assert result["retried"] == 1
            assert result["failed"] == 0
            assert outbox.status == "pending"
            when = outbox.available_at + timedelta(seconds=1)
        else:
            assert result["retried"] == 0
            assert result["failed"] == 1
            assert outbox.status == "failed"
            assert outbox.locked_until is None


@pytest.mark.asyncio
async def test_sweeper_terminalizes_exhausted_stale_lease_without_sixth_attempt(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_stale_exhausted_user")
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_failed",
        title="Do not retry forever",
        deliver_at=future,
    )
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    outbox.status = "processing"
    outbox.attempt_count = 5
    outbox.available_at = stale
    outbox.locked_until = stale
    outbox.claim_token = "stale-claim"
    await db_session.commit()

    result = await notification_scheduler.dispatch_due_notifications(db_session)

    await db_session.refresh(outbox)
    assert result["due"] == 1
    assert result["failed"] == 1
    assert outbox.status == "failed"
    assert outbox.attempt_count == 5
    assert outbox.claim_token is None
    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_stale_worker_cannot_finish_newer_claim(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_fenced_claim_user")
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
        title="Fence the old worker",
        deliver_at=future,
    )
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    outbox.status = "processing"
    outbox.attempt_count = 2
    outbox.available_at = future
    outbox.locked_until = future + timedelta(minutes=5)
    outbox.claim_token = "new-claim"
    await db_session.commit()

    stale_outcome = await notification_scheduler._process_claimed_event(
        db_session,
        event_id=outbox.id,
        claim_token="old-claim",
        when=future + timedelta(seconds=1),
    )
    current_outcome = await notification_scheduler._process_claimed_event(
        db_session,
        event_id=outbox.id,
        claim_token="new-claim",
        when=future + timedelta(seconds=1),
    )

    assert stale_outcome == "canceled"
    assert current_outcome == "dispatched"
    assert len(fake_telegram.sent) == 1


@pytest.mark.asyncio
async def test_cancel_scheduled_preempts_claimed_but_not_visible_event(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_cancel_claimed_user")
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
        title="Cancel while claimed",
        deliver_at=future,
    )
    notification = (await db_session.execute(select(Notification))).scalar_one()
    outbox = (await db_session.execute(select(NotificationOutboxEvent))).scalar_one()
    outbox.status = "processing"
    outbox.attempt_count = 1
    outbox.locked_until = future + timedelta(minutes=5)
    outbox.claim_token = "claimed-before-cancel"
    await db_session.commit()

    assert await notification_scheduler.cancel_scheduled(
        db_session,
        notification_id=notification.id,
    )
    await db_session.commit()
    outcome = await notification_scheduler._process_claimed_event(
        db_session,
        event_id=outbox.id,
        claim_token="claimed-before-cancel",
        when=future + timedelta(seconds=1),
    )

    await db_session.refresh(notification)
    await db_session.refresh(outbox)
    assert outcome == "canceled"
    assert notification.dispatch_status == "canceled"
    assert outbox.status == "canceled"
    assert outbox.claim_token is None
    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_sweeper_skips_event_locked_by_another_dispatcher(
    client: AsyncClient,
    fake_telegram: _Adapter,
):
    ctx = await _register(client, "sched_concurrent_dispatcher_user")
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Only one dispatcher",
        deliver_at=future,
    )

    # The client fixture replaces this factory with one bound to the test DB.
    import packages.core.database as db_module

    when = future + timedelta(seconds=1)
    async with db_module.async_session() as lock_session:
        await lock_session.execute(
            select(NotificationOutboxEvent).with_for_update()
        )
        async with db_module.async_session() as dispatch_session:
            skipped = await notification_scheduler.dispatch_due_notifications(
                dispatch_session,
                now=when,
            )
            assert skipped["due"] == 0
            assert fake_telegram.sent == []
        await lock_session.rollback()

    async with db_module.async_session() as dispatch_session:
        delivered = await notification_scheduler.dispatch_due_notifications(
            dispatch_session,
            now=when,
        )
        assert delivered["due"] == 1
        assert delivered["dispatched"] == 1


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
