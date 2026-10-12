"""Agent conversation memory service."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    from packages.core.memory.dedup import find_duplicate
    from packages.core.memory.policy import content_fingerprint
    duplicate = await find_duplicate(db, entity_id=entity_id, content=prepared_content,
        memory_type=memory_type, agent_id=agent_id, user_id=user_id, expires_at=expires_at)
    if duplicate is not None:
        return duplicate
    prepared_metadata["content_fingerprint"] = content_fingerprint(prepared_content)
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


async def _relevance_ranked_memories(
    db: AsyncSession,
    entity_id: str,
    *,
    agent_id: str | None,
    user_id: str | None,
    query: str,
    limit: int,
) -> list[AgentMemory] | None:
    """Rank the entity's memories by vector similarity to ``query``.

    Returns None when relevance ranking is unavailable (no embedding service,
    no embedding column) so the caller can fall back to importance ordering.
    Rows without an embedding are appended after ranked rows by importance,
    so un-embedded memories never become invisible.
    """
    import asyncio

    from sqlalchemy import text

    try:
        from packages.core.services.embedding_service import generate_embedding

        embedding = await asyncio.wait_for(generate_embedding(query), timeout=10.0)
    except Exception:
        return None
    if not embedding:
        return None

    probe = await db.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'agent_memories'
          AND column_name = 'embedding'
        LIMIT 1
    """))
    if probe.scalar_one_or_none() is None:
        return None

    vec_str = "[" + ",".join(f"{v:.7f}" for v in embedding) + "]"
    sql = """
        SELECT id
        FROM agent_memories
        WHERE entity_id = :entity
          AND status = 'active'
          AND workspace_id IS NULL
          AND agent_id IS NOT DISTINCT FROM :agent
          AND user_id IS NOT DISTINCT FROM :user
          AND (expires_at IS NULL OR expires_at > now())
    """
    params: dict = {"entity": entity_id, "vec": vec_str, "k": limit, "agent": agent_id, "user": user_id}
    sql += """
        ORDER BY (embedding IS NULL) ASC,
                 embedding <=> CAST(:vec AS vector) ASC NULLS LAST,
                 importance DESC
        LIMIT :k
    """
    ranked_ids = [row[0] for row in (await db.execute(text(sql), params)).all()]
    if not ranked_ids:
        return []

    rows = (await db.execute(
        select(AgentMemory).where(AgentMemory.id.in_(ranked_ids))
    )).scalars().all()
    by_id = {row.id: row for row in rows}
    return [by_id[mem_id] for mem_id in ranked_ids if mem_id in by_id]


