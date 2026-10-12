"""Transaction-scoped duplicate admission for both memory stores."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import func, or_, select, text

from packages.core.memory.policy import content_fingerprint, normalized_content
from packages.core.models.memory import AgentMemory


async def find_duplicate(
    db,
    *,
    entity_id: str,
    content: str,
    memory_type: str,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    scope: str | None = None,
    expires_at: datetime | None = None,
) -> AgentMemory | None:
    fingerprint = content_fingerprint(content)
    expires_key = None
    if expires_at is not None:
        expires_key = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None
            else expires_at.astimezone(timezone.utc)
        ).isoformat()
    identity = [entity_id, workspace_id, agent_id, user_id, scope, memory_type, fingerprint, expires_key]
    lock = int.from_bytes(hashlib.sha256(json.dumps(identity).encode()).digest()[:8], "big", signed=True)
    # Same-content concurrent writers serialize until commit/rollback. Redis
    # caching is never the consistency mechanism for durable writes.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    query = (
        select(AgentMemory)
        .where(
            AgentMemory.entity_id == entity_id,
            AgentMemory.workspace_id == workspace_id,
            AgentMemory.agent_id == agent_id,
            AgentMemory.user_id == user_id,
            AgentMemory.scope == scope,
            AgentMemory.memory_type == memory_type,
            AgentMemory.status == "active",
            AgentMemory.expires_at == expires_at,
            or_(
                AgentMemory.metadata_["content_fingerprint"].astext == fingerprint,
                func.regexp_replace(func.btrim(AgentMemory.content), r"\s+", " ", "g") == normalized_content(content),
            ),
        )
        .order_by(AgentMemory.created_at)
    )
    rows = (await db.execute(query)).scalars().all()
    # A stale fingerprint must neither suppress a write nor hide a later match.
    return next((row for row in rows if normalized_content(row.content) == normalized_content(content)), None)


async def existing_memories(db, *, entity_id, workspace_id=None, user_id=None, agent_id=None) -> list[dict]:
    """Bounded, currently active comparison set; caller already owns the scope."""
    query = (
        select(AgentMemory)
        .where(
            AgentMemory.entity_id == entity_id,
            AgentMemory.workspace_id == workspace_id,
            AgentMemory.user_id == user_id,
            AgentMemory.agent_id == agent_id,
            AgentMemory.status == "active",
            or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > func.now()),
        )
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.id)
        .limit(40)
    )
    rows = (await db.execute(query)).scalars().all()
    result, size = [], 0
    for row in rows:
        # Never clip away qualifications just to fit the comparison budget.
        if size + len(row.content) > 12_000:
            continue
        result.append({"id": row.id, "scope": row.scope or row.memory_type, "content": row.content})
        size += len(row.content)
    return result
