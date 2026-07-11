"""Workspace memory — Markdown files as the canonical store.

Layout per workspace under the entity filesystem:

  {entity_root}/.ai/workspaces/{workspace_id}/memory/
    index.md                    auto-maintained TOC
    guidance/                   durable how-we-work notes (SKILL.md role)
    decisions/                  explicit decisions worth recalling
    learnings/                  outcomes from past plans (Strategist-written)
    facts/                      factual observations
    preferences/                user preferences

Each MD file has YAML frontmatter + body. The file is the source of
truth — the user can edit with any editor, ``git`` versions it, and
machine writes round-trip cleanly.

The ``agent_memories`` DB table is a derived index: one row per file,
carrying the embedding (pgvector) for fast similarity search and a
cached copy of the body for cheap LLM-context injection. ``sync``
walks the filesystem and upserts rows; ``service.get_relevant_memory``
queries the index.

Inspired by:
  * Anthropic Skills (SKILL.md frontmatter pattern)
  * Claude Code's CLAUDE.md (layered context loading)
  * Obsidian / Foam / Dendron (markdown wiki with [[wiki-links]])
"""
from packages.core.memory.frontmatter import (
    Frontmatter,
    MemoryScope,
    parse_md,
    serialize_md,
)
from packages.core.memory.paths import workspace_memory_root, scope_dir, file_path_for
from packages.core.memory.repo import (
    MemoryEntry,
    list_entries,
    read_entry,
    write_entry,
    delete_entry,
)
from packages.core.memory.sync import sync_workspace, sync_entry
from packages.core.memory.service import (
    get_relevant_memory,
    record_memory,
    load_memory_block,
)
from packages.core.memory.canonical import (
    WORKSPACE_MEMORY_FILES,
    WORKSPACE_AGENT_MEMORY_FILES,
    append_workspace_agent_memory_block,
    append_workspace_memory_block,
    ensure_workspace_memory_docs,
    ensure_workspace_agent_memory_docs,
    load_workspace_operating_memory,
    load_workspace_agent_memory,
    read_workspace_agent_memory_file,
    read_workspace_memory_file,
    write_workspace_agent_memory_file,
    write_workspace_memory_file,
)

__all__ = [
    "Frontmatter",
    "MemoryScope",
    "parse_md",
    "serialize_md",
    "workspace_memory_root",
    "scope_dir",
    "file_path_for",
    "MemoryEntry",
    "list_entries",
    "read_entry",
    "write_entry",
    "delete_entry",
    "sync_workspace",
    "sync_entry",
    "get_relevant_memory",
    "record_memory",
    "load_memory_block",
    "WORKSPACE_MEMORY_FILES",
    "WORKSPACE_AGENT_MEMORY_FILES",
    "append_workspace_agent_memory_block",
    "append_workspace_memory_block",
    "ensure_workspace_memory_docs",
    "ensure_workspace_agent_memory_docs",
    "load_workspace_operating_memory",
    "load_workspace_agent_memory",
    "read_workspace_agent_memory_file",
    "read_workspace_memory_file",
    "write_workspace_agent_memory_file",
    "write_workspace_memory_file",
]
