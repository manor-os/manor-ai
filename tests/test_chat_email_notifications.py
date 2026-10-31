"""Native chat digests use real persistence/authority and a fake email provider."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import ChatRoom, ChatRoomFile, ChatRoomMember, ChatRoomMessage
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.user import Entity, User
from packages.core.services import chat_email_notifications as chat_email
from packages.core.services import email_service, notification_scheduler as scheduler
from packages.core.services.notification_service import list_notifications

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def chat(db_session, monkeypatch, isolated_test_redis):
    db = db_session
    entity = Entity(id=generate_ulid(), name="Email test owner")
    recipient = User(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        email=f"{generate_ulid()}@example.com",
        display_name="Recipient",
        password_hash="unused",
    )
    sender = User(
        id=generate_ulid(),
        entity_id=entity.id,
        email=f"{generate_ulid()}@example.com",
        display_name="Sender <script>",
        password_hash="unused",
    )
    recipient.verified_email, recipient.email_verified_at = recipient.email, NOW
    db.add_all([entity, recipient, sender])
    await db.flush()
    room = ChatRoom(id=generate_ulid(), kind="group", title="Team <script>", owner_entity_id=entity.id, status="active")
    db.add(room)
    await db.flush()
    member = ChatRoomMember(
        id=generate_ulid(),
        room_id=room.id,
        user_id=recipient.id,
        visible_from=NOW - timedelta(days=1),
        status="active",
        notifications_muted=False,
    )
    db.add(member)
    await db.flush()
    message = ChatRoomMessage(
        id=generate_ulid(),
        room_id=room.id,
        sender_user_id=sender.id,
        content="Please review <img src=x onerror=alert(1)>",
        created_at=NOW - timedelta(minutes=30),
    )
    db.add(message)
    await db.flush()
    db.add(ChatRoomFile(message_id=message.id, name="report <img>.csv", mime_type="text/csv", size=3, content=b"a,b"))
    await db.commit()
    sent = []

    async def send(to, subject, html, text):
        sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    monkeypatch.setattr(email_service, "send_email", send)
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    # Snapshot cleanup IDs: failed delivery rollback may expire ORM instances.
    user_ids, room_id, entity_id = [recipient.id, sender.id], room.id, entity.id
    yield db, entity, recipient, sender, room, member, message, sent
    await db.rollback()
    await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
    await db.execute(delete(ChatRoom).where(ChatRoom.id == room_id))
    await db.execute(delete(User).where(User.id.in_(user_ids)))
    await db.execute(delete(Entity).where(Entity.id == entity_id))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "dm"])
async def test_digest_waits_then_sends_only_new_unread_once(chat, kind):
    db, _, user, sender, room, member, message, sent = chat
    if kind == "dm":
        room.kind, room.owner_entity_id, room.owner_user_id = "dm", None, sender.id
    # Older pre-membership, own, deleted and not-yet-mature messages must stay out.
    for text, author, created, deleted in [
        ("Before join", sender.id, member.visible_from - timedelta(seconds=1), None),
        ("My own message", user.id, NOW - timedelta(hours=1), None),
        ("Retracted", sender.id, NOW - timedelta(hours=1), NOW),
        ("Too recent", sender.id, NOW - timedelta(minutes=10), None),
    ]:
        db.add(
            ChatRoomMessage(
                room_id=room.id, sender_user_id=author, content=text, created_at=created, deleted_at=deleted
            )
        )
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW - timedelta(seconds=1)) == 0
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 1
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 0
    result = await scheduler.dispatch_due_notifications(db, now=NOW)
    assert result["dispatched"] == 1
    assert len(sent) == 1 and sent[0]["to"] == user.email
    assert "1 unread messages" in sent[0]["subject"]
    assert "report &lt;img&gt;.csv" in sent[0]["html"]
    assert "<script>" not in sent[0]["html"] and "<img src=x" not in sent[0]["html"]
    assert f"/chat?room={room.id}" in sent[0]["html"]
    assert "email-logo.png" in sent[0]["html"]  # Existing shared email frame.
    assert all(text not in sent[0]["text"] for text in ("Before join", "My own message", "Retracted", "Too recent"))
    assert (await list_notifications(db, user.entity_id, user.id))[1] == 0
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(minutes=29)) == 0
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(minutes=30)) == 1
    await scheduler.dispatch_due_notifications(db, now=NOW + timedelta(minutes=30))
    assert len(sent) == 2 and "Too recent" in sent[1]["text"]
    assert "Please review" not in sent[1]["text"]
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(days=1)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    [
        "read",
        "muted",
        "left",
        "rejoined",
        "closed",
        "owner_deleted",
        "unverified",
        "email_changed",
        "disabled",
        "retracted",
    ],
)
async def test_queued_digest_rechecks_live_access_and_preferences(chat, revocation):
    db, owner, user, _, room, member, message, sent = chat
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 1
    if revocation == "read":
        member.last_read_at = NOW
    elif revocation == "muted":
        member.notifications_muted = True
    elif revocation == "left":
        member.status = "left"
    elif revocation == "rejoined":
        member.visible_from = NOW
    elif revocation == "closed":
        room.status = "closed"
    elif revocation == "owner_deleted":
        owner.deleted_at = NOW
    elif revocation == "unverified":
        user.email_verified_at = None
    elif revocation == "email_changed":
        user.email = f"{generate_ulid()}@example.com"
    elif revocation == "disabled":
        user.preferences = {"notifications": {"chat_email": {"enabled": False}}}
    elif revocation == "retracted":
        message.deleted_at = NOW
    await db.commit()
    assert (await scheduler.dispatch_due_notifications(db, now=NOW))["canceled"] == 1
    assert sent == []
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(days=1)) == 0


@pytest.mark.asyncio
async def test_custom_delay_defer_quiet_hours_and_provider_retry(chat, monkeypatch):
    db, _, user, sender, room, member, message, sent = chat
    sender_id, room_id = sender.id, room.id
    user.preferences = {"notifications": {"chat_email": {"delay_minutes": 5}}}
    message.created_at = NOW - timedelta(minutes=5)
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW - timedelta(seconds=1)) == 0
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 1
    user.preferences = {
        "notifications": {
            "chat_email": {"delay_minutes": 60},
            "quiet_hours": {"from": "12:00", "to": "13:00", "tz": "UTC"},
        }
    }
    await db.commit()
    assert (await scheduler.dispatch_due_notifications(db, now=NOW))["deferred"] == 1
    notification_id = await db.scalar(
        select(Notification.id).where(Notification.user_id == user.id, Notification.type == chat_email.KIND)
    )
    event = await db.scalar(
        select(NotificationOutboxEvent).where(NotificationOutboxEvent.notification_id == notification_id)
    )
    assert event.available_at == NOW + timedelta(minutes=55) and event.attempt_count == 0
    assert (await scheduler.dispatch_due_notifications(db, now=NOW + timedelta(minutes=55)))["deferred"] == 1
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: False)
    assert (await scheduler.dispatch_due_notifications(db, now=NOW + timedelta(hours=1)))["deferred"] == 1
    assert sent == []
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    original_send = email_service.send_email

    async def failure(*args):
        return False

    monkeypatch.setattr(email_service, "send_email", failure)
    assert (await scheduler.dispatch_due_notifications(db, now=NOW + timedelta(minutes=61)))["retried"] == 1
    monkeypatch.setattr(email_service, "send_email", original_send)
    assert (await scheduler.dispatch_due_notifications(db, now=NOW + timedelta(minutes=62)))["dispatched"] == 1
    assert len(sent) == 1
    await db.refresh(event)
    assert event.attempt_count == 2 and event.status == "delivered"
    # Quiet hours/provider retries must not release a second overdue digest
    # immediately after the first finally reaches the recipient.
    db.add(
        ChatRoomMessage(
            room_id=room_id, sender_user_id=sender_id, content="Next batch", created_at=NOW + timedelta(minutes=1)
        )
    )
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(minutes=62)) == 0
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(minutes=122)) == 1


@pytest.mark.asyncio
async def test_concurrent_discovery_and_read_serialize_with_delivery(chat, monkeypatch):
    db, _, user, _, _, member, _, sent = chat
    user_id, member_id = user.id, member.id
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)

    async def queue():
        async with sessions() as other:
            return await chat_email.queue_due_chat_emails(other, now=NOW)

    assert sum(await asyncio.gather(queue(), queue())) == 1
    arrived, release, read_started, read_done = (asyncio.Event() for _ in range(4))
    original_send = email_service.send_email

    async def paused_send(*args):
        arrived.set()
        await release.wait()
        return await original_send(*args)

    async def mark_read():
        async with sessions() as other:
            read_started.set()
            await other.execute(update(ChatRoomMember).where(ChatRoomMember.id == member_id).values(last_read_at=NOW))
            await other.commit()
            read_done.set()

    monkeypatch.setattr(email_service, "send_email", paused_send)
    delivery = asyncio.create_task(scheduler.dispatch_due_notifications(db, now=NOW))
    reader = None
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        reader = asyncio.create_task(mark_read())
        await read_started.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_done.wait(), 0.1)
        release.set()
        assert (await asyncio.wait_for(delivery, 5))["dispatched"] == 1
        await asyncio.wait_for(reader, 5)
    finally:
        release.set()
        await asyncio.gather(delivery, *([reader] if reader else []), return_exceptions=True)
    assert len(sent) == 1
    assert await db.scalar(
        select(Notification.id).where(Notification.user_id == user_id, Notification.type == chat_email.KIND)
    )
    assert await chat_email.queue_due_chat_emails(db, now=NOW + timedelta(days=1)) == 0


@pytest.mark.asyncio
async def test_worker_sends_bounded_digest_after_live_partial_read(chat, monkeypatch):
    from packages.core import database
    from packages.core.tasks import channel_tasks

    db, _, user, sender, room, member, original, sent = chat
    original.created_at = NOW - timedelta(minutes=40)
    for index in range(4):
        db.add(
            ChatRoomMessage(
                room_id=room.id,
                sender_user_id=sender.id,
                content=f"Summary {index}",
                created_at=NOW - timedelta(minutes=34 - index),
            )
        )
    user.preferences = {"notifications": {"chat_email": {"enabled": False}}}
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 0
    user.preferences = {}
    user.email_verified_at = None
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 0
    user.email_verified_at = NOW
    await db.commit()
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 1
    member.last_read_at = NOW - timedelta(minutes=39)
    await db.commit()

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(chat_email, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler, "datetime", FixedDateTime)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(channel_tasks, "_run_async", lambda coroutine: coroutine)
    result = await channel_tasks.dispatch_due_notifications_task.run()
    assert result["dispatched"] == 1
    assert len(sent) == 1 and "4 unread messages" in sent[0]["subject"]
    assert all(f"Summary {index}" in sent[0]["text"] for index in (1, 2, 3))
    assert "Summary 0" not in sent[0]["text"] and "Please review" not in sent[0]["text"]


@pytest.mark.asyncio
async def test_stale_email_claim_cannot_send(chat):
    db, _, _, _, _, _, _, sent = chat
    assert await chat_email.queue_due_chat_emails(db, now=NOW) == 1
    first, _ = await scheduler._claim_outbox_events(db, when=NOW, batch_size=1)
    event_id, old_token = first[0]
    second, _ = await scheduler._claim_outbox_events(db, when=NOW + timedelta(minutes=6), batch_size=1)
    assert second[0][0] == event_id and second[0][1] != old_token
    assert await scheduler._process_claimed_event(db, event_id=event_id, claim_token=old_token, when=NOW) == "canceled"
    assert sent == []
    assert (
        await scheduler._process_claimed_event(
            db, event_id=event_id, claim_token=second[0][1], when=NOW + timedelta(minutes=6)
        )
        == "dispatched"
    )
    assert len(sent) == 1
