"""High-level memory access for Strategist / Planner / agent tools.

Two patterns covered:
  * ``get_relevant_memory(workspace_id, query, k=10)`` — pgvector
    cosine search; returns top-K MemoryEntry-equivalent dicts ready to
    drop into an LLM context window.
  * ``record_memory(...)`` — Strategist/Planner record a learning;
    writes the MD file (source of truth) and syncs the DB row in the
    same call. Returns the resulting MemoryEntry.

For agents that just want "give me everything as a single text block to
inject into the system prompt", ``load_memory_block`` does the formatting.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.memory.frontmatter import (
    MemoryScope,
    stub_frontmatter,
)
from packages.core.memory.repo import (
    MemoryEntry,
    write_entry,
)
from packages.core.memory.sync import sync_entry
from packages.core.models.base import generate_ulid

logger = logging.getLogger(__name__)

_WORKSPACE_MEMORY_STORE_HARD_MAX_CHARS = 40_000
_WORKSPACE_MEMORY_PROMPT_ITEM_MAX_CHARS = 1400
_WORKSPACE_MEMORY_PROMPT_MIN_BODY_CHARS = 180


def _coerce_memory_body(body: object) -> str:
    text = "" if body is None else str(body)
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


def _prepare_body_for_store(body: object) -> tuple[str, dict[str, Any]]:
    text = _coerce_memory_body(body)
    original_chars = len(text)
    if original_chars <= _WORKSPACE_MEMORY_STORE_HARD_MAX_CHARS:
        return text, {}
    return (
        _truncate_middle(
            text,
            _WORKSPACE_MEMORY_STORE_HARD_MAX_CHARS,
            label="workspace memory store hard cap",
        ),
        {
            "original_body_chars": original_chars,
            "body_truncated_for_memory_store": True,
        },
    )


def _compact_body_for_prompt(body: object, max_chars: int) -> str:
    text = _coerce_memory_body(body)
    if max_chars <= 0:
        return ""
    return _truncate_middle(text, max_chars, label="prompt budget")


def _joined_len(parts: list[str]) -> int:
    return sum(len(part) for part in parts)


# ── Retrieval ─────────────────────────────────────────────────────────

async def get_relevant_memory(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    query: str,
    k: int = 10,
    scope: Optional[MemoryScope | list[MemoryScope]] = None,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Return top-K most relevant memory entries for ``query``.

    Falls back to "most recent active entries" when the query embedding
    can't be generated (no embedding service / empty query). Useful for
    Strategist's first review when there's no specific question.

    Filters expired entries automatically.
    """
    scopes_filter: Optional[list[str]] = None
    if isinstance(scope, str):
        scopes_filter = [scope]
    elif scope:
        scopes_filter = list(scope)

    # Generate embedding with a short timeout so we never hold a DB
    # session waiting on a slow / unreachable embedding service. On
    # timeout / failure we fall through to the recency search below.
    embedding = None
    if query:
        try:
            from packages.core.services.embedding_service import generate_embedding
            embedding = await asyncio.wait_for(generate_embedding(query), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("memory query embedding timed out — falling back to recency")
        except Exception as exc:
            logger.debug("memory query embedding failed (%s) — falling back to recency", exc)
        if not embedding:
            embedding = None

    if embedding:
        if not await _agent_memory_embedding_available(db):
            logger.warning("agent_memories.embedding missing — falling back to recency memory search")
            return await _recency_search(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                k=k,
                scopes_filter=scopes_filter,
                min_confidence=min_confidence,
            )
        return await _vector_search(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            embedding=embedding,
            k=k,
            scopes_filter=scopes_filter,
            min_confidence=min_confidence,
        )
    return await _recency_search(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        k=k,
        scopes_filter=scopes_filter,
        min_confidence=min_confidence,
    )


async def _agent_memory_embedding_available(db: AsyncSession) -> bool:
    result = await db.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'agent_memories'
          AND column_name = 'embedding'
        LIMIT 1
    """))
    return result.scalar_one_or_none() is not None


async def _vector_search(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    embedding: list[float],
    k: int,
    scopes_filter: Optional[list[str]],
    min_confidence: float,
) -> list[dict[str, Any]]:
    vec_str = "[" + ",".join(f"{v:.7f}" for v in embedding) + "]"
    sql = """
        SELECT id, scope, content, importance, confidence, source, metadata,
               status, expires_at, created_at, updated_at,
               1 - (embedding <=> CAST(:vec AS vector)) AS similarity
        FROM agent_memories
        WHERE entity_id = :entity
          AND workspace_id = :ws
          AND status = 'active'
          AND confidence >= :min_conf
          AND embedding IS NOT NULL
          AND (expires_at IS NULL OR expires_at > NOW())
    """
    params: dict[str, Any] = {
        "vec": vec_str, "entity": entity_id, "ws": workspace_id,
        "min_conf": min_confidence,
    }
    if scopes_filter:
        sql += " AND scope = ANY(:scopes)"
        params["scopes"] = scopes_filter
    sql += " ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :k"
    params["k"] = k

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


async def _recency_search(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    k: int,
    scopes_filter: Optional[list[str]],
    min_confidence: float,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, scope, content, importance, confidence, source, metadata,
               status, expires_at, created_at, updated_at,
               NULL::float AS similarity
        FROM agent_memories
        WHERE entity_id = :entity
          AND workspace_id = :ws
          AND status = 'active'
          AND confidence >= :min_conf
          AND (expires_at IS NULL OR expires_at > NOW())
    """
    params: dict[str, Any] = {
        "entity": entity_id, "ws": workspace_id, "min_conf": min_confidence,
    }
    if scopes_filter:
        sql += " AND scope = ANY(:scopes)"
        params["scopes"] = scopes_filter
    sql += " ORDER BY importance DESC, COALESCE(updated_at, created_at) DESC LIMIT :k"
    params["k"] = k

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict[str, Any]:
    meta = row["metadata"] or {}
    return {
        "id": row["id"],
        "scope": row["scope"],
        "title": meta.get("title"),
        "tags": meta.get("tags", []),
        "applies_to": meta.get("applies_to", {}),
        "content": row["content"],
        "importance": row["importance"],
        "confidence": float(row["confidence"]) if row["confidence"] is not None else 1.0,
        "source": row["source"],
        "file_path": meta.get("file_path"),
        "similarity": float(row["similarity"]) if row.get("similarity") is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# ── Recording ─────────────────────────────────────────────────────────

async def record_memory(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scope: MemoryScope,
    title: str,
    body: str,
    tags: Optional[list[str]] = None,
    confidence: float = 1.0,
    source: Optional[str] = None,
    slug: Optional[str] = None,
    importance: int = 5,
    mirror_to_canonical: bool = True,
) -> MemoryEntry:
    """Write a memory note, sync its DB row, and mirror durable docs.

    The scoped note remains the searchable/indexed source. The canonical
    Markdown mirror gives Strategist and workspace chat a predictable,
    human-readable operating-memory surface for future loops.
    """
    prepared_body, body_meta = _prepare_body_for_store(body)
    fm = stub_frontmatter(
        id_=generate_ulid(),
        title=title,
        scope=scope,
        confidence=confidence,
        source=source,
        tags=tags or [],
    )
    fm = fm.model_copy(update={"importance": importance, **body_meta})

    entry = write_entry(
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope=scope,
        slug=slug,
        frontmatter=fm,
        body=prepared_body,
    )
    await sync_entry(db, entry, embed=True)
    if mirror_to_canonical:
        _mirror_entry_to_canonical(entry)
    return entry


def _mirror_entry_to_canonical(entry: MemoryEntry) -> dict[str, object] | None:
    try:
        from packages.core.memory.canonical import append_workspace_memory_block

        filename = _canonical_filename_for_entry(entry)
        marker = f"runtime-learning:{entry.frontmatter.id}"
        block = _canonical_block_for_entry(entry, marker=marker)
        return append_workspace_memory_block(
            entry.entity_id,
            entry.workspace_id,
            filename,
            block,
            marker=marker,
        )
    except Exception:
        logger.debug("canonical workspace memory mirror skipped", exc_info=True)
        return None


def _canonical_filename_for_entry(entry: MemoryEntry) -> str:
    scope = str(entry.scope or "").strip().lower()
    title = entry.frontmatter.title or ""
    body = entry.body or ""
    tags = [str(tag or "").lower() for tag in (entry.frontmatter.tags or [])]
    text = f"{title}\n{body}".lower()
    if scope == "learning":
        return "LEARNINGS.md"
    if scope == "guidance":
        return "RULES.md"
    if _looks_like_guardrail(text, tags):
        return "RULES.md"
    return "MEMORY.md"


def _looks_like_guardrail(text: str, tags: list[str]) -> bool:
    if {"approval", "hitl", "guardrail", "rule"} & set(tags):
        return True
    return any(token in text for token in [
        "必须",
        "不要",
        "不能",
        "不准",
        "审核",
        "批准",
        "同意",
        "must",
        "never",
        "do not",
        "approval",
        "approve",
        "permission",
        "guardrail",
    ])


def _canonical_block_for_entry(entry: MemoryEntry, *, marker: str) -> str:
    fm = entry.frontmatter
    body = _compact_body_for_prompt(entry.body, 1200)
    tags = ", ".join(fm.tags or [])
    lines = [
        f"<!-- {marker} -->",
        f"## Runtime Learning: {fm.title}",
        f"- Status: mirrored from workspace memory `{fm.id}`.",
        f"- Scope: {entry.scope}.",
        f"- Confidence: {float(fm.confidence):.2f}.",
    ]
    if fm.source:
        lines.append(f"- Source: {fm.source}.")
    if tags:
        lines.append(f"- Tags: {tags}.")
    if body:
        lines.append(f"- Workspace memory: {body}")
    lines.append("<!-- /runtime-learning -->")
    return "\n".join(lines)


# ── Block-format helper ───────────────────────────────────────────────

def load_memory_block(entries: list[dict[str, Any]], *, max_chars: int = 8000) -> str:
    """Format retrieved memory entries into a single text block ready to
    paste into an LLM system prompt.

    Both the overall block and each individual memory are bounded so one
    oversized note cannot crowd out the rest of the workspace context.
    """
    if not entries:
        return ""

    lines: list[str] = ["## Workspace Memory\n"]
    omitted = 0
    for idx, e in enumerate(entries):
        title = e.get("title") or "(untitled)"
        scope = e.get("scope") or "?"
        conf = e.get("confidence", 1.0)
        heading = f"\n### [{scope}] {title}  (confidence={conf:.2f})\n"
        remaining = max_chars - _joined_len(lines) - len(heading) - 1
        if remaining < _WORKSPACE_MEMORY_PROMPT_MIN_BODY_CHARS:
            omitted = len(entries) - idx
            break
        body_limit = min(_WORKSPACE_MEMORY_PROMPT_ITEM_MAX_CHARS, remaining)
        body = _compact_body_for_prompt(e.get("content") or "", body_limit)
        if not body:
            continue
        chunk = f"{heading}{body}\n"
        if _joined_len(lines) + len(chunk) > max_chars:
            omitted = len(entries) - idx
            break
        lines.append(chunk)

    if omitted:
        note = f"\n_(truncated; {omitted} more entries omitted due to context budget)_\n"
        remaining = max_chars - _joined_len(lines)
        if remaining >= len(note):
            lines.append(note)
        elif remaining > 32:
            lines.append(_truncate_middle(note, remaining, label="prompt budget"))
    return "".join(lines)
