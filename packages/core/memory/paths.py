"""Filesystem path resolution for workspace memory.

Sits above ``packages.core.services.entity_fs`` to follow the existing
``.ai/`` convention for AI-managed files inside an entity's filesystem,
and adds a ``workspaces/{workspace_id}/memory/`` subtree per workspace.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from packages.core.memory.frontmatter import MemoryScope, scope_to_dirname
from packages.core.services.entity_fs import get_entity_root


_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe_segment(s: str) -> str:
    """Tighten a string into a single safe path segment, **case-preserving**.

    ULIDs are case-sensitive (Crockford base32 — uppercase by convention)
    so we strip dangerous chars but never lowercase. Slug normalisation
    for filename-derived paths happens in ``title_to_slug``.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("path segment cannot be empty")
    s = _SAFE_RE.sub("-", s)
    s = s.strip("-")
    return s or "untitled"


def workspace_memory_root(entity_id: str, workspace_id: str) -> str:
    """``.../{entity}/.ai/workspaces/{workspace}/memory``."""
    return os.path.join(
        get_entity_root(entity_id),
        ".ai", "workspaces",
        _safe_segment(workspace_id),
        "memory",
    )


def scope_dir(entity_id: str, workspace_id: str, scope: MemoryScope) -> str:
    return os.path.join(
        workspace_memory_root(entity_id, workspace_id),
        scope_to_dirname(scope),
    )


def file_path_for(
    entity_id: str,
    workspace_id: str,
    scope: MemoryScope,
    slug: str,
) -> str:
    """Canonical filesystem path for a memory entry."""
    return os.path.join(
        scope_dir(entity_id, workspace_id, scope),
        f"{_safe_segment(slug)}.md",
    )


def slug_from_path(path: str) -> str:
    base = os.path.basename(path)
    if base.endswith(".md"):
        base = base[:-3]
    return base


def title_to_slug(title: str) -> str:
    """Best-effort filename slug. Lowercase + safe chars only."""
    return _safe_segment(title).lower() or "untitled"


def parse_workspace_memory_path(path: str) -> Optional[tuple[str, MemoryScope, str]]:
    """Reverse-parse a memory file path into (workspace_id, scope, slug).

    Returns None for paths that don't sit under a recognised
    ``.ai/workspaces/<ws>/memory/<scope_dir>/`` layout — protects callers
    from accidentally indexing arbitrary files.
    """
    parts = path.split(os.sep)
    try:
        i = parts.index(".ai")
    except ValueError:
        return None
    if len(parts) <= i + 5:
        return None
    if parts[i + 1] != "workspaces" or parts[i + 3] != "memory":
        return None

    workspace_id = parts[i + 2]
    from packages.core.memory.frontmatter import dirname_to_scope
    scope = dirname_to_scope(parts[i + 4])
    if scope is None:
        return None
    slug = slug_from_path(parts[-1])
    return workspace_id, scope, slug
