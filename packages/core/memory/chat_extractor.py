"""Chat → operator memory extractor.

Periodically reads recent workspace chat messages authored by the
operator and asks an LLM to pull out anything worth remembering for
future Strategist runs:

  * preferences  ("I don't want to be asked for approval at night")
  * guidance     ("always cite sources in tutorial threads")
  * facts        ("our target audience is solo founders")
  * decisions    ("we picked Stripe over Paddle because...")

The extractor is conservative — it only writes a memory when the LLM
returns a confident extraction in the canonical schema. Confidence is
capped at 0.8 so an autonomously-extracted memory can never out-weigh
an operator's manual entry. Each entry's ``source`` is set to
``chat_extract:<message_id>`` so the operator can trace any spurious
write back to the chat that produced it.

Public entry: ``extract_chat_insights(workspace_id, db)``. Called by
``scheduler.tick`` via ``execution_type='chat_insight_extraction'``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_chat_insight_payload,
    runtime_execute_chat_insight_extraction_completion,
)
from packages.core.memory.service import record_memory
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Workspace, WorkspaceActivity

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

# Ignore very short messages — "ok" / "👍" / "yes" rarely contain
# anything worth remembering and they balloon the prompt.
MIN_MESSAGE_CHARS = 12


# ── Public ────────────────────────────────────────────────────────────

async def extract_chat_insights(
    db: AsyncSession,
    workspace_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One extraction pass for one workspace. Caller commits.

    Returns a stats dict suitable for the WorkspaceActivity log.
    """
    now = now or datetime.now(timezone.utc)

    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        return {"workspace_id": workspace_id, "skipped": True, "reason": "not_found"}
    if workspace.status != "active":
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "reason": f"workspace_{workspace.status}",
        }

    bookmark = _read_bookmark(workspace, default=now - timedelta(hours=LOOKBACK_HOURS))

    messages = await _fetch_operator_messages(
        db, workspace.entity_id, workspace_id,
        since=bookmark, limit=MAX_MESSAGES_PER_PASS,
    )
    if not messages:
        _write_bookmark(workspace, now)
        await db.flush()
        return {"workspace_id": workspace_id, "messages": 0, "extracted": 0}

    payload = runtime_chat_insight_payload(messages)
    extractions = await _call_llm(
        payload,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
    )

    written = 0
    skipped_invalid = 0
    for entry in extractions:
        if not _is_valid_entry(entry):
            skipped_invalid += 1
            continue
        await _persist_entry(
            db,
            workspace=workspace,
            entry=entry,
        )
        written += 1

    # Move the bookmark to the most recent processed message timestamp
    # (not ``now``) so we don't accidentally skip a message that arrived
    # mid-pass.
    last_ts = max(m.created_at for m in messages)
    _write_bookmark(workspace, last_ts)
    await db.flush()

    if written or skipped_invalid:
        await _log_activity(
            db, workspace=workspace,
            messages_seen=len(messages),
            extracted=written,
            skipped_invalid=skipped_invalid,
        )

    return {
        "workspace_id": workspace_id,
        "messages": len(messages),
        "extracted": written,
        "skipped_invalid": skipped_invalid,
    }