async def get_context_memories(
    db: AsyncSession,
    entity_id: str,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    max_tokens: int = 2000,
    query: str | None = None,
    sync_files: bool = True,
) -> str:
    """Get relevant memories formatted for injection into system prompt.

    When ``query`` is provided (the active user message) and embeddings are
    available, memories are ranked by relevance to the query with importance
    as the tie-breaker — importance-only ordering injects the same top
    entries into every turn regardless of topic. Falls back to importance
    DESC when relevance ranking is unavailable. Concatenates until the
    max_tokens budget is reached. Returns a formatted string like:

    ## Your Memory
    - [fact] User prefers weekly email reports
    - [preference] Communication style: concise and direct
    - [context] Company is a property management firm with 50 units
    """
    from packages.core.memory.manager import select_memory_records
    from packages.core.memory.policy import content_fingerprint

    if sync_files:
        try:
            await sync_agent_memory_files(
                db, entity_id=entity_id, agent_id=agent_id, user_id=user_id, exact_scope=True,
            )
        except Exception:
            logger.debug("agent memory file sync skipped", exc_info=True)

    # This is prompt retrieval, not the administrative list API. Every dimension
    # is exact, including NULL. Apply filters before LIMIT or relevance ranking.
    scoped = select(AgentMemory).where(
        AgentMemory.entity_id == entity_id, AgentMemory.workspace_id.is_(None),
        AgentMemory.agent_id == agent_id, AgentMemory.user_id == user_id,
        AgentMemory.status == "active",
        or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > func.now()),
    )
    # Estimate ~4 chars per token
    max_chars = max_tokens * 4

    memories: list[AgentMemory] | None = None
    if query and query.strip():
        try:
            memories = await _relevance_ranked_memories(
                db,
                entity_id,
                agent_id=agent_id,
                user_id=user_id,
                query=query.strip(),
                limit=100,
            )
        except Exception:
            logger.debug("relevance memory ranking failed", exc_info=True)
            memories = None
    ranked_by_relevance = memories is not None
    if memories is None:
        memories = list((await db.scalars(scoped.order_by(AgentMemory.importance.desc(), AgentMemory.id).limit(100))).all())
    else:
        # Recheck the current snapshot after asynchronous ranking, before any
        # worker sees it (also protects alternative ranking adapters).
        current = (await db.scalars(scoped.where(AgentMemory.id.in_([m.id for m in memories]))
                                   .execution_options(populate_existing=True))).all()
        by_id = {m.id: m for m in current}
        memories = [by_id[m.id] for m in memories if m.id in by_id]

    # Filter expired
    now = datetime.now(timezone.utc)
    active = [m for m in memories if not m.expires_at or m.expires_at > now]

    if not ranked_by_relevance:
        # Sort by importance DESC (already sorted from query, but ensure)
        active.sort(key=lambda m: m.importance, reverse=True)

    records = [{"id": m.id, "content": m.content, "scope": m.memory_type,
                "revision": content_fingerprint(m.content)} for m in active]
    selected = await select_memory_records(
        records, query=query, entity_id=entity_id, agent_id=agent_id, user_id=user_id,
    )
    current = (await db.scalars(scoped.where(
        AgentMemory.id.in_([r["id"] for r in selected]),
        or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > datetime.now(timezone.utc)),
    )
                               .execution_options(populate_existing=True))).all()
    by_id = {m.id: m for m in current}
    active = [by_id[r["id"]] for r in selected if r["id"] in by_id
              and content_fingerprint(by_id[r["id"]].content) == r["revision"]
              and by_id[r["id"]].memory_type == r["scope"]]

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
        from packages.core.memory.policy import content_fingerprint
        kwargs["metadata_"]["content_fingerprint"] = content_fingerprint(prepared_content)

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
    db: AsyncSession, entity_id: str, conversation_id: str, *,
    agent_id: str | None = None, user_id: str | None = None,
):
    """Return reviewable candidates through the shared chat admission service.

    The caller supplies the authenticated user, never an inferred owner. This
    legacy API now has the same evidence and approval rules as scheduled Chat
    and compact; explicit manual add_memory remains a direct operator edit.
    """
    from types import SimpleNamespace
    from packages.core.memory.compaction import MemoryScopeDenied, prepare_chat_memory
    from packages.core.models.task import Conversation, Message
    from packages.core.models.runtime_learning import AgentLearningCandidate

    conversation = await db.scalar(select(Conversation).where(
        Conversation.id == conversation_id, Conversation.entity_id == entity_id,
    ))
    if not user_id or conversation is None:
        raise MemoryScopeDenied("Memory extraction requires an authorized conversation actor")
    if agent_id is not None and agent_id != conversation.agent_id:
        raise MemoryScopeDenied("Memory extraction agent differs from the conversation")
    ctx = SimpleNamespace(entity_id=entity_id, user_id=user_id, agent_id=conversation.agent_id,
                          workspace_id=conversation.workspace_id, conversation_id=conversation.id)
    messages = (await db.scalars(select(Message).where(
        Message.conversation_id == conversation_id, Message.role == "user",
        Message.author_kind == "user", Message.message_kind == "text", Message.content.is_not(None),
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(50))).all()
    result = await prepare_chat_memory(ctx, [], source="conversation_extract", source_message_ids=[m.id for m in messages])
    return list((await db.scalars(select(AgentLearningCandidate).where(
        AgentLearningCandidate.entity_id == entity_id, AgentLearningCandidate.id.in_(result["candidate_ids"]),
    ))).all())
