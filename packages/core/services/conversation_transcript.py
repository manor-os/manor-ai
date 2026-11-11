"""Append-only model transcript for Manor chat: owner chat, agent DMs, Workspace
chat, customer conversations and group rooms.

History used to be re-rendered from the messages table every turn: final
replies plus 320-char tool previews in a sliding window. The transcript keeps
what the model actually worked with — user requests, tool calls, tool outputs,
replies — and the next run sends it again unchanged, so earlier tool data is
available and the request prefix stays cacheable.

Working history = latest ``compaction``/``legacy`` item's messages + every
``message`` item after it. Messages-table rows no run wrote (notifications,
turns from entry points that do not write transcripts) are appended as they
appear, so nothing visible to the user is missing from the model's history.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_host_feedback_message
from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.models.conversation_transcript import (
    TRANSCRIPT_KIND_COMPACTION,
    TRANSCRIPT_KIND_LEGACY,
    TRANSCRIPT_KIND_MESSAGE,
    ConversationTranscriptItem,
)

logger = logging.getLogger(__name__)

# Tool output kept per item, ~10k tokens (Codex uses 10k tokens per output).
TRANSCRIPT_TOOL_RESULT_MAX_CHARS = 40_000
_BASE_KINDS = (TRANSCRIPT_KIND_COMPACTION, TRANSCRIPT_KIND_LEGACY)
_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content")


@dataclass(frozen=True)
class TranscriptScope:
    entity_id: str
    conversation_id: str | None
    user_id: str | None
    agent_id: str | None
    workspace_id: str | None
    # Group rooms have no conversation row; their streams hang off the room.
    room_id: str | None = None
    stream_key: str | None = None

    @property
    def key(self) -> str:
        return self.stream_key or f"{self.user_id or ''}|{self.agent_id or ''}|{self.workspace_id or ''}"


def _stream(scope: TranscriptScope) -> tuple:
    owner = (
        ConversationTranscriptItem.room_id == scope.room_id if scope.room_id
        else ConversationTranscriptItem.conversation_id == scope.conversation_id
    )
    return owner, ConversationTranscriptItem.scope_key == scope.key


def transcript_scope_for_turn(
    surface: Any,
    *,
    entity_id: str | None,
    conversation_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
) -> TranscriptScope | None:
    """The transcript a chat turn reads and appends to, or None for rendered history.

    Each running principal has its own stream, also in a shared Workspace chat:
    a user's tool outputs never reach another user's runs, and other people's
    turns arrive as sender-labelled rows synced from the messages table.
    Customer conversations (public webchat, external channels) belong to one
    customer, so their stream is the conversation's for each serving Agent,
    whether or not the visitor is signed in.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface

    if not conversation_id:
        return None
    if surface in {ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT}:
        return TranscriptScope(
            entity_id=entity_id or "", conversation_id=conversation_id, user_id=None,
            agent_id=agent_id, workspace_id=workspace_id,
        )
    covered = (
        (surface == ChatSurface.GLOBAL_OWNER_CHAT and not workspace_id)
        or (surface == ChatSurface.AGENT_DM and bool(agent_id))
        or (surface in {ChatSurface.WORKSPACE_CHAT, ChatSurface.TASK_COMMENT_THREAD} and bool(workspace_id))
    )
    if not (covered and user_id):
        return None
    return TranscriptScope(
        entity_id=entity_id or "", conversation_id=conversation_id, user_id=user_id,
        agent_id=agent_id, workspace_id=workspace_id,
    )


def _clean(message: dict) -> dict:
    cleaned = {key: message[key] for key in _MESSAGE_KEYS if key in message and message[key] is not None}
    content = cleaned.get("content")
    if isinstance(content, list):
        # Images/audio were seen in their own turn; re-sending base64 every
        # turn costs tokens without new information.
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(f"[{block.get('type') or 'attachment'} attached in this turn; not re-sent]")
        cleaned["content"] = "\n".join(part for part in parts if part)
    return cleaned


def _identity(message: dict) -> str:
    return json.dumps(
        {key: message.get(key) for key in ("role", "content", "tool_calls", "tool_call_id")},
        ensure_ascii=False, sort_keys=True, default=str,
    )