async def extract_entity_chat_insights(
    db: AsyncSession,
    entity_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One extraction pass over an entity's workspace-less chats. Caller commits.

    The workspace extractor never sees the main assistant chat
    (``Conversation.workspace_id IS NULL``), so preferences typed there
    never reached long-term memory. Insights are persisted as per-user
    agent memories (``agent_memories`` rows with ``workspace_id`` null),
    which the chat prompt reads back via ``get_context_memories``.
    """
    from packages.core.models.user import Entity
    from packages.core.services.memory_service import add_memory

    now = now or datetime.now(timezone.utc)

    entity = (await db.execute(
        select(Entity).where(Entity.id == entity_id)
    )).scalar_one_or_none()
    if entity is None:
        return {"entity_id": entity_id, "skipped": True, "reason": "not_found"}

    settings = dict(entity.settings or {})
    bookmark = _parse_bookmark(
        settings.get(LAST_EXTRACT_KEY),
        default=now - timedelta(hours=LOOKBACK_HOURS),
    )

    conv_rows = (await db.execute(
        select(Conversation.id, Conversation.user_id).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id.is_(None),
        )
    )).all()
    conv_user_by_id = {row[0]: row[1] for row in conv_rows}
    if not conv_user_by_id:
        return {"entity_id": entity_id, "messages": 0, "extracted": 0}

    rows = list((await db.execute(
        select(Message).where(
            Message.conversation_id.in_(list(conv_user_by_id)),
            Message.author_kind == "user",
            Message.created_at > bookmark,
        ).order_by(desc(Message.created_at)).limit(MAX_MESSAGES_PER_PASS)
    )).scalars().all())
    messages = [
        m for m in rows
        if m.content and len(m.content.strip()) >= MIN_MESSAGE_CHARS
    ]
    messages.sort(key=lambda m: m.created_at)

    def _save_bookmark(ts: datetime) -> None:
        settings[LAST_EXTRACT_KEY] = ts.isoformat()
        entity.settings = settings

    if not messages:
        _save_bookmark(now)
        await db.flush()
        return {"entity_id": entity_id, "messages": 0, "extracted": 0}

    user_by_message_id = {
        m.id: conv_user_by_id.get(m.conversation_id) for m in messages
    }
    fallback_user_id = next(
        (uid for uid in user_by_message_id.values() if uid), None
    )

    payload = runtime_chat_insight_payload(messages)
    extractions = await _call_llm(
        payload, entity_id=entity_id, workspace_id=None,
    )

    written = 0
    skipped_invalid = 0
    for entry in extractions:
        if not _is_valid_entry(entry):
            skipped_invalid += 1
            continue
        source_msg_id = str(entry.get("source_message_id") or "")
        user_id = user_by_message_id.get(source_msg_id) or fallback_user_id
        await add_memory(
            db,
            entity_id,
            f"{entry['title'].strip()}: {entry['body'].strip()}",
            memory_type=_ENTITY_MEMORY_TYPE_BY_SCOPE.get(entry["scope"], "fact"),
            user_id=user_id,
            importance=int(entry.get("importance") or 5),
            source=f"chat_extract:{source_msg_id or 'unknown'}",
            metadata={
                "confidence": float(entry["confidence"]),
                "tags": list(entry.get("tags") or []) + ["auto", "from_chat"],
            },
        )
        written += 1

    _save_bookmark(max(m.created_at for m in messages))
    await db.flush()

    return {
        "entity_id": entity_id,
        "messages": len(messages),
        "extracted": written,
        "skipped_invalid": skipped_invalid,
    }


# Map the extractor taxonomy onto the per-agent memory taxonomy
# ('fact' | 'preference' | 'context' | 'instruction').
_ENTITY_MEMORY_TYPE_BY_SCOPE = {
    "guidance": "instruction",
    "preference": "preference",
    "decision": "context",
    "fact": "fact",
}


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


def _write_bookmark(workspace: Workspace, ts: datetime) -> None:
    settings = dict(workspace.settings or {})
    settings[LAST_EXTRACT_KEY] = ts.isoformat()
    workspace.settings = settings


async def _fetch_operator_messages(
    db: AsyncSession, entity_id: str, workspace_id: str,
    *, since: datetime, limit: int,
) -> list[Message]:
    """Pull recent operator-authored chat messages.

    Operator = ``author_kind='user'`` AND non-trivial body. The LLM
    only judges what the operator said — agents' own chatter is noise
    for this purpose.
    """
    convs = list((await db.execute(
        select(Conversation.id).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
        )
    )).scalars().all())
    if not convs:
        return []

    rows = list((await db.execute(
        select(Message).where(
            Message.conversation_id.in_(convs),
            Message.author_kind == "user",
            Message.created_at > since,
        ).order_by(desc(Message.created_at)).limit(limit)
    )).scalars().all())

    rows = [
        m for m in rows
        if m.content and len(m.content.strip()) >= MIN_MESSAGE_CHARS
    ]
    rows.sort(key=lambda m: m.created_at)  # chronological for the LLM
    return rows


async def _call_llm(
    payload: str,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> list[dict[str, Any]]:
    """Single LLM call asking for the extracted entries.

    Returns ``[]`` on any failure — the extractor is best-effort and
    must never block the scheduler tick.
    """
    try:
        completion = await runtime_execute_chat_insight_extraction_completion(
            entity_id=entity_id,
            workspace_id=workspace_id,
            payload=payload,
        )
        text = completion.content.strip()
    except Exception as exc:
        logger.warning("chat extractor LLM call failed: %s", exc)
        return []

    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("chat extractor: bad JSON from LLM: %s", exc)
        return []

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return entries


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first = text.find("\n")
        if first != -1:
            text = text[first + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


_VALID_SCOPES = {"guidance", "decision", "fact", "preference"}


def _is_valid_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("scope") not in _VALID_SCOPES:
        return False
    title = (entry.get("title") or "").strip()
    body = (entry.get("body") or "").strip()
    if not title or not body:
        return False
    if len(title) > 200 or len(body) > 4000:
        return False
    confidence = entry.get("confidence")
    if not isinstance(confidence, (int, float)):
        return False
    if confidence < 0.3 or confidence > 0.8:
        return False
    return True


async def _persist_entry(
    db: AsyncSession, *, workspace: Workspace, entry: dict[str, Any],
) -> None:
    source_msg_id = entry.get("source_message_id") or "unknown"
    await record_memory(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        scope=entry["scope"],
        title=entry["title"].strip(),
        body=entry["body"].strip(),
        tags=list(entry.get("tags") or []) + ["auto", "from_chat"],
        source=f"chat_extract:{source_msg_id}",
        importance=int(entry.get("importance") or 5),
        confidence=float(entry["confidence"]),
    )


async def _log_activity(
    db: AsyncSession, *, workspace: Workspace,
    messages_seen: int, extracted: int, skipped_invalid: int,
) -> None:
    summary = (
        f"Extracted {extracted} memorie(s) from {messages_seen} operator "
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
