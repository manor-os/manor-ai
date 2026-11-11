"""Core workspace_chat operations — conversation lifecycle + message CRUD.

Conversation model:
  * One ``scope='workspace_main'`` per workspace (lazy-created on first
    post). All workspace-level chatter lives here by default.
  * Per-thread ``scope='workspace_thread'`` rows are spawned by
    long-running plans / goals to keep the main feed scannable.
    Identified by (thread_ref_kind, thread_ref_id).

Both share the same ``messages`` table — the router walks
``conversation_id`` to fetch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, desc, event, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import (
    MESSAGE_OPEN_PENDING_ACTION_CONDITION, Conversation, Message, Task,
)

logger = logging.getLogger(__name__)

_CHAT_EVENT_QUEUE_KEY = "workspace_chat_events_after_commit"
_CHAT_EVENT_LISTENER_KEY = "workspace_chat_event_listener"
_CHAT_EVENT_PUBLISH_TASKS: set[asyncio.Task[None]] = set()


_AFTER_COMMIT_EVENTS = "workspace_chat_after_commit_events"


def _publish_workspace_chat_event_after_commit(db: AsyncSession, entity_id: str, data: dict) -> None:
    """Queue a realtime event that is sent only if the transaction commits."""
    import asyncio

    from sqlalchemy import event as sa_event

    session = db.sync_session
    queued = session.info.setdefault(_AFTER_COMMIT_EVENTS, [])
    if not queued:
        def publish(sync_session) -> None:
            if sync_session.in_nested_transaction():
                return
            for loop, owner, payload in sync_session.info.pop(_AFTER_COMMIT_EVENTS, []):
                if not loop.is_closed():
                    loop.create_task(_publish_workspace_chat_event(owner, payload))

        def discard(sync_session) -> None:
            if not sync_session.in_nested_transaction():
                sync_session.info.pop(_AFTER_COMMIT_EVENTS, None)

        sa_event.listen(session, "after_commit", publish, once=True)
        sa_event.listen(session, "after_rollback", discard, once=True)
    queued.append((asyncio.get_running_loop(), entity_id, data))


async def _publish_workspace_chat_event(entity_id: str, data: dict) -> None:
    """Best-effort realtime fanout; the durable Message remains authoritative."""
    try:
        from packages.core.cache import _get_redis

        workspace_id = str(data.get("workspace_id") or "").strip()
        if not workspace_id:
            logger.warning("workspace chat realtime publish missing workspace_id")
            return
        r = await _get_redis()
        if not r:
            return
        await r.publish(
            "manor:ws_broadcast",
            json.dumps({
                # The relay authorizes top-level targets. Keeping the workspace
                # only inside ``data`` would fall back to an entity-wide push.
                "target": "workspace",
                "entity_id": entity_id,
                "workspace_id": workspace_id,
                "event": "workspace_chat_message",
                "data": data,
            }, ensure_ascii=False, default=str),
        )
    except Exception:
        logger.debug("workspace chat realtime publish skipped", exc_info=True)


async def publish_workspace_chat_message_event(
    entity_id: str,
    *,
    workspace_id: str,
    message: Message,
) -> None:
    """Publish a persisted message event after its transaction commits.

    Callers that own a transaction can opt out of ``post_message``'s
    immediate best-effort fanout, commit, and then call this helper. This
    prevents a realtime refetch from racing the message transaction.
    """
    await _publish_workspace_chat_event(
        entity_id,
        _workspace_chat_message_event_data(
            workspace_id=workspace_id,
            message=message,
        ),
    )


def _workspace_chat_message_event_data(
    *,
    workspace_id: str,
    message: Message,
) -> dict:
    action = (
        message.pending_action
        if isinstance(message.pending_action, dict) and message.pending_action.get("kind")
        else None
    )
    meta = message.meta if isinstance(message.meta, dict) else {}
    event_data = {
        "workspace_id": workspace_id,
        "message_id": message.id,
        "message_kind": message.message_kind,
        "author_kind": message.author_kind,
        "has_pending_action": action is not None,
        "action_kind": (action or {}).get("kind"),
    }
    conversation_id = str(getattr(message, "conversation_id", None) or "").strip()
    if conversation_id:
        event_data["conversation_id"] = conversation_id
    if meta.get("simulation_runtime") is True:
        # The person driving a simulation is looking at it; no toast.
        event_data["simulation"] = True
    if meta.get("agent_greeting") is True:
        event_data.update({
            "agent_greeting": True,
            "agent_greeting_sequence": meta.get("agent_greeting_sequence"),
            "agent_greeting_total": meta.get("agent_greeting_total"),
        })
    strategist_activity = meta.get("strategist_activity")
    if isinstance(strategist_activity, dict):
        event_data["strategist_activity"] = {
            "stage": strategist_activity.get("stage"),
            "state": strategist_activity.get("state"),
            "review_id": strategist_activity.get("review_id"),
        }
    return event_data


def _schedule_workspace_chat_event(
    loop: asyncio.AbstractEventLoop,
    *,
    entity_id: str,
    event_data: dict,
) -> None:
    if loop.is_closed():
        return

    def schedule() -> None:
        publish = loop.create_task(
            _publish_workspace_chat_event(entity_id, dict(event_data))
        )
        _CHAT_EVENT_PUBLISH_TASKS.add(publish)

        def finished(done: asyncio.Task[None]) -> None:
            _CHAT_EVENT_PUBLISH_TASKS.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug(
                    "workspace chat post-commit realtime publish skipped",
                    exc_info=True,
                )

        publish.add_done_callback(finished)

    try:
        if asyncio.get_running_loop() is loop:
            schedule()
            return
    except RuntimeError:
        pass
    if loop.is_running():
        loop.call_soon_threadsafe(schedule)


def _chat_event_transaction_descends_from(transaction, ancestor) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _publish_workspace_chat_events_after_commit(session) -> None:
    if session.in_nested_transaction():
        return
    queued = session.info.pop(_CHAT_EVENT_QUEUE_KEY, [])
    for loop, entity_id, event_data, _owner in queued:
        _schedule_workspace_chat_event(
            loop,
            entity_id=entity_id,
            event_data=event_data,
        )


def _clear_workspace_chat_events_after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_CHAT_EVENT_QUEUE_KEY, None)


def _clear_workspace_chat_events_after_soft_rollback(
    session,
    previous_transaction,
) -> None:
    if not previous_transaction.nested:
        return
    queued = session.info.get(_CHAT_EVENT_QUEUE_KEY, [])
    session.info[_CHAT_EVENT_QUEUE_KEY] = [
        item
        for item in queued
        if not _chat_event_transaction_descends_from(item[3], previous_transaction)
    ]


def queue_workspace_chat_message_event_after_commit(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    message: Message,
) -> None:
    """Fan out a durable Message only after its transaction commits."""

    sync_session = db.sync_session
    owner = sync_session.get_nested_transaction()
    sync_session.info.setdefault(_CHAT_EVENT_QUEUE_KEY, []).append((
        asyncio.get_running_loop(),
        entity_id,
        _workspace_chat_message_event_data(
            workspace_id=workspace_id,
            message=message,
        ),
        owner,
    ))
    if sync_session.info.get(_CHAT_EVENT_LISTENER_KEY):
        return
    event.listen(
        sync_session,
        "after_commit",
        _publish_workspace_chat_events_after_commit,
    )
    event.listen(
        sync_session,
        "after_rollback",
        _clear_workspace_chat_events_after_rollback,
    )
    event.listen(
        sync_session,
        "after_soft_rollback",
        _clear_workspace_chat_events_after_soft_rollback,
    )
    sync_session.info[_CHAT_EVENT_LISTENER_KEY] = True


# ── Conversation lifecycle ────────────────────────────────────────────

async def ensure_main_conversation(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
) -> Conversation:
    """Get or create the workspace_main conversation. Caller commits."""
    existing = (await db.execute(
        select(Conversation).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Conversation.scope == "workspace_main",
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return existing

    conv = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Workspace",
        channel="workspace",
        scope="workspace_main",
    )
    db.add(conv)
    await db.flush()
    return conv


async def spawn_thread(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str],
    thread_ref_kind: str,
    thread_ref_id: str,
    title: Optional[str] = None,
) -> Conversation:
    """Idempotent: re-call returns the existing thread for the same ref.

    ``workspace_id`` is None only for the thread of a Task assigned directly
    to an Agent, outside any Workspace.
    """
    if thread_ref_kind not in {"task", "plan", "goal"}:
        raise ValueError(f"unsupported thread_ref_kind={thread_ref_kind!r}")
    if workspace_id is None and thread_ref_kind != "task":
        raise ValueError("only a Task thread may exist outside a Workspace")

    def in_scope(column):
        return column.is_(None) if workspace_id is None else column == workspace_id

    # A Task is the durable authority for its thread. Lock it before the
    # check/create sequence so simultaneous first turns cannot create two
    # Conversations for the same Task.
    if thread_ref_kind == "task":
        task_id = (await db.execute(
            select(Task.id).where(
                Task.id == thread_ref_id,
                Task.entity_id == entity_id,
                in_scope(Task.workspace_id),
            ).with_for_update()
        )).scalar_one_or_none()
        if not task_id:
            raise LookupError("Task not found")

    existing = (await db.execute(
        select(Conversation).where(
            Conversation.entity_id == entity_id,
            in_scope(Conversation.workspace_id),
            Conversation.scope == "workspace_thread",
            Conversation.thread_ref_kind == thread_ref_kind,
            Conversation.thread_ref_id == thread_ref_id,
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        conv = existing
    else:
        conv = Conversation(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            title=title or f"{thread_ref_kind} {thread_ref_id[:8]}",
            channel="workspace",
            scope="workspace_thread",
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
        )
        db.add(conv)
        await db.flush()

    if thread_ref_kind == "task":
        from packages.core.services.task_session import bind_task_session_conversation

        await bind_task_session_conversation(db, conv)
    return conv


# ── Posting ───────────────────────────────────────────────────────────

async def post_message(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    body: str,
    message_kind: str = "text",
    author_kind: str = "user",
    author_user_id: Optional[str] = None,
    author_subscription_id: Optional[str] = None,
    refs: Optional[list[dict]] = None,
    attachments: Optional[dict] = None,
    pending_action: Optional[dict] = None,
    meta: Optional[dict] = None,
    thread_ref_kind: Optional[str] = None,
    thread_ref_id: Optional[str] = None,
    publish_event: bool = True,
) -> Message:
    """Post into the right conversation (thread or main). Caller commits.

    The realtime event is sent after that commit; ``publish_event=False``
    leaves it to the caller.
    """
    if thread_ref_kind and thread_ref_id:
        conv = await spawn_thread(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
        )
    else:
        conv = await ensure_main_conversation(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )

    # ``role`` is the LLM-protocol field — we mirror author_kind into it
    # so existing chat code that filters by role still works:
    #   user   → 'user'
    #   agent  → 'assistant'
    #   system → 'system'
    role_map = {"user": "user", "agent": "assistant", "system": "system"}
    role = role_map.get(author_kind, "user")

    # Message has no author_user_id column — the posting user is
    # captured in the conversation row when needed; here we stash it
    # in meta for traceability without requiring a schema change.
    meta = dict(meta or {})
    if author_user_id:
        meta["author_user_id"] = author_user_id

    action = pending_action if isinstance(pending_action, dict) and pending_action.get("kind") else None
    if action is not None:
        from packages.core.services.decision_recommendation import ensure_recommendation

        action = ensure_recommendation(action)

    msg = Message(
        id=generate_ulid(),
        conversation_id=conv.id,
        role=role,
        content=body,
        meta=meta,
        author_kind=author_kind,
        author_subscription_id=author_subscription_id,
        message_kind=message_kind,
        refs=refs,
        attachments=attachments,
        pending_action=action,
    )
    db.add(msg)
    await db.flush()

    if publish_event:
        # Members refetch on the event, so it goes out once the message is readable.
        queue_workspace_chat_message_event_after_commit(
            db, entity_id=entity_id, workspace_id=workspace_id, message=msg,
        )

    return msg


async def post_workspace_lifecycle_activity(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    body: str,
    action: str,
    phase: str,
    transition_id: str | None = None,
) -> Message:
    """Create a durable Workspace start/pause activity row.

    The caller commits and publishes the message after the surrounding
    lifecycle operation succeeds. The transition id is metadata only; it lets
    clients correlate a server row with an optimistic local animation.
    """
    meta = {
        "workspace_lifecycle": True,
        "workspace_lifecycle_phase": phase,
        "workspace_lifecycle_action": action,
    }
    if transition_id:
        meta["workspace_lifecycle_transition_id"] = transition_id
    return await post_message(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        body=body,
        message_kind="strategist_activity",
        author_kind="system",
        refs=[{"type": "workspace", "id": workspace_id}],
        meta=meta,
        publish_event=False,
    )


# ── Reads ─────────────────────────────────────────────────────────────

def open_pending_action_filters(entity_id: str, workspace_id: str) -> tuple:
    """Unresolved message cards for the existing chat loading behavior."""
    return (
        Conversation.entity_id == entity_id,
        Conversation.workspace_id == workspace_id,
        text(MESSAGE_OPEN_PENDING_ACTION_CONDITION),
    )


async def unresolved_pending_messages(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    limit: int = 50,
) -> list[Message]:
    """Every unresolved action card in the workspace, any conversation.

    Background plans post their approval/input cards into their own thread
    conversation, so the main chat's own message window can miss them entirely
    while the sidebar badge still counts them. Callers merge these in so the
    badge can never point at work the chat refuses to show.
    """
    return list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*open_pending_action_filters(entity_id, workspace_id))
        .order_by(desc(Message.created_at))
        .limit(limit)
    )).scalars().all())


async def count_open_pending_actions(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
) -> int:
    """How many action cards are still waiting — authoritative, from the DB.

    The chat banner used to count the cards it happened to be holding in
    memory, which only ever drifts upward: ``unresolved_pending_messages``
    pins cards from far outside the page window, and the moment the user
    answers one it leaves the pinned set instead of coming back marked
    resolved. The client merges pages by id and never removes, so the stale
    "still waiting" copy sits there until a reload. Meanwhile the sidebar
    badge re-counted from the DB and dropped — one answer, two numbers.
    """
    from .pending_actions import count_pending_actions_by_workspace, pending_action_total

    counts = await count_pending_actions_by_workspace(
        db, entity_id=entity_id, workspace_ids=[workspace_id],
    )
    return pending_action_total(counts[workspace_id])


async def list_messages(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    thread_ref_kind: Optional[str] = None,
    thread_ref_id: Optional[str] = None,
    limit: int = 100,
    before: Optional[datetime] = None,
    before_id: Optional[str] = None,
    pin_pending: bool = True,
) -> list[Message]:
    """List messages newest-first. ``before`` cursor for pagination.

    ``pin_pending`` merges unresolved action cards from other conversations
    into the first page of the main view. Paginating callers pass False and
    merge them AFTER their own windowing — otherwise the merged rows sit past
    the page limit and get truncated away (which silently hid days of blocked
    work behind a non-zero badge).
    """
    main_workspace_view = not (thread_ref_kind and thread_ref_id)
    if thread_ref_kind and thread_ref_id:
        conv = (await db.execute(
            select(Conversation).where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
                Conversation.scope == "workspace_thread",
                Conversation.thread_ref_kind == thread_ref_kind,
                Conversation.thread_ref_id == thread_ref_id,
            ).limit(1)
        )).scalar_one_or_none()
    else:
        conv = (await db.execute(
            select(Conversation).where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
                Conversation.scope == "workspace_main",
            ).limit(1)
        )).scalar_one_or_none()

    if conv is None:
        return []

    stmt = select(Message).where(Message.conversation_id == conv.id)
    if before is not None:
        if before_id:
            stmt = stmt.where(
                or_(
                    Message.created_at < before,
                    and_(Message.created_at == before, Message.id < before_id),
                )
            )
        else:
            stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(desc(Message.created_at), desc(Message.id)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())

    # The chat sidebar badge counts unresolved proposal/HITL cards. If those
    # cards are older than the normal message window, pin them into the initial
    # workspace chat payload so the badge never points at invisible work.
    if pin_pending and main_workspace_view and before is None:
        pending_rows = await unresolved_pending_messages(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )
        by_id = {m.id: m for m in rows}
        for msg in pending_rows:
            by_id.setdefault(msg.id, msg)
        # Keep the newest-first contract the callers slice and reverse on.
        rows = sorted(
            by_id.values(),
            key=lambda m: (m.created_at, m.id),
            reverse=True,
        )

    return rows


# ── Interactive resolution ────────────────────────────────────────────

async def resolve_pending_action(
    db: AsyncSession,
    *,
    message_id: str,
    user_id: str,
    resolution: dict,
    publish_resolution: bool = True,
) -> Optional[Message]:
    """User clicked a button on an interactive message. Records the
    resolution on that message; the card itself shows the choice.

    No separate receipt message is written: the answered card collapses into
    one "you chose" line, and any outcome the caller records in ``resolution``
    (for example ``delivery_outcome``) is shown on that same line.

    Returns the original message. Caller commits."""
    msg = (await db.execute(
        select(Message).where(Message.id == message_id)
    )).scalar_one_or_none()
    if msg is None:
        return None
    if not (isinstance(msg.pending_action, dict) and msg.pending_action.get("kind")):
        return msg
    if msg.resolved_at is not None:
        return msg

    msg.resolution = resolution
    msg.resolved_at = datetime.now(timezone.utc)
    msg.resolved_by_user_id = user_id

    if publish_resolution:
        await _publish_pending_action_resolution(db, message=msg)

    return msg


async def _publish_pending_action_resolution(
    db: AsyncSession,
    *,
    message: Message,
) -> None:
    """Tell other open clients that one card was answered.

    Shared workspaces need a cross-tab signal: clients that loaded the card
    close it with the recorded choice, whichever surface answered it, and
    refresh their sidebar counts. Callers that publish their own event pass
    ``publish_resolution=False``."""

    msg = message
    try:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == msg.conversation_id)
        )).scalar_one_or_none()
        if conv and conv.workspace_id:
            entity_id = conv.entity_id
            workspace_id = conv.workspace_id
            # Only a committed decision closes the card on other clients: a
            # resolve that fails after this point (the Task stopped waiting)
            # rolls back, and the card must stay open with its options.
            _publish_workspace_chat_event_after_commit(db, entity_id, {
                "workspace_id": workspace_id,
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "resolved_message_id": msg.id,
                "resolved_at": msg.resolved_at.isoformat() if msg.resolved_at else None,
                "resolution": msg.resolution,
                "message_kind": msg.message_kind,
                "author_kind": msg.author_kind,
                "has_pending_action": False,
                "action_resolved": True,
            })
    except Exception:
        pass  # WS push is best-effort