def transcript_messages(messages: list[dict]) -> list[dict]:
    """Messages worth carrying to later runs.

    System prompts are rebuilt every run. Runtime feedback, and the rejected
    candidate reply it answered, only steered the run that produced them.
    """
    kept: list[dict] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") in {"system", "developer"}:
            continue
        if runtime_is_host_feedback_message(message):
            if kept and kept[-1].get("role") == "assistant" and not kept[-1].get("tool_calls"):
                kept.pop()
            continue
        kept.append(_clean(message))
    return kept


async def _items(db: AsyncSession, scope: TranscriptScope, *, from_seq: int = 0) -> list[ConversationTranscriptItem]:
    return list((await db.execute(
        select(ConversationTranscriptItem).where(
            *_stream(scope),
            ConversationTranscriptItem.seq >= from_seq,
        ).order_by(ConversationTranscriptItem.seq)
    )).scalars().all())


async def _base_seq(db: AsyncSession, scope: TranscriptScope) -> int | None:
    return (await db.execute(
        select(func.max(ConversationTranscriptItem.seq)).where(
            *_stream(scope), ConversationTranscriptItem.kind.in_(_BASE_KINDS),
        )
    )).scalar_one_or_none()


async def _working_history(
    db: AsyncSession, scope: TranscriptScope,
) -> tuple[list[dict], ConversationTranscriptItem | None]:
    base_seq = await _base_seq(db, scope)
    if base_seq is None:
        return [], None
    items = await _items(db, scope, from_seq=base_seq)
    base = items[0]
    history = list((base.payload or {}).get("messages") or [])
    history.extend(item.payload for item in items[1:] if item.kind == TRANSCRIPT_KIND_MESSAGE)
    return history, base


async def _sync_start(db: AsyncSession, scope: TranscriptScope, base_seq: int) -> tuple[int, datetime]:
    """(seq, created_at) where the check for rows no run wrote starts.

    One snapshot before the working base, or the stream's first item: a
    compaction is written when its run ends, so rows that arrived while it ran,
    or replies still streaming when it started, are in no item before it. Older
    rows were synced by an earlier turn, and the read stays the same size
    however long the conversation grows.
    """
    previous = (await db.execute(
        select(ConversationTranscriptItem.seq, ConversationTranscriptItem.created_at).where(
            *_stream(scope),
            ConversationTranscriptItem.kind.in_(_BASE_KINDS),
            ConversationTranscriptItem.seq < base_seq,
        ).order_by(ConversationTranscriptItem.seq.desc()).limit(1)
    )).first()
    if previous is None:
        previous = (await db.execute(
            select(ConversationTranscriptItem.seq, ConversationTranscriptItem.created_at).where(
                *_stream(scope),
            ).order_by(ConversationTranscriptItem.seq).limit(1)
        )).one()
    return previous.seq, previous.created_at


async def _represented_message_ids(db: AsyncSession, scope: TranscriptScope, *, from_seq: int) -> set[str]:
    # Only snapshots record source ids in their payload; message payloads hold
    # tool outputs and are never read here.
    snapshot_ids = case(
        (ConversationTranscriptItem.kind.in_(_BASE_KINDS), ConversationTranscriptItem.payload["source_message_ids"]),
        else_=None,
    )
    rows = (await db.execute(
        select(ConversationTranscriptItem.source_message_id, snapshot_ids).where(
            *_stream(scope), ConversationTranscriptItem.seq >= from_seq,
        )
    )).all()
    ids: set[str] = set()
    for source_id, source_ids in rows:
        if source_id:
            ids.add(source_id)
        ids.update(str(value) for value in (source_ids if isinstance(source_ids, list) else []) if value)
    return ids


async def _append(db: AsyncSession, scope: TranscriptScope, entries: list[tuple[str, dict, str | None]], *, run_id: str | None) -> None:
    """Append (kind, payload, source_message_id) entries after the current last item."""
    if not entries:
        return
    for attempt in range(2):
        last = (await db.execute(
            select(func.max(ConversationTranscriptItem.seq)).where(*_stream(scope))
        )).scalar_one_or_none() or 0
        for offset, (kind, payload, source_id) in enumerate(entries, start=1):
            db.add(ConversationTranscriptItem(
                entity_id=scope.entity_id, conversation_id=scope.conversation_id, room_id=scope.room_id,
                scope_key=scope.key,
                user_id=scope.user_id, agent_id=scope.agent_id, workspace_id=scope.workspace_id,
                seq=last + offset, kind=kind, run_id=run_id, source_message_id=source_id, payload=payload,
                token_estimate=runtime_estimate_tokens_for_text(json.dumps(payload, ensure_ascii=False, default=str)),
            ))
        try:
            await db.commit()
            return
        except IntegrityError:
            # Another writer took these seq numbers; append after it.
            await db.rollback()
            if attempt:
                raise


