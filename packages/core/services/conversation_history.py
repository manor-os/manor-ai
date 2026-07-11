from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.output_policy import (
    PROVIDER_REASONING_META_KEY,
    runtime_strip_leaked_tool_activity,
)
from packages.core.ai.runtime import (
    runtime_execute_conversation_summary_completion,
    runtime_conversation_summary_text,
)
from packages.core.ai.runtime.streams import runtime_persisted_tool_calls_history_summary

logger = logging.getLogger(__name__)

HISTORY_TOKEN_BUDGET = 80_000
CHARS_PER_TOKEN = 4
SUMMARY_TRIGGER = 10
MAX_HISTORY_ROWS = 200
CHAT_MODE_MARKER_RE = re.compile(r"^\[Mode:\s*.+?\]\s*$", re.IGNORECASE)
CHAT_MODE_SETTINGS_MARKER_RE = re.compile(r"^\[Mode settings:\s*\{.*\}\]\s*$", re.IGNORECASE)
GENERATIVE_TOOL_HISTORY_NAMES = {
    "generate_file",
    "generate_image",
    "generate_video",
    "generate_audio",
}


def is_running_stream_placeholder(message: Any) -> bool:
    meta = message.meta or {}
    return (
        message.role == "assistant"
        and meta.get("stream_status") in {"running", "streaming"}
    )


def strip_leaked_tool_activity(content: str) -> str:
    """Compatibility wrapper for runtime-owned output cleanup."""

    return runtime_strip_leaked_tool_activity(content)


def strip_chat_mode_history_markers(content: str) -> str:
    """Remove UI-only chat-mode labels before history is sent to the model."""

    lines = [
        line
        for line in str(content or "").splitlines()
        if not CHAT_MODE_MARKER_RE.match(line.strip())
        and not CHAT_MODE_SETTINGS_MARKER_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


def should_include_previous_tool_activity_for_turn(
    latest_user_message: str | None,
    tool_summary: str | None,
) -> bool:
    """Decide whether prior tool activity is safe to replay into this turn.

    Persisted tool summaries are operational traces, not user-visible memory.
    When a new user turn is being built, keep prior prose history but do not
    replay earlier tool traces into the model as fresh context. This prevents
    stale queued/search actions from being interpreted as active work without
    relying on keyword or topic matching.
    """

    if not tool_summary:
        return False
    return not str(latest_user_message or "").strip()


def should_include_tool_history_summary(message: Any) -> bool:
    """Keep prior tool context, except generation calls that can re-trigger old intent."""

    raw = getattr(message, "tool_calls", None)
    if not raw:
        return False
    calls = raw if isinstance(raw, list) else [{"name": name, "result": result} for name, result in raw.items()] if isinstance(raw, dict) else []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if name in GENERATIVE_TOOL_HISTORY_NAMES:
            return False
    return True


async def load_conversation_history(
    db: AsyncSession,
    conversation_id: str,
    *,
    token_budget: int = HISTORY_TOKEN_BUDGET,
    latest_user_message: str | None = None,
) -> list[dict]:
    """Load conversation history within a token budget."""

    msgs = [
        m
        for m in await _list_messages_for_history(db, conversation_id, limit=MAX_HISTORY_ROWS)
        if not is_running_stream_placeholder(m)
    ]
    if not msgs:
        return []

    char_budget = token_budget * CHARS_PER_TOKEN
    used_chars = 0
    selected: list[Any] = []

    for m in reversed(msgs):
        meta = m.meta or {}
        reasoning_chars = len(str(meta.get(PROVIDER_REASONING_META_KEY) or ""))
        tool_summary = runtime_persisted_tool_calls_history_summary(m.tool_calls)
        msg_chars = (
            len(m.content or "")
            + len(tool_summary or "")
            + reasoning_chars
        )
        if used_chars + msg_chars > char_budget and selected:
            break
        used_chars += msg_chars
        selected.append(m)

    selected.reverse()

    history: list[dict] = []
    if len(selected) < len(msgs):
        conv = await _get_conversation_for_history(db, conversation_id)
        if conv and conv.summary:
            history.append({
                "role": "system",
                "content": f"[Earlier conversation summary]\n{conv.summary}",
            })

        dropped = len(msgs) - len(selected)
        if dropped >= SUMMARY_TRIGGER:
            try:
                asyncio.create_task(
                    update_conversation_summary(db, conversation_id, msgs[:dropped])
                )
            except Exception:
                pass

    for m in selected:
        content = strip_chat_mode_history_markers(
            strip_leaked_tool_activity(m.content or "")
        )
        tool_summary = (
            runtime_persisted_tool_calls_history_summary(m.tool_calls)
            if should_include_tool_history_summary(m)
            else None
        )
        if tool_summary and not should_include_previous_tool_activity_for_turn(
            latest_user_message,
            tool_summary,
        ):
            tool_summary = None
        if tool_summary:
            content = f"{content}\n\n{tool_summary}" if content else tool_summary
        entry: dict = {
            "role": m.role,
            "content": content,
        }
        meta = m.meta or {}
        reasoning_content = meta.get(PROVIDER_REASONING_META_KEY)
        if (
            m.role == "assistant"
            and isinstance(reasoning_content, str)
            and reasoning_content.strip()
        ):
            entry["reasoning_content"] = reasoning_content
        history.append(entry)
    return history


async def update_conversation_summary(
    db: AsyncSession,
    conversation_id: str,
    dropped_messages: list[Any],
) -> None:
    """Generate a rolling summary of dropped messages and store it."""

    try:
        conv_row = await _get_conversation_for_history(db, conversation_id)

        text_block = runtime_conversation_summary_text(dropped_messages)
        if not text_block:
            return

        completion = await runtime_execute_conversation_summary_completion(
            entity_id=conv_row.entity_id if conv_row else None,
            workspace_id=conv_row.workspace_id if conv_row else None,
            text_block=text_block,
        )
        summary = completion.content
        if summary and summary.strip():
            conv = await _get_conversation_for_history(db, conversation_id)
            if conv:
                conv.summary = summary.strip()
                await db.flush()
                logger.info(
                    "Updated conversation summary for %s (%d chars)",
                    conversation_id,
                    len(conv.summary),
                )
    except Exception:
        logger.warning("Failed to update conversation summary", exc_info=True)


async def _list_messages_for_history(
    db: AsyncSession,
    conversation_id: str,
    *,
    limit: int,
) -> list[Any]:
    from packages.core.models.task import Message

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    msgs = list(result.scalars().all())
    msgs.reverse()
    return msgs


async def _get_conversation_for_history(
    db: AsyncSession,
    conversation_id: str,
) -> Any | None:
    from packages.core.models.task import Conversation

    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    return result.scalar_one_or_none()
