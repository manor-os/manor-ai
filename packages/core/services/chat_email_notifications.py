"""Delayed native group/DM digests; the existing outbox owns retries and leases.

Only reference ranges are queued. Content and the verified registered recipient
are resolved under live chat authority immediately before the external send.
These email-only parents stay hidden from the in-app notification list.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import DateTime, Integer, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from packages.core.models.chat_room import ChatRoom, ChatRoomFile, ChatRoomMember, ChatRoomMessage
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.user import Entity, User
from packages.core.services import email_service
from packages.core.services.notification_routing import _is_in_quiet_hours
from packages.core.services.notification_service import create_notification_once
from packages.core.services.user_email_proof import email_is_verified

KIND = "chat_unread_email"
DELAYS = (5, 15, 30, 60, 120)


def _preferences(user: User) -> tuple[dict, bool, int]:
    prefs = (user.preferences or {}).get("notifications") or {}
    chat = prefs.get("chat_email") or {}
    delay = chat.get("delay_minutes", 30)
    return prefs, chat.get("enabled", True) is not False, delay if delay in DELAYS else 30


def _messages(member: ChatRoomMember):
    return select(ChatRoomMessage).where(
        ChatRoomMessage.room_id == member.room_id,
        ChatRoomMessage.created_at >= member.visible_from,
        ChatRoomMessage.created_at > func.coalesce(member.last_read_at, member.visible_from),
        ChatRoomMessage.sender_user_id.is_distinct_from(member.user_id),
        ChatRoomMessage.deleted_at.is_(None),
        or_(ChatRoomMessage.agent_run_status.is_(None), ChatRoomMessage.agent_run_status == "succeeded"),
    )


def _due_members(when: datetime):
    member = ChatRoomMember
    history = and_(
        Notification.user_id == member.user_id,
        Notification.type == KIND,
        Notification.meta["member_id"].astext == member.id,
    )
    previous_through = (
        select(func.max(cast(Notification.meta["through"].astext, DateTime(timezone=True))))
        .where(history)
        .correlate(member)
        .scalar_subquery()
    )
    previous_at = (
        select(func.max(func.greatest(Notification.created_at, NotificationOutboxEvent.delivered_at)))
        .join(NotificationOutboxEvent)
        .where(history)
        .correlate(member)
        .scalar_subquery()
    )
    outstanding = (
        select(Notification.id)
        .join(NotificationOutboxEvent)
        .where(
            history,
            NotificationOutboxEvent.status.in_(("pending", "processing")),
        )
        .correlate(member)
        .exists()
    )
    chat = User.preferences["notifications"]["chat_email"]
    minutes = case(
        (
            chat["delay_minutes"].astext.in_([str(value) for value in DELAYS]),
            cast(chat["delay_minutes"].astext, Integer),
        ),
        else_=30,
    )
    cutoff = literal(when, type_=DateTime(timezone=True)) - minutes * timedelta(minutes=1)
    mature = (
        _messages(member)
        .where(
            ChatRoomMessage.created_at <= cutoff,
            or_(previous_through.is_(None), ChatRoomMessage.created_at > previous_through),
        )
        .correlate(member, User)
        .exists()
    )
    owner = aliased(User)
    owner_active = or_(
        select(Entity.id).where(Entity.id == ChatRoom.owner_entity_id, Entity.deleted_at.is_(None)).exists(),
        select(owner.id)
        .where(owner.id == ChatRoom.owner_user_id, owner.status == "active", owner.deleted_at.is_(None))
        .exists(),
    )
    return (
        select(member)
        .join(User, User.id == member.user_id)
        .join(ChatRoom, ChatRoom.id == member.room_id)
        .where(
            member.status == "active",
            member.notifications_muted.is_(False),
            ChatRoom.status == "active",
            owner_active,
            User.status == "active",
            User.deleted_at.is_(None),
            User.email != "",
            User.verified_email == User.email,
            User.email_verified_at.is_not(None),
            func.coalesce(chat["enabled"].astext, "true") != "false",
            or_(previous_at.is_(None), previous_at <= cutoff),
            ~outstanding,
            mature,
        )
    )


async def queue_due_chat_emails(db: AsyncSession, *, now: datetime | None = None, batch_size: int = 50) -> int:
    when = now or datetime.now(timezone.utc)
    candidates = list(
        (
            await db.scalars(
                _due_members(when).with_only_columns(ChatRoomMember.id).order_by(ChatRoomMember.id).limit(batch_size)
            )
        ).all()
    )
    queued = 0
    for member_id in candidates:
        member = await db.scalar(
            select(ChatRoomMember).where(ChatRoomMember.id == member_id).with_for_update(skip_locked=True)
        )
        # Re-evaluate history after locking: another producer may have committed
        # since the candidate scan. Never create overlapping digest ranges.
        if member is None or await db.scalar(_due_members(when).where(ChatRoomMember.id == member_id)) is None:
            await db.commit()
            continue
        user = await db.get(User, member.user_id)
        _, _, delay = _preferences(user)
        previous = await db.scalar(
            select(func.max(cast(Notification.meta["through"].astext, DateTime(timezone=True)))).where(
                Notification.user_id == member.user_id,
                Notification.type == KIND,
                Notification.meta["member_id"].astext == member.id,
            )
        )
        since = max(member.last_read_at or member.visible_from, previous or member.visible_from)
        through = await db.scalar(
            _messages(member)
            .with_only_columns(func.max(ChatRoomMessage.created_at))
            .where(
                ChatRoomMessage.created_at > since,
                ChatRoomMessage.created_at <= when - timedelta(minutes=delay),
            )
        )
        if through is not None:
            notification, created = await create_notification_once(
                db,
                user.entity_id,
                user.id,
                KIND,
                "Unread chat messages",
                idempotency_key=f"chat-email:{member.id}:{through.isoformat()}",
                meta={
                    "member_id": member.id,
                    "room_id": member.room_id,
                    "since": since.isoformat(),
                    "through": through.isoformat(),
                },
                deliver_at=when,
                dispatch_status="pending",
                push_realtime=False,
            )
            if created:
                notification.created_at = when
                db.add(NotificationOutboxEvent(notification_id=notification.id, payload={}, available_at=when))
                queued += 1
        await db.commit()
    await db.commit()
    return queued


def render_digest(
    *, title: str, room_id: str, count: int, messages: list[dict], locale: str = "en"
) -> tuple[str, str, str]:
    """Render the existing Manor email frame, with escaped bounded excerpts."""
    copy = {
        "en": (
            "Unread messages",
            "{count} unread messages in {title}",
            "Open chat",
            "Notification settings",
            "You receive this email because messages are still unread. Change the delay or turn reminders off in Settings.",
        ),
        "zh": (
            "未读消息",
            "{title} 中有 {count} 条未读消息",
            "打开聊天",
            "通知设置",
            "这些消息仍未读，因此我们发送了提醒。你可以在设置中修改等待时间或关闭邮件提醒。",
        ),
        "es": (
            "Mensajes sin leer",
            "{count} mensajes sin leer en {title}",
            "Abrir chat",
            "Ajustes de notificaciones",
            "Recibes este correo porque hay mensajes sin leer. Cambia el tiempo de espera o desactiva los recordatorios en Ajustes.",
        ),
    }
    language = (locale or "en").split("-")[0]
    heading, summary, action, settings, reason = copy.get(language, copy["en"])
    summary = summary.format(count=count, title=title)
    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    link, preferences = f"{app_url}/chat?room={room_id}", f"{app_url}/settings?tab=notifications"
    rows, plain = [], [summary, ""]
    for message in messages:
        sender, stamp, text = message["sender"], message["time"], message["text"]
        files = " · ".join(message["files"])
        attachment = f'<p style="margin:10px 0 0;color:#6c706c;font-size:13px">📎 {escape(files)}</p>' if files else ""
        rows.append(
            f'<tr><td style="padding:18px 0;border-bottom:1px solid #e9e8e4"><div style="font-weight:600;color:#242a27">{escape(sender)} <span style="font-weight:400;font-size:12px;color:#787e79">{escape(stamp)}</span></div><p style="margin:8px 0 0;line-height:1.6;overflow-wrap:anywhere">{escape(text).replace(chr(10), "<br>")}</p>{attachment}</td></tr>'
        )
        plain.extend([f"{sender} · {stamp}", text, files, ""])
    content = f'''<div style="white-space:normal">
      <p style="font-size:12px;letter-spacing:1px;color:#52756e;text-transform:uppercase">{escape(heading)}</p>
      <h1 style="font-size:24px;line-height:1.35;margin:12px 0">{escape(summary)}</h1>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>
      <p style="margin:28px 0"><a href="{escape(link, quote=True)}" style="display:inline-block;padding:14px 24px;border-radius:12px;background:#52756e;color:#fff;text-decoration:none;font-weight:600">{escape(action)}</a></p>
      <p style="font-size:12px;line-height:1.6;color:#787e79">{escape(reason)}<br><a href="{escape(preferences, quote=True)}" style="color:#52756e">{escape(settings)}</a></p></div>'''
    plain.extend([link, "", reason, preferences])
    subject = " ".join(summary.splitlines())[:250]
    return subject, email_service._render("common", content=content, email_locale=language), "\n".join(plain)


async def deliver_chat_email(
    db: AsyncSession, *, event_id: str, notification_id: str, claim_token: str, now: datetime
) -> str:
    notification = await db.scalar(select(Notification).where(Notification.id == notification_id).with_for_update())
    event = await db.scalar(
        select(NotificationOutboxEvent)
        .where(
            NotificationOutboxEvent.id == event_id,
            NotificationOutboxEvent.notification_id == notification_id,
            NotificationOutboxEvent.status == "processing",
            NotificationOutboxEvent.claim_token == claim_token,
        )
        .with_for_update()
    )
    if notification is None or event is None:
        await db.rollback()
        return "canceled"

    async def finish(outcome: str, *, until: datetime | None = None) -> str:
        event.locked_until = event.claim_token = None
        if until is not None:
            event.status, event.available_at = "pending", until
            event.attempt_count = max(0, event.attempt_count - 1)
        elif outcome == "canceled":
            event.status = notification.dispatch_status = "canceled"
        else:
            event.status, event.delivered_at = "delivered", now
        await db.commit()
        return outcome

    meta = notification.meta or {}
    try:
        since, through = (datetime.fromisoformat(meta[key]) for key in ("since", "through"))
        valid = since.tzinfo is not None and through.tzinfo is not None and since < through
    except (KeyError, TypeError, ValueError):
        valid = False
    if (
        not valid
        or notification.dispatch_status != "pending"
        or notification.idempotency_key != f"chat-email:{meta.get('member_id')}:{through.isoformat()}"
    ):
        return await finish("canceled")

    # Match room attachment authorization's owner -> room -> member lock order.
    owner_ids = (
        await db.execute(
            select(ChatRoom.owner_entity_id, ChatRoom.owner_user_id).where(ChatRoom.id == meta.get("room_id"))
        )
    ).one_or_none()
    if owner_ids is None:
        return await finish("canceled")
    entity_id, owner_id = owner_ids
    if entity_id:
        owner = await db.scalar(
            select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None)).with_for_update(read=True)
        )
    else:
        owner = await db.scalar(
            select(User)
            .where(User.id == owner_id, User.status == "active", User.deleted_at.is_(None))
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    room = await db.scalar(
        select(ChatRoom)
        .where(ChatRoom.id == meta.get("room_id"), ChatRoom.status == "active")
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    member = await db.scalar(
        select(ChatRoomMember)
        .where(
            ChatRoomMember.id == meta.get("member_id"),
            ChatRoomMember.room_id == meta.get("room_id"),
            ChatRoomMember.user_id == notification.user_id,
            ChatRoomMember.status == "active",
            ChatRoomMember.notifications_muted.is_(False),
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    user = await db.scalar(
        select(User)
        .where(User.id == notification.user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        owner is None
        or room is None
        or member is None
        or user is None
        or not email_is_verified(user)
        or (room.owner_entity_id, room.owner_user_id) != tuple(owner_ids)
    ):
        return await finish("canceled")
    prefs, enabled, delay = _preferences(user)
    if not enabled:
        return await finish("canceled")
    query = _messages(member).where(ChatRoomMessage.created_at > since, ChatRoomMessage.created_at <= through)
    count = await db.scalar(query.with_only_columns(func.count()).select_from(ChatRoomMessage))
    if not count:
        return await finish("canceled")
    due_at = through + timedelta(minutes=delay)
    if due_at > now:
        return await finish("deferred", until=due_at)
    if _is_in_quiet_hours(prefs, now=now) or not email_service.is_email_delivery_enabled():
        return await finish("deferred", until=now + timedelta(minutes=1))

    messages = list(
        (
            await db.scalars(
                query.order_by(ChatRoomMessage.created_at.desc(), ChatRoomMessage.id.desc())
                .limit(3)
                .with_for_update(read=True)
            )
        ).all()
    )
    try:
        zone = ZoneInfo(user.timezone or "UTC")
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    summaries = []
    for message in reversed(messages):
        name = (
            await db.scalar(select(User.display_name).where(User.id == message.sender_user_id))
            if message.sender_user_id
            else message.sender_agent_name
        )
        files = list(
            (
                await db.scalars(
                    select(ChatRoomFile.name)
                    .where(ChatRoomFile.message_id == message.id, ChatRoomFile.deleted_at.is_(None))
                    .order_by(ChatRoomFile.id)
                    .limit(10)
                    .with_for_update(read=True)
                )
            ).all()
        )
        summaries.append(
            {
                "sender": name or "Member",
                "time": message.created_at.astimezone(zone).strftime("%b %d, %H:%M %Z"),
                "text": message.content[:500] + ("…" if len(message.content) > 500 else ""),
                "files": files,
            }
        )
    title = room.title
    if not title and room.kind == "dm":
        title = await db.scalar(
            select(User.display_name)
            .join(ChatRoomMember, ChatRoomMember.user_id == User.id)
            .where(ChatRoomMember.room_id == room.id, ChatRoomMember.user_id != user.id)
            .limit(1)
        )
    subject, html, text = render_digest(
        title=title or "Chat", room_id=room.id, count=count, messages=summaries, locale=user.locale
    )
    if not await email_service.send_email(user.email, subject, html, text):
        raise RuntimeError("Chat email provider did not accept the message")
    return await finish("dispatched")
