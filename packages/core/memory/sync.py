"""FS ↔ DB sync — one-way (FS → DB) for the goal-driven runtime.

The Markdown file is the source of truth. The ``agent_memories`` row
holds an embedding (pgvector) for similarity search and a cached copy
of the body so LLM-context injection doesn't have to hit the disk.

Sync semantics:
  * ``sync_entry(entry)``     upsert one row from one MemoryEntry.
  * ``sync_workspace(...)``   walk all MD files under a workspace,
                              upsert + prune deleted rows.

We never write back to the file from the DB side — if the operator
wants to "fix" content, they edit the file. This avoids any class of
"I changed the file but the system overwrote it" footgun.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.memory.repo import MemoryEntry, list_entries
from packages.core.models.memory import AgentMemory

logger = logging.getLogger(__name__)

_SYNCED_WORKSPACE_MEMORY_CONTENT_MAX_CHARS = 40_000


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


def _content_for_row(body: str) -> str:
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return _truncate_middle(
        text,
        _SYNCED_WORKSPACE_MEMORY_CONTENT_MAX_CHARS,
        label="workspace memory sync hard cap",
    )


# ── Single-entry upsert ───────────────────────────────────────────────

async def sync_entry(
    db: AsyncSession,
    entry: MemoryEntry,
    *,
    embed: bool = True,
) -> AgentMemory:
    """Upsert one ``agent_memories`` row from a parsed MD file.

    ``embed=False`` skips the embedding call — useful for bulk seeding
    where embeddings are computed in a follow-up batch."""
    row = (await db.execute(
        select(AgentMemory).where(AgentMemory.id == entry.frontmatter.id)
    )).scalar_one_or_none()

    if row is None:
        row = AgentMemory(
            id=entry.frontmatter.id,
            entity_id=entry.entity_id,
            workspace_id=entry.workspace_id,
            agent_id=None,
            user_id=None,
            memory_type=entry.scope,           # mirror scope into the
            scope=entry.scope,                  # legacy memory_type field
            content=_content_for_row(entry.body),
            importance=entry.frontmatter.importance,
            confidence=entry.frontmatter.confidence,
            source=entry.frontmatter.source,
            metadata_=_build_metadata(entry),
            expires_at=entry.frontmatter.expires_at,
            status=entry.frontmatter.status,
        )
        db.add(row)
    else:
        row.entity_id = entry.entity_id
        row.workspace_id = entry.workspace_id
        row.memory_type = entry.scope
        row.scope = entry.scope
        row.content = _content_for_row(entry.body)
        row.importance = entry.frontmatter.importance
        row.confidence = entry.frontmatter.confidence
        row.source = entry.frontmatter.source
        row.metadata_ = _build_metadata(entry)
        row.expires_at = entry.frontmatter.expires_at
        row.status = entry.frontmatter.status

    await db.flush()

    if embed:
        await _embed_into_row(db, row, entry)

    return row


# ── Workspace sweep ───────────────────────────────────────────────────

async def sync_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    embed: bool = True,
    prune: bool = True,
) -> dict:
    """Walk MD files under a workspace, upsert all, optionally prune
    DB rows whose source file is gone. Caller commits."""
    entries = list_entries(entity_id, workspace_id)
    seen_ids: set[str] = set()

    for e in entries:
        await sync_entry(db, e, embed=embed)
        seen_ids.add(e.frontmatter.id)

    pruned = 0
    if prune:
        rows = list((await db.execute(
            select(AgentMemory).where(
                AgentMemory.entity_id == entity_id,
                AgentMemory.workspace_id == workspace_id,
            )
        )).scalars().all())
        for r in rows:
            if r.id not in seen_ids and r.scope is not None:
                # Only prune rows that came from the workspace memory
                # subsystem (have ``scope`` set). Legacy per-agent
                # memories have scope=None and stay untouched.
                await db.delete(r)
                pruned += 1

    await db.flush()
    return {
        "synced": len(entries),
        "pruned": pruned,
        "workspace_id": workspace_id,
    }


# ── Helpers ───────────────────────────────────────────────────────────

def _build_metadata(entry: MemoryEntry) -> dict:
    fm = entry.frontmatter
    metadata = {
        "title": fm.title,
        "tags": list(fm.tags),
        "applies_to": fm.applies_to.model_dump(exclude_none=False),
        "file_path": entry.file_path,
        "slug": entry.slug,
        "mtime": entry.mtime,
        "created_at": fm.created_at.isoformat(),
        "updated_at": (fm.updated_at or fm.created_at).isoformat(),
    }
    for key in ("original_body_chars", "body_truncated_for_memory_store"):
        if hasattr(fm, key):
            metadata[key] = getattr(fm, key)
    body_chars = len((entry.body or "").strip())
    if body_chars > _SYNCED_WORKSPACE_MEMORY_CONTENT_MAX_CHARS:
        metadata.setdefault("original_body_chars", body_chars)
        metadata["body_truncated_for_memory_store"] = True
    return metadata


async def _embed_into_row(
    db: AsyncSession, row: AgentMemory, entry: MemoryEntry,
) -> None:
    """Compute embedding for ``title + body`` and store via raw SQL.

    The ``embedding`` column lives outside the ORM (pgvector type), so
    we use raw SQL to write — same pattern Document indexing uses."""
    try:
        from packages.core.services.embedding_service import generate_embedding

        text_to_embed = f"{entry.frontmatter.title}\n\n{row.content}"
        vec = await generate_embedding(text_to_embed)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "memory sync: embedding failed for %s (%s) — row stored without embedding",
            entry.slug, exc,
        )
        return

    if not vec:
        return

    has_embedding = await db.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'agent_memories'
          AND column_name = 'embedding'
        LIMIT 1
    """))
    if has_embedding.scalar_one_or_none() is None:
        logger.warning("memory sync: agent_memories.embedding column missing — skipping embedding store")
        return

    vec_str = "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
    await db.execute(
        text("UPDATE agent_memories SET embedding = CAST(:v AS vector) WHERE id = :id"),
        {"v": vec_str, "id": row.id},
    )
