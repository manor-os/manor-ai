"""
Wiki link resolver — resolves [[Obsidian-style links]] to actual file paths.

Supports:
  [[Page Name]]                → find Page Name.md anywhere in entity
  [[folder/Page Name]]         → relative path to Page Name.md
  [[Page Name|display text]]   → same resolution, display text ignored

Used by:
  - Lint operation (find broken links, orphaned pages)
  - Frontend (render clickable links)
  - AI agents (cross-reference knowledge)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from packages.core.config import get_settings
from packages.core.services.knowledge_visibility import (
    is_user_visible_folder_path,
    is_user_visible_path,
    normalize_rel_path,
)

logger = logging.getLogger(__name__)

# [[link target]] or [[link target|display text]]
WIKI_LINK_PATTERN = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]*))?\]\]')


def extract_wiki_links(content: str) -> list[tuple[str, Optional[str]]]:
    """
    Extract wiki links from markdown content.
    Returns list of (target, display_text) tuples.
    """
    return [(m.group(1).strip(), m.group(2).strip() if m.group(2) else None)
            for m in WIKI_LINK_PATTERN.finditer(content)]


def list_wiki_pages(entity_id: str) -> list[str]:
    """Return every user-visible markdown path that can participate in link resolution."""
    return [rel for rel, _ in (_iter_visible_markdown_files(entity_id) or [])]


def build_file_index(
    entity_id: str,
    allowed_paths: set[str] | None = None,
) -> dict[str, str]:
    """
    Build a lookup index: {lowercase_name → relative_path} for all .md files.
    Used for case-insensitive link resolution.
    """
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(root):
        return {}

    has_path_filter = allowed_paths is not None
    normalized_allowed_paths = {
        normalize_rel_path(path)
        for path in (allowed_paths or set())
        if normalize_rel_path(path)
    }
    index: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        dirnames[:] = [
            d for d in dirnames
            if is_user_visible_folder_path(os.path.join(rel_dir, d))
        ]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(dirpath, fname)
            rel = normalize_rel_path(os.path.relpath(full, root))
            if not is_user_visible_path(rel):
                continue
            if has_path_filter and rel not in normalized_allowed_paths:
                continue
            name_no_ext = rel[:-3]
            index[name_no_ext.lower()] = rel
            basename = os.path.splitext(fname)[0]
            if basename.lower() not in index:
                index[basename.lower()] = rel

    return index


def resolve_link(
    target: str,
    entity_id: str,
    file_index: dict[str, str] | None = None,
    allowed_paths: set[str] | None = None,
) -> Optional[str]:
    """
    Resolve a wiki link target to an actual file path.
    Returns relative path from entity root, or None.
    """
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    normalized_allowed_paths = None
    if allowed_paths is not None:
        normalized_allowed_paths = {
            normalize_rel_path(path)
            for path in allowed_paths
            if normalize_rel_path(path)
        }

    def _is_allowed(rel: str) -> bool:
        return normalized_allowed_paths is None or rel in normalized_allowed_paths

    target_clean = target.strip()
    if not target_clean.endswith(".md"):
        target_clean_md = target_clean + ".md"
    else:
        target_clean_md = target_clean

    exact = os.path.join(root, target_clean_md)
    if os.path.isfile(exact):
        rel = normalize_rel_path(os.path.relpath(exact, root))
        if is_user_visible_path(rel) and _is_allowed(rel):
            return rel

    exact_raw = os.path.join(root, target_clean)
    if os.path.isfile(exact_raw):
        rel = normalize_rel_path(os.path.relpath(exact_raw, root))
        if is_user_visible_path(rel) and _is_allowed(rel):
            return rel

    if file_index is None:
        file_index = build_file_index(entity_id, allowed_paths=allowed_paths)

    key = target_clean.lower()
    if key.endswith(".md"):
        key = key[:-3]
    if key in file_index:
        resolved = file_index[key]
        return resolved if _is_allowed(resolved) else None

    return None


def find_broken_links(entity_id: str) -> list[dict[str, str]]:
    """Find all broken [[wiki links]] across an entity's filesystem."""
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(root):
        return []

    file_index = build_file_index(entity_id)
    broken = []

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        dirnames[:] = [
            d for d in dirnames
            if is_user_visible_folder_path(os.path.join(rel_dir, d))
        ]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(dirpath, fname)
            rel = normalize_rel_path(os.path.relpath(full, root))
            if not is_user_visible_path(rel):
                continue
            try:
                with open(full) as f:
                    content = f.read()
            except Exception:
                continue

            for link_target, _ in extract_wiki_links(content):
                if resolve_link(link_target, entity_id, file_index) is None:
                    broken.append({"file": rel, "link": link_target})

    return broken


def find_orphaned_pages(entity_id: str) -> list[str]:
    """Find .md pages that no other page links to (orphans)."""
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(root):
        return []

    all_pages: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        dirnames[:] = [
            d for d in dirnames
            if is_user_visible_folder_path(os.path.join(rel_dir, d))
        ]
        for fname in filenames:
            if fname.endswith(".md") and fname not in ("MANOR.md", "index.md", "log.md"):
                rel = normalize_rel_path(os.path.relpath(os.path.join(dirpath, fname), root))
                if is_user_visible_path(rel):
                    all_pages.add(rel)

    file_index = build_file_index(entity_id)
    linked: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        dirnames[:] = [
            d for d in dirnames
            if is_user_visible_folder_path(os.path.join(rel_dir, d))
        ]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(dirpath, fname)
            rel = normalize_rel_path(os.path.relpath(full, root))
            if not is_user_visible_path(rel):
                continue
            try:
                with open(full) as f:
                    content = f.read()
            except Exception:
                continue
            for link_target, _ in extract_wiki_links(content):
                resolved = resolve_link(link_target, entity_id, file_index)
                if resolved:
                    linked.add(resolved)

    return sorted(all_pages - linked)


def _iter_visible_markdown_files(entity_id: str):
    """Yield (relative_path, absolute_path) for user-visible markdown pages."""
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(root):
        return

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
        dirnames[:] = [
            d for d in dirnames
            if is_user_visible_folder_path(os.path.join(rel_dir, d))
        ]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = os.path.join(dirpath, fname)
            rel = normalize_rel_path(os.path.relpath(full, root))
            if is_user_visible_path(rel):
                yield rel, full


def lint_entity(entity_id: str) -> dict[str, list]:
    """
    Run health check on an entity's knowledge base.
    Returns {"broken_links": [...], "orphaned_pages": [...], "unprocessed_files": [...]}.
    """
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)

    broken = find_broken_links(entity_id)
    orphans = find_orphaned_pages(entity_id)

    unprocessed = []
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = normalize_rel_path(os.path.relpath(dirpath, root))
            dirnames[:] = [
                d for d in dirnames
                if is_user_visible_folder_path(os.path.join(rel_dir, d))
            ]
            md_files = {f for f in filenames if f.endswith(".md")}
            for fname in filenames:
                if fname.startswith(".") or fname.endswith(".md"):
                    continue
                base = os.path.splitext(fname)[0]
                has_page = any(md.lower().startswith(base.lower()) for md in md_files)
                if not has_page:
                    rel = normalize_rel_path(os.path.relpath(os.path.join(dirpath, fname), root))
                    if is_user_visible_path(rel):
                        unprocessed.append(rel)

    return {
        "broken_links": broken,
        "orphaned_pages": orphans,
        "unprocessed_files": unprocessed[:50],
    }
