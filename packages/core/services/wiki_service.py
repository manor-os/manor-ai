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


def build_file_index(entity_id: str) -> dict[str, str]:
    """
    Build a lookup index: {lowercase_name → relative_path} for all .md files.
    Used for case-insensitive link resolution.
    """
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(root):
        return {}

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
            name_no_ext = rel[:-3]
            index[name_no_ext.lower()] = rel
            basename = os.path.splitext(fname)[0]
            if basename.lower() not in index:
                index[basename.lower()] = rel

    return index


def resolve_link(target: str, entity_id: str, file_index: dict[str, str] | None = None) -> Optional[str]:
    """
    Resolve a wiki link target to an actual file path.
    Returns relative path from entity root, or None.
    """
    root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)

    target_clean = target.strip()
    if not target_clean.endswith(".md"):
        target_clean_md = target_clean + ".md"
    else:
        target_clean_md = target_clean

    exact = os.path.join(root, target_clean_md)
    if os.path.isfile(exact):
        rel = normalize_rel_path(os.path.relpath(exact, root))
        return rel if is_user_visible_path(rel) else None

    exact_raw = os.path.join(root, target_clean)
    if os.path.isfile(exact_raw):
        rel = normalize_rel_path(os.path.relpath(exact_raw, root))
        return rel if is_user_visible_path(rel) else None

    if file_index is None:
        file_index = build_file_index(entity_id)

    key = target_clean.lower()
    if key.endswith(".md"):
        key = key[:-3]
    if key in file_index:
        return file_index[key]

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


def build_wiki_graph(entity_id: str, allowed_paths: set[str] | None = None) -> dict[str, object]:
    """
    Build a lightweight wiki graph for UI/agent navigation.

    Pages are regular markdown files. Links are Obsidian-style [[Page]] links.
    The graph intentionally ignores hidden/runtime paths so user-facing wiki
    search cannot accidentally expose agent internals.
    """
    has_path_filter = allowed_paths is not None
    normalized_allowed_paths = {
        normalize_rel_path(path)
        for path in (allowed_paths or set())
        if normalize_rel_path(path)
    }
    file_index = build_file_index(entity_id)
    pages: dict[str, dict[str, object]] = {}
    missing_by_target: dict[str, dict[str, object]] = {}
    seen_physical_files: set[tuple[int, int]] = set()
    link_count = 0

    for rel, full in _iter_visible_markdown_files(entity_id) or []:
        if has_path_filter and rel not in normalized_allowed_paths:
            continue
        try:
            stat = os.stat(full)
            physical_key = (stat.st_dev, stat.st_ino)
        except OSError:
            physical_key = None
        if physical_key is not None:
            if physical_key in seen_physical_files:
                continue
            seen_physical_files.add(physical_key)

        title = os.path.splitext(os.path.basename(rel))[0]
        page = {
            "path": rel,
            "title": title,
            "links": [],
            "backlinks": [],
        }
        pages[rel] = page
        try:
            with open(full) as f:
                content = f.read()
        except Exception:
            content = ""

        for target, display in extract_wiki_links(content):
            link_count += 1
            resolved_path = resolve_link(target, entity_id, file_index)
            link = {
                "target": target,
                "display": display,
                "resolved_path": resolved_path,
                "exists": resolved_path is not None,
            }
            page["links"].append(link)
            if resolved_path and resolved_path in pages:
                pages[resolved_path]["backlinks"].append({"source_path": rel, "source_title": title})
            elif not resolved_path:
                row = missing_by_target.setdefault(target, {"target": target, "count": 0, "sources": []})
                row["count"] = int(row["count"]) + 1
                row["sources"].append({"path": rel, "title": title})

    # A page may be linked before it is visited in os.walk order. Fill those
    # backlinks after every page has been discovered.
    for page in pages.values():
        for link in page["links"]:
            resolved_path = link.get("resolved_path")
            if resolved_path and resolved_path in pages:
                backlink = {"source_path": page["path"], "source_title": page["title"]}
                backlinks = pages[resolved_path]["backlinks"]
                if backlink not in backlinks:
                    backlinks.append(backlink)

    orphaned_pages = [
        path
        for path, page in pages.items()
        if not page["backlinks"] and os.path.basename(path) not in ("MANOR.md", "index.md", "log.md")
    ]

    return {
        "pages": sorted(pages.values(), key=lambda page: str(page["title"]).lower()),
        "missing_links": sorted(missing_by_target.values(), key=lambda row: (-int(row["count"]), str(row["target"]).lower())),
        "orphaned_pages": sorted(orphaned_pages),
        "page_count": len(pages),
        "link_count": link_count,
        "missing_count": len(missing_by_target),
        "orphaned_count": len(orphaned_pages),
    }


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
