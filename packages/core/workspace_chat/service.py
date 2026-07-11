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

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message

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
        return existing

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

    await _publish_workspace_chat_event(entity_id, {
        "workspace_id": workspace_id,
        "message_id": msg.id,
        "message_kind": message_kind,
        "author_kind": author_kind,
        "has_pending_action": action is not None,
        "action_kind": (action or {}).get("kind"),
    })

    return msg


# ── Reads ─────────────────────────────────────────────────────────────

async def list_messages(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    thread_ref_kind: Optional[str] = None,
    thread_ref_id: Optional[str] = None,
    limit: int = 100,
    before: Optional[datetime] = None,
) -> list[Message]:
    """List messages newest-first. ``before`` cursor for pagination."""
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
        stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(desc(Message.created_at)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())

    # The chat sidebar badge counts unresolved proposal/HITL cards. If those
    # cards are older than the normal message window, pin them into the initial
    # workspace chat payload so the badge never points at invisible work.
    if main_workspace_view and before is None:
        pending_rows = list((await db.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
                Message.pending_action.isnot(None),
                Message.pending_action["kind"].as_string().isnot(None),
                Message.resolved_at.is_(None),
            )
            .order_by(desc(Message.created_at))
            .limit(50)
        )).scalars().all())
        by_id = {m.id: m for m in rows}
        for msg in pending_rows:
            by_id.setdefault(msg.id, msg)
        rows = list(by_id.values())

    return rows


# ── Interactive resolution ────────────────────────────────────────────

async def resolve_pending_action(
    db: AsyncSession,
    *,
    message_id: str,
    user_id: str,
    resolution: dict,
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

    # Mirror the user choice as a system message for continuity.
    choice = resolution.get("choice") or ""
    note = resolution.get("note") or ""
    is_retry = choice in ("retry", "retry_now")
    is_feedback = choice == "feedback"
    is_response = choice in ("respond", "provide_answers", "submit", "ok")
    is_cancelled = "cancel" in choice or choice in ("skip", "stopped")
    is_approve = (
        "approve" in choice
        or choice in ("yes", "accept", "confirm", "continue_after_login")
    )
    if is_retry:
        content = "Retry requested"
    elif is_feedback:
        content = "✓ Feedback sent"
    elif is_response:
        content = "✓ Response submitted"
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
        if conv and conv.workspace_id:
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
