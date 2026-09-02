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

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message, Task

logger = logging.getLogger(__name__)


async def _publish_workspace_chat_event(entity_id: str, data: dict) -> None:
    """Best-effort realtime fanout for workspace chat events.

    Keep this awaited inside the caller's current event loop. Detached tasks in
    Celery workers can outlive the loop that created them and produce
    "Future attached to a different loop" errors while the DB transaction has
    already succeeded.
    """
    try:
        from packages.core.cache import _get_redis

        r = await _get_redis()
        if not r:
            return
        await r.publish(
            "manor:ws_broadcast",
            json.dumps({
                "entity_id": entity_id,
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
    await _publish_workspace_chat_event(entity_id, event_data)


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
    workspace_id: str,
    thread_ref_kind: str,
    thread_ref_id: str,
    title: Optional[str] = None,
) -> Conversation:
    """Idempotent: re-call returns the existing thread for the same ref."""
    if thread_ref_kind not in {"task", "plan", "goal"}:
        raise ValueError(f"unsupported thread_ref_kind={thread_ref_kind!r}")

    # A Task is the durable authority for its thread. Lock it before the
    # check/create sequence so simultaneous first turns cannot create two
    # Conversations for the same Task.
    if thread_ref_kind == "task":
        task_id = (await db.execute(
            select(Task.id).where(
                Task.id == thread_ref_id,
                Task.entity_id == entity_id,
                Task.workspace_id == workspace_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if not task_id:
            raise LookupError("Task not found")

    existing = (await db.execute(
        select(Conversation).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
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
    """Post into the right conversation (thread or main). Caller commits."""
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
        await publish_workspace_chat_message_event(
            entity_id,
            workspace_id=workspace_id,
            message=msg,
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
    """The single definition of "an action card still waiting on a human".

    Every surface that shows the user a number must count through this, or the
    numbers drift apart and the user has to guess which one is lying.
    """
    return (
        Conversation.entity_id == entity_id,
        Conversation.workspace_id == workspace_id,
        Message.pending_action.isnot(None),
        Message.pending_action["kind"].as_string().isnot(None),
        Message.resolved_at.is_(None),
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
    return int((await db.execute(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*open_pending_action_filters(entity_id, workspace_id))
    )).scalar() or 0)


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
    emit_followup: bool = True,
) -> Optional[Message]:
    """User clicked a button on an interactive message. Records the
    resolution + emits a follow-up agent reply summarising the choice.

    Returns the original message; the follow-up reply is in the same
    conversation. Caller commits."""
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

    # Workspace Chat keeps a standalone resolution receipt for continuity.
    # Personal Chat approval handlers already persist the user's choice and an
    # assistant continuation message, while the original card also gains its
    # resolved badge. Emitting this receipt there would show the same outcome
    # twice, so those callers explicitly disable it.
    choice = resolution.get("choice") or ""
    note = resolution.get("note") or ""
    is_retry = choice in ("retry", "retry_now")
    is_feedback = choice == "feedback"
    is_response = choice in ("respond", "provide_answers", "submit", "ok")
    is_workflow_start = (
        msg.pending_action.get("kind") == PendingActionKind.WORKFLOW_STARTER_INPUT
        and choice in ("run", "start", "confirm")
    )
    is_cancelled = "cancel" in choice or choice in ("skip", "stopped")
    is_approve = (
        "approve" in choice
        or choice in ("yes", "accept", "confirm", "continue_after_login")
    )
    follow: Message | None = None
    if emit_followup:
        if is_retry:
            content = "Retry requested"
        elif is_feedback:
            content = "✓ Feedback sent"
        elif is_response:
            content = "✓ Response submitted"
        elif is_workflow_start:
            content = "✓ Workflow started"
        elif is_cancelled:
            content = "✗ Cancelled"
        else:
            label = "Approved" if is_approve else "Rejected" if choice else "Resolved"
            content = f"✓ {label}" if is_approve else f"✗ {label}"
        if note:
            content += f" — {note}"
        follow = Message(
            id=generate_ulid(),
            conversation_id=msg.conversation_id,
            role="system",
            content=content,
            author_kind="system",
            message_kind="system",
            refs=[{"type": "message", "id": msg.id}],
        )
        db.add(follow)
        await db.flush()

    # Let other open clients refresh their sidebar counts. The API caller also
    # updates optimistically, but shared workspaces need a cross-tab signal.
    try:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == msg.conversation_id)
        )).scalar_one_or_none()
        if conv and conv.workspace_id and follow is not None:
            entity_id = conv.entity_id
            workspace_id = conv.workspace_id
            await _publish_workspace_chat_event(entity_id, {
                "workspace_id": workspace_id,
                "message_id": follow.id,
                "resolved_message_id": msg.id,
                "message_kind": "system",
                "author_kind": "system",
                "has_pending_action": False,
                "action_resolved": True,
            })
    except Exception:
        pass  # WS push is best-effort

    return msg
