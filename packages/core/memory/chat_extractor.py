"""Chat → operator memory extractor.

Periodically reads recent workspace chat messages authored by the
operator and asks an LLM to pull out anything worth remembering for
future Strategist runs:

  * preferences  ("I don't want to be asked for approval at night")
  * guidance     ("always cite sources in tutorial threads")
  * facts        ("our target audience is solo founders")
  * decisions    ("we picked Stripe over Paddle because...")

The extractor shares durable evidence receipts and candidate admission with
context compaction. Confidence is capped at 0.8; proposals become active only
through the existing learning review/application flow. Original message IDs
remain attached to the evidence and candidate.

Public entry: ``extract_chat_insights(workspace_id, db)``. Called by
``scheduler.tick`` via ``execution_type='chat_insight_extraction'``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Workspace, WorkspaceActivity
from packages.core.services.workspace_autonomy import workspace_allows_manual_work

logger = logging.getLogger(__name__)


# How far back we look on each pass. Pairs with the schedule (default
# every 6h) — overlap is fine because we de-dupe by last-extract bookmark.
LOOKBACK_HOURS = 12

# Cap how many messages we send to the LLM in one extraction pass so
# the prompt stays bounded.
MAX_MESSAGES_PER_PASS = 80

# Workspace.settings key for the extractor bookmark — we only re-process
# messages newer than this on each run.
LAST_EXTRACT_KEY = "chat_extract_bookmark"
LAST_EXTRACT_ID_KEY = "chat_extract_bookmark_id"

# Ignore very short messages — "ok" / "👍" / "yes" rarely contain
# anything worth remembering and they balloon the prompt.
MIN_MESSAGE_CHARS = 12


# ── Public ────────────────────────────────────────────────────────────

async def extract_chat_insights(
    db: AsyncSession, workspace_id: str, *, now: Optional[datetime] = None,
    require_autonomous_runtime: bool = True,
) -> dict[str, Any]:
    """Schedule reviewable candidates through the same admission as compact.

    Candidate/receipt transactions commit independently. The caller owns the
    scan cursor; a failed pass can replay safely without repeating admitted work.
    """
    now = now or datetime.now(timezone.utc)
    workspace = (await db.execute(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if workspace is None:
        return {"workspace_id": workspace_id, "skipped": True, "reason": "not_found"}
    if workspace.status != "active" and (
        require_autonomous_runtime or not workspace_allows_manual_work(workspace)
    ):
        return {"workspace_id": workspace_id, "skipped": True, "reason": f"workspace_{workspace.status}"}
    bookmark = _read_bookmark(workspace, default=now - timedelta(hours=LOOKBACK_HOURS))
    messages = await _fetch_operator_messages(
        db, workspace.entity_id, workspace_id, since=bookmark,
        after_id=(workspace.settings or {}).get(LAST_EXTRACT_ID_KEY), limit=MAX_MESSAGES_PER_PASS,
    )
    result = await _extract_messages(
        db, workspace.entity_id, messages, require_autonomous_runtime=require_autonomous_runtime,
    )
    if messages and not result["pending"] and not result.get("skipped"):
        await _write_bookmark(db, workspace, messages[-1].created_at, messages[-1].id)
    if result["extracted"] or result["skipped_invalid"]:
        await _log_activity(db, workspace=workspace, messages_seen=len(messages),
                            extracted=result["extracted"], skipped_invalid=result["skipped_invalid"])
    await db.flush()
    return {"workspace_id": workspace_id, **result}


async def extract_entity_chat_insights(
    db: AsyncSession, entity_id: str, *, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Scan personal conversations; never make an extracted memory active."""
    from packages.core.models.user import Entity

    now = now or datetime.now(timezone.utc)
    entity = await db.get(Entity, entity_id)
    if entity is None:
        return {"entity_id": entity_id, "skipped": True, "reason": "not_found"}
    settings = dict(entity.settings or {})
    bookmark = _parse_bookmark(settings.get(LAST_EXTRACT_KEY), default=now - timedelta(hours=LOOKBACK_HOURS))
    messages = await _fetch_operator_messages(
        db, entity_id, None, since=bookmark, after_id=settings.get(LAST_EXTRACT_ID_KEY), limit=MAX_MESSAGES_PER_PASS,
    )
    result = await _extract_messages(db, entity_id, messages)
    if messages and not result["pending"] and not result.get("skipped"):
        # Merge into current settings after the worker, preserving concurrent
        # entity configuration changes. The cursor is the last processed tuple.
        entity = await db.scalar(select(Entity).where(Entity.id == entity_id)
                                 .with_for_update().execution_options(populate_existing=True))
        settings = dict(entity.settings or {})
        settings.update({LAST_EXTRACT_KEY: messages[-1].created_at.isoformat(), LAST_EXTRACT_ID_KEY: messages[-1].id})
        entity.settings = settings
    await db.flush()
    return {"entity_id": entity_id, **result}


