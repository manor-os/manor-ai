"""Filesystem CRUD for workspace memory MD files.

This is the file-side mirror of ``service`` (which talks to the DB index).
Single-writer model: a write here followed by ``sync.sync_entry`` keeps
the agent_memories row aligned. Concurrent edits across user-edits and
machine-writes use last-write-wins on file mtime — fine for solo use,
revisit if multi-user.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from packages.core.memory.frontmatter import (
    Frontmatter,
    MemoryScope,
    parse_md,
    serialize_md,
)
from packages.core.memory.paths import (
    file_path_for,
    parse_workspace_memory_path,
    scope_dir,
    title_to_slug,
    workspace_memory_root,
)

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """One memory MD file as Python data."""

    workspace_id: str
    entity_id: str
    scope: MemoryScope
    slug: str
    file_path: str
    frontmatter: Frontmatter
    body: str
    mtime: float
    """File mtime — used by sync to skip unchanged entries."""


# ── Read ──────────────────────────────────────────────────────────────

def read_entry(file_path: str) -> Optional[MemoryEntry]:
    """Read + parse a single MD file. Returns None if path doesn't
    sit under a recognised workspace/scope directory."""
    if not os.path.isfile(file_path):
        return None

    parsed_path = parse_workspace_memory_path(file_path)
    if parsed_path is None:
        logger.debug("skipping %s — not in workspace memory layout", file_path)
        return None
    workspace_id, scope, slug = parsed_path

    with open(file_path, encoding="utf-8") as f:
        text = f.read()

    try:
        fm, body = parse_md(text)
    except ValueError as exc:
        logger.warning("memory MD %s parse failed: %s", file_path, exc)
        return None

    if fm.scope != scope:
        logger.warning(
            "memory MD %s frontmatter scope=%s but directory says %s "
            "— directory wins for indexing",
            file_path, fm.scope, scope,
        )

    # Pull entity_id from the path: .../{entity}/.ai/workspaces/...
    parts = file_path.split(os.sep)
    ai_idx = parts.index(".ai")
    entity_id = parts[ai_idx - 1] if ai_idx > 0 else ""

    return MemoryEntry(
        workspace_id=workspace_id,
        entity_id=entity_id,
        scope=scope,
        slug=slug,
        file_path=file_path,
        frontmatter=fm,
        body=body,
        mtime=os.path.getmtime(file_path),
    )


def list_entries(
    entity_id: str,
    workspace_id: str,
    *,
    scope: Optional[MemoryScope] = None,
    include_archived: bool = False,
) -> list[MemoryEntry]:
    """All memory entries under a workspace. Scope filter optional."""
    root = workspace_memory_root(entity_id, workspace_id)
    if not os.path.isdir(root):
        return []

    out: list[MemoryEntry] = []
    scopes = [scope] if scope else None

    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            if fname == "index.md":
                # The index is auto-generated TOC, not an indexable entry.
                continue
            full = os.path.join(dirpath, fname)
            entry = read_entry(full)
            if entry is None:
                continue
            if scopes and entry.scope not in scopes:
                continue
            if not include_archived and entry.frontmatter.status != "active":
                continue
            out.append(entry)

    out.sort(key=lambda e: (e.scope, e.slug))
    return out


# ── Write ─────────────────────────────────────────────────────────────

def write_entry(
    *,
    entity_id: str,
    workspace_id: str,
    scope: MemoryScope,
    slug: Optional[str],
    frontmatter: Frontmatter,
    body: str,
) -> MemoryEntry:
    """Write or overwrite a memory MD. Returns the freshly-read entry.

    ``slug`` defaults to a slug derived from ``frontmatter.title``.
    Bumps ``frontmatter.updated_at`` to now.
    """
    if frontmatter.scope != scope:
        raise ValueError(
            f"scope arg ({scope}) doesn't match frontmatter.scope ({frontmatter.scope})"
        )

    slug = slug or title_to_slug(frontmatter.title)
    path = file_path_for(entity_id, workspace_id, scope, slug)

    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Bump updated_at — file is being rewritten.
    frontmatter = frontmatter.model_copy(update={
        "updated_at": datetime.now(timezone.utc),
    })

    text = serialize_md(frontmatter, body)
    # Atomic-ish write: temp file + rename.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

    entry = read_entry(path)
    if entry is None:
        # Should not happen — we just wrote a valid file.
        raise RuntimeError(f"failed to re-read written entry at {path}")
    return entry


def delete_entry(
    *,
    entity_id: str,
    workspace_id: str,
    scope: MemoryScope,
    slug: str,
) -> bool:
    path = file_path_for(entity_id, workspace_id, scope, slug)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def ensure_workspace_memory_dirs(
    entity_id: str, workspace_id: str,
) -> str:
    """Create the standard scope dir layout for a fresh workspace.

    Idempotent. Returns the workspace memory root path."""
    root = workspace_memory_root(entity_id, workspace_id)
    os.makedirs(root, exist_ok=True)
    for scope in ("guidance", "decision", "learning", "fact", "preference"):
        os.makedirs(scope_dir(entity_id, workspace_id, scope), exist_ok=True)

    index_path = os.path.join(root, "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(_INDEX_TEMPLATE)
    try:
        from packages.core.memory.canonical import ensure_workspace_memory_docs

        ensure_workspace_memory_docs(entity_id, workspace_id)
    except Exception:
        logger.debug("canonical workspace memory docs skipped", exc_info=True)
    return root


_INDEX_TEMPLATE = """\
# Workspace Memory

Auto-maintained index of how this workspace works.

## Sections

- [Guidance](./guidance/)        durable how-we-work notes
- [Decisions](./decisions/)      explicit decisions worth recalling
- [Learnings](./learnings/)      what past plans taught us
- [Facts](./facts/)              factual observations
- [Preferences](./preferences/)  user preferences to honour

## Canonical operating memory

- [WORKSPACE.md](./WORKSPACE.md)  workspace charter and operating model
- [STATE.md](./STATE.md)          generated current-state cache
- [FILES.md](./FILES.md)          generated workspace file wiki
- [RULES.md](./RULES.md)          durable workspace rules and approvals
- [KNOWLEDGE.md](./KNOWLEDGE.md)  Knowledge map and retrieval policy
- [MEMORY.md](./MEMORY.md)        stable facts, preferences, and decisions
- [LEARNINGS.md](./LEARNINGS.md)  execution learnings and calibration
- [TOOLS.md](./TOOLS.md)          workspace tool and integration guidance
- [RUNBOOKS.md](./RUNBOOKS.md)    repeatable process playbooks
- [AGENTS.md](./AGENTS.md)        workspace agent roster and override policy

The folders above contain small Markdown notes with YAML frontmatter. The
canonical operating memory docs are fixed Markdown files without frontmatter.
Edit either with any editor — the system re-indexes note files on next sync
and loads canonical docs directly at runtime.
"""
