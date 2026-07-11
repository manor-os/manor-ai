"""Agent conversation memory service."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_conversation_memory_text,
    runtime_execute_conversation_memory_extraction_completion,
)
from packages.core.models.base import generate_ulid
from packages.core.models.memory import AgentMemory
from packages.core.services.agent_memory_files import (
    delete_memory_file,
    maybe_move_memory_file,
    sync_agent_memory_files,
    write_memory_file,
)

logger = logging.getLogger(__name__)

_MEMORY_STORE_HARD_MAX_CHARS = 20_000
_MEMORY_PROMPT_ITEM_MAX_CHARS = 900
_MEMORY_PROMPT_MIN_BODY_CHARS = 120


def _coerce_memory_content(content: object) -> str:
    text = "" if content is None else str(content)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _truncate_middle(text: str, max_chars: int, *, label: str = "truncated") -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 12:
        return text[:max_chars]

    omitted = len(text) - max_chars
    marker = f"\n... [{label}; approx {max(omitted, 1)} chars omitted] ...\n"
    if len(marker) + 24 >= max_chars:
        return text[: max_chars - 3].rstrip() + "..."

    remaining = max_chars - len(marker)
    head = max(1, remaining * 2 // 3)
    tail = max(1, remaining - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _prepare_memory_content(
    content: object,
    metadata: dict | None,
) -> tuple[str, dict]:
    text = _coerce_memory_content(content)
    meta = dict(metadata or {})
    original_chars = len(text)
    if original_chars > _MEMORY_STORE_HARD_MAX_CHARS:
        text = _truncate_middle(
            text,
            _MEMORY_STORE_HARD_MAX_CHARS,
            label="memory store hard cap",
        )
        meta.setdefault("original_content_chars", original_chars)
        meta["content_truncated_for_memory_store"] = True
    return text, meta


def _compact_memory_for_prompt(content: object, max_chars: int) -> str:
    text = " ".join(_coerce_memory_content(content).split())
    if max_chars <= 0:
        return ""
    return _truncate_middle(text, max_chars, label="prompt budget")


def _append_with_budget(lines: list[str], line: str, *, max_chars: int) -> bool:
    current = sum(len(part) for part in lines) + max(0, len(lines) - 1)
    next_len = current + (1 if lines else 0) + len(line)
    if next_len > max_chars:
        return False
    lines.append(line)
    return True


async def add_memory(
    db: AsyncSession,
    entity_id: str,
    content: str,
    memory_type: str = "fact",
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    importance: int = 5,
    source: str | None = None,
    metadata: dict | None = None,
    expires_at: datetime | None = None,
) -> AgentMemory:
    """Add a memory entry."""
    prepared_content, prepared_metadata = _prepare_memory_content(content, metadata)
    mem = AgentMemory(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent_id,
        user_id=user_id,
        memory_type=memory_type,
        content=prepared_content,
        importance=min(max(importance, 1), 10),
        source=source,
        metadata_=prepared_metadata,
        expires_at=expires_at,
        status="active",
    )
    db.add(mem)
    await db.flush()
    write_memory_file(mem)
    return mem


async def list_memories(
    db: AsyncSession,
    entity_id: str,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 50,
) -> list[AgentMemory]:
    """List memories for an entity/agent/user combo."""
    try:
        await sync_agent_memory_files(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            user_id=user_id,
        )
    except Exception:
        logger.debug("agent memory file sync skipped", exc_info=True)

    q = (
        select(AgentMemory)
        .where(AgentMemory.entity_id == entity_id, AgentMemory.status == "active")
    )
    if agent_id is not None:
        q = q.where(AgentMemory.agent_id == agent_id)
    if user_id is not None:
        q = q.where(AgentMemory.user_id == user_id)
    if memory_type is not None:
        q = q.where(AgentMemory.memory_type == memory_type)

    q = q.order_by(AgentMemory.importance.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_context_memories(
    db: AsyncSession,
    entity_id: str,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    max_tokens: int = 2000,
) -> str:
    """Get relevant memories formatted for injection into system prompt.

    Selects active, non-expired memories sorted by importance DESC.
    Concatenates them until max_tokens budget is reached.
    Returns a formatted string like:

    ## Your Memory
    - [fact] User prefers weekly email reports
    - [preference] Communication style: concise and direct
    - [context] Company is a property management firm with 50 units
    """
    # Estimate ~4 chars per token
    max_chars = max_tokens * 4

    memories = await list_memories(
        db, entity_id, agent_id=agent_id, user_id=user_id, limit=100
    )

    # Filter expired
    now = datetime.now(timezone.utc)
    active = [m for m in memories if not m.expires_at or m.expires_at > now]

    # Sort by importance DESC (already sorted from query, but ensure)
    active.sort(key=lambda m: m.importance, reverse=True)

    lines = ["## Your Memory"]
    omitted = 0

    for idx, mem in enumerate(active):
        prefix = f"- [{mem.memory_type}] "
        current = sum(len(part) for part in lines) + max(0, len(lines) - 1)
        remaining = max_chars - current - 1 - len(prefix)
        if remaining < _MEMORY_PROMPT_MIN_BODY_CHARS:
            omitted = len(active) - idx
            break

        body_limit = min(_MEMORY_PROMPT_ITEM_MAX_CHARS, remaining)
        body = _compact_memory_for_prompt(mem.content, body_limit)
        if not body:
            continue
        if not _append_with_budget(lines, prefix + body, max_chars=max_chars):
            omitted = len(active) - idx
            break

    if omitted:
        note = f"- [truncated] {omitted} more memories omitted due to context budget."
        _append_with_budget(lines, note, max_chars=max_chars)

    if len(lines) == 1:
        return ""  # no memories
    return "\n".join(lines)


async def update_memory(
    db: AsyncSession,
    memory_id: str,
    entity_id: str,
    **kwargs,
) -> AgentMemory | None:
    """Update a memory entry. Only updates provided fields."""
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.id == memory_id, AgentMemory.entity_id == entity_id
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        return None

    old_type = mem.memory_type
    allowed = {"content", "memory_type", "importance", "source", "metadata_", "expires_at", "status"}
    if "content" in kwargs:
        current_meta = dict(mem.metadata_ or {})
        provided_meta = kwargs.get("metadata_")
        if provided_meta is not None:
            current_meta.update(dict(provided_meta or {}))
        prepared_content, prepared_metadata = _prepare_memory_content(
            kwargs["content"],
            current_meta,
        )
        kwargs["content"] = prepared_content
        kwargs["metadata_"] = prepared_metadata

    for key, value in kwargs.items():
        if key in allowed:
            setattr(mem, key, value)

    await db.flush()
    maybe_move_memory_file(mem, old_type)
    await db.refresh(mem)
    return mem


async def delete_memory(db: AsyncSession, memory_id: str, entity_id: str) -> bool:
    """Hard-delete a memory entry."""
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.id == memory_id, AgentMemory.entity_id == entity_id
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        return False
    delete_memory_file(
        mem.entity_id,
        mem.agent_id,
        mem.id,
        mem.memory_type,
        user_id=mem.user_id,
    )
    await db.delete(mem)
    await db.flush()
    return True


async def archive_memory(db: AsyncSession, memory_id: str, entity_id: str) -> bool:
    """Archive a memory (soft remove from context)."""
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.id == memory_id, AgentMemory.entity_id == entity_id
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        return False
    mem.status = "archived"
    await db.flush()
    write_memory_file(mem)
    return True


async def extract_memories_from_conversation(
    db: AsyncSession,
    entity_id: str,
    conversation_id: str,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
) -> list[AgentMemory]:
    """Use LLM to extract memorable facts from a conversation.

    1. Load recent messages from conversation
    2. Ask LLM: "Extract key facts, preferences, and context worth remembering"
    3. Parse response into individual memory entries
    4. Store new memories
    """
    from packages.core.services.conversation_records import list_messages

    messages = await list_messages(db, conversation_id, limit=50)
    if not messages:
        return []

    conv_text = runtime_conversation_memory_text(messages)

    if len(conv_text) < 50:  # too short to extract memories
        return []

    completion = await runtime_execute_conversation_memory_extraction_completion(
        entity_id=entity_id,
        conversation_text=conv_text,
    )
    content = completion.content

    if not content:
        return []

    # Parse JSON response
    try:
        start = content.find("[")
        end = content.rfind("]") + 1
        if start >= 0 and end > start:
            items = json.loads(content[start:end])
        else:
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # Create memory entries
    new_memories: list[AgentMemory] = []
    for item in items[:10]:  # cap at 10 per conversation
        if not isinstance(item, dict) or "content" not in item:
            continue
        mem = await add_memory(
            db,
            entity_id,
            content=item["content"],
            memory_type=item.get("type", "fact"),
            agent_id=agent_id,
            user_id=user_id,
            importance=min(max(int(item.get("importance", 5)), 1), 10),
            source=f"conversation:{conversation_id}",
        )
        new_memories.append(mem)

    return new_memories