async def _extract_messages(db, entity_id, messages, *, require_autonomous_runtime=False):
    from types import SimpleNamespace
    from packages.core.memory.compaction import MemoryScopeDenied, prepare_chat_memory

    result = {"messages": len(messages), "extracted": 0, "skipped_invalid": 0, "pending": 0}
    conversations = {c.id: c for c in (await db.scalars(select(Conversation).where(
        Conversation.entity_id == entity_id, Conversation.id.in_({m.conversation_id for m in messages}),
    ))).all()}
    groups = {}
    for message in messages:
        conversation = conversations.get(message.conversation_id)
        actor = (message.meta or {}).get("author_user_id") or (conversation.user_id if conversation else None)
        if not conversation or not isinstance(actor, str) or not actor:
            result["skipped_invalid"] += 1
            continue
        groups.setdefault((conversation.id, actor), []).append(message)
    for (conversation_id, actor), batch in groups.items():
        conversation = conversations[conversation_id]
        ctx = SimpleNamespace(entity_id=entity_id, workspace_id=conversation.workspace_id,
                              conversation_id=conversation.id, user_id=actor, agent_id=conversation.agent_id)
        try:
            admitted = await prepare_chat_memory(
                ctx, [], source="chat_extract", source_message_ids=[m.id for m in batch],
                require_autonomous_runtime=require_autonomous_runtime,
            )
        except MemoryScopeDenied:
            # Revoked/deleted actors cannot submit new memories. Other actors in
            # a scheduled sweep can still proceed; model/storage failures retry.
            result["skipped_invalid"] += len(batch)
            continue
        result["extracted"] += admitted["extracted"]
        result["pending"] += admitted["pending"]
        if admitted.get("skipped"):
            result["skipped"] = True
    return result


# ── Internals ─────────────────────────────────────────────────────────

def _parse_bookmark(raw: Any, *, default: datetime) -> datetime:
    if not raw:
        return default
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return default


def _read_bookmark(workspace: Workspace, *, default: datetime) -> datetime:
    return _parse_bookmark(
        (workspace.settings or {}).get(LAST_EXTRACT_KEY), default=default
    )


async def _write_bookmark(
    db: AsyncSession,
    workspace: Workspace,
    ts: datetime,
    message_id: str,
) -> None:
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )

    locked_workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
    )
    if locked_workspace is None:
        return
    settings = dict(locked_workspace.settings or {})
    settings[LAST_EXTRACT_KEY] = ts.isoformat()
    settings[LAST_EXTRACT_ID_KEY] = message_id
    locked_workspace.settings = settings


async def _fetch_operator_messages(
    db: AsyncSession, entity_id: str, workspace_id: str | None,
    *, since: datetime, limit: int, after_id: str | None = None,
) -> list[Message]:
    """Oldest eligible messages first, with a stable timestamp+ID cursor.

    Filter human text before LIMIT. An assistant row's legacy default
    author_kind=user must never become evidence, nor can generated cards.
    A cursor written by the old implementation has no ID: replay that timestamp
    once, letting receipts/dedup decide, rather than lose same-time messages.
    """
    after = or_(Message.created_at > since,
                and_(Message.created_at == since, Message.id > after_id)) if after_id else Message.created_at >= since
    return list((await db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
        Conversation.entity_id == entity_id, Conversation.workspace_id == workspace_id,
        Message.role == "user", Message.author_kind == "user", Message.message_kind == "text",
        func.length(func.btrim(Message.content)) >= MIN_MESSAGE_CHARS, after,
    ).order_by(Message.created_at, Message.id).limit(limit))).all())


async def _log_activity(
    db: AsyncSession, *, workspace: Workspace,
    messages_seen: int, extracted: int, skipped_invalid: int,
) -> None:
    summary = (
        f"Proposed {extracted} memory candidate(s) from {messages_seen} operator "
        f"message(s)"
    )
    if skipped_invalid:
        summary += f" ({skipped_invalid} invalid skipped)"

    db.add(WorkspaceActivity(
        id=generate_ulid(),
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
        event_type="chat_insight_extraction",
        summary=summary,
        details={
            "messages_seen": messages_seen,
            "extracted": extracted,
            "skipped_invalid": skipped_invalid,
        },
    ))