def _with_attachment_context(content: str, attachment_context: str | None) -> str:
    if not attachment_context:
        return content
    return f"{content}\n\n{attachment_context}" if content else attachment_context


async def _shared_senders(db: AsyncSession, scope: TranscriptScope, rows: list[Any]) -> Any:
    """Sender labels when the conversation is a shared Workspace chat, else None."""
    if not (scope.workspace_id and scope.user_id and scope.conversation_id):
        return None
    from packages.core.services.conversation_history import shared_workspace_senders

    return await shared_workspace_senders(db, scope.conversation_id, rows, scope.user_id)


async def transcript_current_sender_note(db: AsyncSession, scope: TranscriptScope) -> str | None:
    """Who sends the next message, for shared Workspace chats (system-prompt text)."""
    senders = await _shared_senders(db, scope, [])
    return senders.current_sender_note() if senders is not None else None


async def _external_message(db: AsyncSession, row: Any, senders: Any = None) -> dict | None:
    """A messages-table row as rendered history would show it (minus tool previews)."""
    from packages.core.services.conversation_history import (
        FAILED_ASSISTANT_TURN_HISTORY,
        is_failed_assistant_turn,
        is_running_stream_placeholder,
        message_attachment_context,
        strip_leaked_tool_activity,
    )

    if row.role not in {"user", "assistant"} or is_running_stream_placeholder(row):
        return None
    content = (
        FAILED_ASSISTANT_TURN_HISTORY if is_failed_assistant_turn(row)
        else strip_leaked_tool_activity(str(row.content or "")).strip()
    )
    content = _with_attachment_context(content, await message_attachment_context(db, row))
    if senders is not None and content:
        content = senders.label(row, content)
    return {"role": row.role, "content": content} if content else None


LEGACY_SUMMARY_PREFIX = "[Earlier conversation summary]"


def _summary_as_context(message: dict) -> dict:
    """Keep rendered history's rolling summary; other system messages are per-turn."""
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if message.get("role") == "system" and isinstance(content, str) and content.startswith(LEGACY_SUMMARY_PREFIX):
        return {"role": "user", "content": content}
    return message


ExternalRows = Callable[[datetime, set[str]], Awaitable[list[tuple[str, dict]]]]


def _conversation_external_rows(db: AsyncSession, scope: TranscriptScope) -> ExternalRows:
    async def rows_since(started_at: datetime, represented: set[str]) -> list[tuple[str, dict]]:
        from packages.core.models.task import Message

        rows = (await db.execute(
            select(Message).where(
                Message.conversation_id == scope.conversation_id,
                Message.created_at > started_at,
            ).order_by(Message.created_at, Message.id)
        )).scalars().all()
        unrepresented = [row for row in rows if row.id not in represented]
        senders = await _shared_senders(db, scope, unrepresented) if unrepresented else None
        return [
            (row.id, message)
            for row in unrepresented
            for message in [await _external_message(db, row, senders)] if message
        ]
    return rows_since


