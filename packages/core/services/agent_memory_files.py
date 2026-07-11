"""Markdown-backed storage for agent memories.

The database row remains the query/index surface, but the Markdown file is
kept in sync so agent memories can be edited and loaded like workspace memory.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.memory import AgentMemory
from packages.core.services.agent_files import effective_agent_id
from packages.core.services.entity_fs import (
    get_agent_dir,
    get_entity_root,
    is_fs_enabled,
    provision_agent_workspace,
)


_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)
_AGENT_MEMORY_FILE_SYNC_HARD_MAX_CHARS = 20_000


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


def _prepare_file_synced_content(body: str, metadata: dict | None) -> tuple[str, dict]:
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    meta = dict(metadata or {})
    original_chars = len(text)
    if original_chars > _AGENT_MEMORY_FILE_SYNC_HARD_MAX_CHARS:
        text = _truncate_middle(
            text,
            _AGENT_MEMORY_FILE_SYNC_HARD_MAX_CHARS,
            label="memory file sync hard cap",
        )
        meta.setdefault("original_content_chars", original_chars)
        meta["content_truncated_for_memory_store"] = True
    return text, meta


def _safe_segment(value: str) -> str:
    value = (value or "").strip()
    value = _SAFE_RE.sub("-", value)
    value = value.strip("-")
    return value or "untitled"


def _scope_dir(memory_type: str) -> str:
    mapping = {
        "fact": "facts",
        "preference": "preferences",
        "context": "contexts",
        "instruction": "instructions",
    }
    return mapping.get(memory_type or "fact", _safe_segment(memory_type or "fact"))


def agent_memory_root(entity_id: str, agent_id: str | None, user_id: str | None = None) -> str:
    aid = effective_agent_id(agent_id)
    provision_agent_workspace(entity_id, aid)
    root = os.path.join(get_agent_dir(entity_id, aid), "memory")
    if user_id:
        root = os.path.join(root, "users", _safe_segment(user_id))
    return root


def memory_file_path(
    entity_id: str,
    agent_id: str | None,
    memory_id: str,
    memory_type: str,
    *,
    user_id: str | None = None,
) -> str:
    return os.path.join(
        agent_memory_root(entity_id, agent_id, user_id=user_id),
        _scope_dir(memory_type),
        f"{_safe_segment(memory_id)}.md",
    )


def write_memory_file(mem: AgentMemory) -> str:
    """Write/update the Markdown representation for an AgentMemory row."""
    if not is_fs_enabled():
        return ""
    path = memory_file_path(
        mem.entity_id,
        mem.agent_id,
        mem.id,
        mem.memory_type,
        user_id=mem.user_id,
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)

    metadata = dict(mem.metadata_ or {})
    metadata["agent_memory_file"] = True
    metadata["visibility"] = "internal"

    fm = {
        "id": mem.id,
        "memory_type": mem.memory_type,
        "agent_id": mem.agent_id,
        "user_id": mem.user_id,
        "importance": mem.importance,
        "source": mem.source,
        "metadata": metadata,
        "expires_at": mem.expires_at.isoformat() if mem.expires_at else None,
        "status": mem.status,
        "created_at": mem.created_at.isoformat() if mem.created_at else datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    yaml_text = yaml.safe_dump(
        {k: v for k, v in fm.items() if v is not None},
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    text = f"---\n{yaml_text}\n---\n\n{(mem.content or '').rstrip()}\n"

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def delete_memory_file(
    entity_id: str,
    agent_id: str | None,
    memory_id: str,
    memory_type: str,
    *,
    user_id: str | None = None,
) -> bool:
    if not is_fs_enabled():
        return False
    path = memory_file_path(entity_id, agent_id, memory_id, memory_type, user_id=user_id)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def maybe_move_memory_file(mem: AgentMemory, old_type: Optional[str]) -> None:
    """Move files when memory_type changes, then write the fresh body."""
    if old_type and old_type != mem.memory_type:
        delete_memory_file(
            mem.entity_id,
            mem.agent_id,
            mem.id,
            old_type,
            user_id=mem.user_id,
        )
    write_memory_file(mem)


async def sync_agent_memory_files(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """Walk agent memory MD files and upsert rows into agent_memories."""
    if not is_fs_enabled():
        return 0

    synced = 0
    for root, root_agent_id, root_user_id in _memory_roots_to_sync(
        entity_id,
        agent_id=agent_id,
        user_id=user_id,
    ):
        if not os.path.isdir(root):
            continue
        synced += await _sync_memory_root(
            db,
            entity_id=entity_id,
            root=root,
            agent_id=root_agent_id,
            user_id=root_user_id,
        )

    await db.flush()
    return synced


def _memory_roots_to_sync(
    entity_id: str,
    *,
    agent_id: str | None,
    user_id: str | None,
) -> list[tuple[str, str | None, str | None]]:
    """Return agent memory roots to scan.

    A specific agent/user request only scans that scope. A broad list request
    scans all existing agent memory roots so file-edited memories are indexed.
    """
    if agent_id is not None or user_id is not None:
        return [(agent_memory_root(entity_id, agent_id, user_id=user_id), agent_id, user_id)]

    agents_dir = os.path.join(get_entity_root(entity_id), ".ai", "agents")
    if not os.path.isdir(agents_dir):
        return []

    roots: list[tuple[str, str | None, str | None]] = []
    for name in sorted(os.listdir(agents_dir)):
        agent_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_dir):
            continue
        memory_root = os.path.join(agent_dir, "memory")
        if os.path.isdir(memory_root):
            roots.append((memory_root, name, None))
    return roots


async def _sync_memory_root(
    db: AsyncSession,
    *,
    entity_id: str,
    root: str,
    agent_id: str | None,
    user_id: str | None,
) -> int:
    synced = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(dirpath, fname)
            parsed = _read_memory_md(full)
            if not parsed:
                continue
            fm, body = parsed
            mem_id = str(fm.get("id") or "").strip() or generate_ulid()
            memory_type = str(fm.get("memory_type") or _type_from_dir(dirpath)).strip() or "fact"
            content, metadata = _prepare_file_synced_content(body, fm.get("metadata") or {})

            row = (await db.execute(
                select(AgentMemory).where(
                    AgentMemory.id == mem_id,
                    AgentMemory.entity_id == entity_id,
                )
            )).scalar_one_or_none()
            if row is None:
                row = AgentMemory(
                    id=mem_id,
                    entity_id=entity_id,
                    agent_id=fm.get("agent_id") or agent_id,
                    user_id=fm.get("user_id") or user_id,
                    memory_type=memory_type,
                    content=content,
                    importance=_int_in_range(fm.get("importance"), default=5),
                    source=fm.get("source") or "file_sync",
                    metadata_=metadata,
                    expires_at=_parse_dt(fm.get("expires_at")),
                    status=fm.get("status") or "active",
                )
                db.add(row)
            else:
                row.agent_id = fm.get("agent_id") or agent_id
                row.user_id = fm.get("user_id") or user_id
                row.memory_type = memory_type
                row.content = content
                row.importance = _int_in_range(fm.get("importance"), default=row.importance or 5)
                row.source = fm.get("source") or row.source
                row.metadata_ = metadata
                row.expires_at = _parse_dt(fm.get("expires_at"))
                row.status = fm.get("status") or row.status or "active"
            synced += 1
    return synced


def _read_memory_md(path: str) -> tuple[dict, str] | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group("yaml")) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return fm, m.group("body").lstrip("\n")


def _type_from_dir(path: str) -> str:
    dirname = os.path.basename(path)
    reverse = {
        "facts": "fact",
        "preferences": "preference",
        "contexts": "context",
        "instructions": "instruction",
    }
    return reverse.get(dirname, dirname.rstrip("s") or "fact")


def _int_in_range(value, *, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return min(max(n, 1), 10)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