async def load_or_import_transcript(
    db: AsyncSession,
    scope: TranscriptScope,
    *,
    legacy_history: Callable[[], Awaitable[list[dict]]],
    exclude_message_ids: set[str],
    external_rows: ExternalRows | None = None,
) -> list[dict]:
    """History for the next run: the stored transcript, importing it once if missing.

    ``exclude_message_ids`` are the current request and its reply placeholder,
    which this run writes itself. ``external_rows(started_at, represented)``
    returns visible rows no run wrote, as (row id, message); conversations read
    the messages table by default.
    """
    history, base = await _working_history(db, scope)
    if base is None:
        imported = transcript_messages([_summary_as_context(m) for m in await legacy_history()])
        await _append(db, scope, [(TRANSCRIPT_KIND_LEGACY, {
            "messages": imported,
            "covered_through": datetime.now(timezone.utc).isoformat(),
        }, None)], run_id=None)
        return imported
    # Rows no run wrote (notifications, other entry points) still belong in
    # the model's history.
    start_seq, started_at = await _sync_start(db, scope, base.seq)
    represented = await _represented_message_ids(db, scope, from_seq=start_seq) | set(exclude_message_ids)
    rows_since = external_rows or _conversation_external_rows(db, scope)
    external = [
        (TRANSCRIPT_KIND_MESSAGE, message, row_id)
        for row_id, message in await rows_since(started_at, represented)
    ]
    if external:
        await _append(db, scope, external, run_id=None)
        history.extend(message for _, message, _ in external)
    return history


async def _recorded_request_content(
    db: AsyncSession, scope: TranscriptScope, message_id: str | None, content: str,
) -> str:
    """The run's request as later runs should see it.

    The run saw the file's text or image; later runs need its persisted ref
    (document_id/path/url) to open it again. In a shared Workspace chat the
    request also carries its sender label, like everyone else's turns.
    """
    if scope.room_id:
        return content  # room requests already carry their sender label
    from packages.core.models.task import Message
    from packages.core.services.conversation_history import message_attachment_context

    row = await db.get(Message, message_id) if message_id else None
    if row is not None:
        content = _with_attachment_context(content, await message_attachment_context(db, row))
    author = row if row is not None else SimpleNamespace(role="user", meta={"author_user_id": scope.user_id})
    senders = await _shared_senders(db, scope, [author])
    return senders.label(author, content) if senders is not None else content


async def append_run_transcript(
    db: AsyncSession,
    scope: TranscriptScope,
    *,
    run_id: str | None,
    initial_messages: list[dict],
    final_messages: list[dict],
    user_message_id: str | None,
    assistant_message_id: str | None,
) -> None:
    """Append what this run added, or one compaction item if it compacted."""
    if await _base_seq(db, scope) is None:
        # The stream was discarded while the run worked (a retracted message,
        # a closed room); the next run re-imports what is still visible.
        return
    body = [m for m in final_messages if isinstance(m, dict) and m.get("role") not in {"system", "developer"}]
    initial = [_identity(m) for m in initial_messages]
    source_ids = [value for value in (user_message_id, assistant_message_id) if value]
    if len(body) > len(initial) and [_identity(m) for m in body[:len(initial)]] == initial:
        added = transcript_messages(body[len(initial):])
        final_reply = max(
            (index for index, m in enumerate(added) if m.get("role") == "assistant" and not m.get("tool_calls")),
            default=None,
        )
        entries = []
        for index, message in enumerate(added):
            source_id = None
            if index == 0 and message.get("role") == "user":
                source_id = user_message_id
                message = {**message, "content": await _recorded_request_content(
                    db, scope, user_message_id, str(message.get("content") or ""),
                )}
            elif index == final_reply:
                source_id = assistant_message_id
            entries.append((TRANSCRIPT_KIND_MESSAGE, message, source_id))
        await _append(db, scope, entries, run_id=run_id)
        return
    # The loop replaced its history (compaction): keep its final working view.
    await _append(db, scope, [(TRANSCRIPT_KIND_COMPACTION, {
        "messages": transcript_messages(body), "source_message_ids": source_ids,
    }, None)], run_id=run_id)


async def discard_room_transcripts(
    db: AsyncSession, room_id: str, *, keep_stream_key: str | None = None, stream_key_prefix: str | None = None,
) -> None:
    """Delete a room's agent transcripts (caller commits).

    Called when a message is retracted or the room is closed, so no model
    context outlives what members can still see, and when an Agent's audience
    changes, so the stale stream does not linger.
    """
    from sqlalchemy import delete

    query = delete(ConversationTranscriptItem).where(ConversationTranscriptItem.room_id == room_id)
    if stream_key_prefix is not None:
        query = query.where(ConversationTranscriptItem.scope_key.startswith(stream_key_prefix, autoescape=True))
    if keep_stream_key is not None:
        query = query.where(ConversationTranscriptItem.scope_key != keep_stream_key)
    await db.execute(query)
