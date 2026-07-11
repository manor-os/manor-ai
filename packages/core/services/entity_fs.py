"""
Entity Filesystem — JuiceFS-backed POSIX filesystem per entity.

Each entity gets a real filesystem at /mnt/manor/{entity_id}/ with:
  - MANOR.md  — AI schema (how AI works in this filesystem)
  - index.md  — AI-maintained master catalog of all knowledge pages
  - log.md    — Chronological record of AI actions
  - .ai/      — Hidden AI workspace (agent memory, skills, temp)
  - Everything else: free-form user content
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone

from packages.core.config import get_settings

logger = logging.getLogger(__name__)


class EntityFilesystemError(RuntimeError):
    """Raised when the entity filesystem cannot safely persist data."""


class EntityFileWriteError(EntityFilesystemError):
    """Raised when a file write cannot be verified after persistence."""

# System files/dirs hidden from user view
SYSTEM_FILES = frozenset({"MANOR.md", "index.md", "log.md"})
SYSTEM_DIRS = frozenset({".ai"})
_POSIX_NAME_MAX_BYTES = 255


def get_entity_root(entity_id: str) -> str:
    """Return the filesystem root for an entity."""
    safe_id = _sanitize_entity_id(entity_id)
    return os.path.join(get_settings().MANOR_FS_ROOT, safe_id)


def _sanitize_entity_id(entity_id: str) -> str:
    """Sanitize entity_id to prevent path traversal."""
    eid = str(entity_id).strip()
    eid = re.sub(r'[/\\]', '_', eid)
    eid = eid.replace('..', '_')
    if not eid or eid in ('.', '..'):
        raise ValueError(f"Invalid entity_id: {entity_id!r}")
    return eid


def _truncate_utf8(value: str, max_bytes: int) -> str:
    data = (value or "").encode("utf-8")
    if len(data) <= max_bytes:
        return value or ""
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _atomic_tmp_path(parent: str, full_path: str) -> str:
    suffix = f".tmp-{os.getpid()}-{time.monotonic_ns()}-{uuid.uuid4().hex}"
    max_prefix_bytes = max(16, _POSIX_NAME_MAX_BYTES - len(suffix.encode("utf-8")))
    prefix = _truncate_utf8(f".{os.path.basename(full_path)}", max_prefix_bytes).rstrip(" .")
    if prefix in {"", "."}:
        prefix = ".entity-file"
    return os.path.join(parent, f"{prefix}{suffix}")


def is_fs_enabled() -> bool:
    """Check if JuiceFS entity filesystem is enabled."""
    return get_settings().MANOR_FS_ENABLED


def assert_entity_filesystem_ready() -> str:
    """Return MANOR_FS_ROOT after validating it is safe for persistent writes.

    In cloud deployments the root must be an actual mount point. This prevents
    workers from silently writing generated media into an ephemeral container
    directory when JuiceFS failed to mount.
    """
    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        raise EntityFilesystemError("Entity filesystem is disabled (MANOR_FS_ENABLED=false)")

    fs_root = os.path.abspath(settings.MANOR_FS_ROOT)
    return fs_root


def write_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    data: bytes,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
) -> str:
    """Atomically write bytes into an entity filesystem and verify the result."""
    assert_entity_filesystem_ready()
    if not allow_empty and not data:
        raise EntityFileWriteError("Refusing to persist an empty generated file")

    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFileWriteError(f"Path traversal not allowed: {rel_path!r}")

    parent = os.path.dirname(full_path)
    os.makedirs(parent, exist_ok=True)

    expected = len(data) if expected_size is None else int(expected_size)
    tmp_path = _atomic_tmp_path(parent, full_path)
    wrote_target = False
    try:
        with open(tmp_path, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp_size = os.path.getsize(tmp_path)
        if tmp_size != expected:
            raise EntityFileWriteError(
                f"Temporary file size mismatch for {safe_rel}: expected {expected}, got {tmp_size}"
            )
        os.replace(tmp_path, full_path)
        wrote_target = True
        actual_size = os.path.getsize(full_path)
        if actual_size != expected:
            raise EntityFileWriteError(
                f"Persisted file size mismatch for {safe_rel}: expected {expected}, got {actual_size}"
            )
        if not os.path.isfile(full_path):
            raise EntityFileWriteError(f"Persisted file missing after write: {safe_rel}")
        return full_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            logger.debug("Failed to remove temporary entity file %s", tmp_path, exc_info=True)
        if wrote_target:
            try:
                if os.path.exists(full_path):
                    os.unlink(full_path)
            except OSError:
                logger.debug("Failed to remove unverified entity file %s", full_path, exc_info=True)
        raise


def copy_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    source_path: str,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
) -> str:
    """Atomically copy an existing local file into an entity filesystem."""
    assert_entity_filesystem_ready()
    if not os.path.isfile(source_path):
        raise EntityFileWriteError(f"Source file does not exist: {source_path}")

    source_size = os.path.getsize(source_path)
    if not allow_empty and source_size <= 0:
        raise EntityFileWriteError("Refusing to persist an empty generated file")
    expected = source_size if expected_size is None else int(expected_size)
    if source_size != expected:
        raise EntityFileWriteError(
            f"Source file size mismatch for {source_path}: expected {expected}, got {source_size}"
        )

    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFileWriteError(f"Path traversal not allowed: {rel_path!r}")

    parent = os.path.dirname(full_path)
    os.makedirs(parent, exist_ok=True)
    tmp_path = _atomic_tmp_path(parent, full_path)
    wrote_target = False
    try:
        with open(source_path, "rb") as src, open(tmp_path, "xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        tmp_size = os.path.getsize(tmp_path)
        if tmp_size != expected:
            raise EntityFileWriteError(
                f"Temporary file size mismatch for {safe_rel}: expected {expected}, got {tmp_size}"
            )
        os.replace(tmp_path, full_path)
        wrote_target = True
        actual_size = os.path.getsize(full_path)
        if actual_size != expected:
            raise EntityFileWriteError(
                f"Persisted file size mismatch for {safe_rel}: expected {expected}, got {actual_size}"
            )
        return full_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            logger.debug("Failed to remove temporary entity file %s", tmp_path, exc_info=True)
        if wrote_target:
            try:
                if os.path.exists(full_path):
                    os.unlink(full_path)
            except OSError:
                logger.debug("Failed to remove unverified entity file %s", full_path, exc_info=True)
        raise


def entity_fs_exists(entity_id: str) -> bool:
    """Check if an entity's filesystem has been provisioned."""
    root = get_entity_root(entity_id)
    return os.path.isdir(root) and os.path.isfile(os.path.join(root, "MANOR.md"))


def is_system_path(name: str) -> bool:
    """Check if a file/dir name is a hidden system file."""
    return name in SYSTEM_FILES or name in SYSTEM_DIRS or name.startswith(".")


def provision_entity_filesystem(entity_id: str, entity_name: str = "") -> str:
    """
    Create the entity's filesystem structure.
    Idempotent — safe to call multiple times.
    Returns the entity root path.
    """
    root = get_entity_root(entity_id)
    display_name = entity_name or f"Entity {entity_id}"

    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "agents"), exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "skills"), exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "temp"), exist_ok=True)

    manor_md_path = os.path.join(root, "MANOR.md")
    if not os.path.exists(manor_md_path):
        with open(manor_md_path, "w") as f:
            f.write(_build_manor_md(display_name))
        logger.info("Created MANOR.md for entity %s", entity_id)

    index_path = os.path.join(root, "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w") as f:
            f.write(
                f"# {display_name} Knowledge Index\n\n"
                f"_Auto-maintained by AI. Upload files or ask AI to create content._\n"
            )

    log_path = os.path.join(root, "log.md")
    if not os.path.exists(log_path):
        with open(log_path, "w") as f:
            f.write(
                f"# Activity Log\n\n"
                f"[PROVISION] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} "
                f"Entity filesystem created for {display_name}\n"
            )

    logger.info("Entity filesystem provisioned: %s → %s", entity_id, root)
    return root


def _sanitize_agent_id(agent_id: str) -> str:
    """Sanitize agent_id to prevent path traversal."""
    return re.sub(r'[/\\]', '_', str(agent_id).strip())


def get_agent_dir(entity_id: str, agent_id: str) -> str:
    """Return the filesystem path for an agent's definition directory."""
    root = get_entity_root(entity_id)
    safe_agent = _sanitize_agent_id(agent_id)
    return os.path.join(root, ".ai", "agents", safe_agent)


def provision_agent_workspace(entity_id: str, agent_id: str) -> str:
    """Create an agent's workspace within the entity filesystem.

    For the master agent (_master), seeds comprehensive platform-level
    defaults. Custom agents get minimal templates to be filled in.
    """
    agent_dir = get_agent_dir(entity_id, agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "memory"), exist_ok=True)

    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    if agent_id == MANOR_AGENT_FS_ID:
        defaults = _MASTER_AGENT_DEFAULTS
    else:
        defaults = _custom_agent_defaults(agent_id)

    for fname, content in defaults:
        fpath = os.path.join(agent_dir, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                f.write(content)

    return agent_dir


# ── Platform defaults for the master agent ──────────────────────────────────

_MASTER_SOUL = """\
# Manor AI

You are **Manor AI**, the primary intelligent assistant for this organization.

## Personality
- Professional yet approachable — adapt tone to the user's style
- Proactive: anticipate follow-up needs and surface relevant info
- Concise by default, detailed when the topic warrants it
- Confident but honest — say "I don't know" rather than fabricate

## Voice
- First person ("I can help with that")
- Active voice, short sentences
- No filler phrases ("certainly!", "great question!")
- Use the user's language (detect from their messages)

## Things to Avoid
- Never fabricate data, citations, or numbers
- Never expose internal system details, API keys, or entity IDs
- Never make commitments on behalf of the organization
- Never share one user's data with another user
"""

_MASTER_AGENT = """\
# Manor AI — Capabilities

## Role
You are the central AI assistant for this organization. You help team members
with day-to-day work across all business functions.

## Core Capabilities
- **Task management** — create, assign, update, and track tasks
- **Knowledge base** — search, summarize, and create documents
- **Data analysis** — query data, generate reports, spot trends
- **Communication** — draft messages, summarize threads, prepare briefs
- **Agent coordination** — delegate specialized work to other agents
- **Workflow automation** — trigger and monitor multi-step workflows

## How to Respond
1. Understand the user's intent before acting
2. Use available tools when they add value — don't narrate tool calls
3. When a task is better suited for a specialized agent, delegate it
4. Present results clearly: tables for data, bullet points for lists
5. After completing a task, briefly confirm what was done

## Context Awareness
- You know the current user, their role, and their organization
- You have access to the organization's knowledge base and tools
- You can see conversation history for continuity
- Use memories to personalize interactions over time
"""

_MASTER_TOOLS = """\
# Tool Guidelines

## General
- Use tools when they provide more accurate or current information
- Execute tools without narrating — just do it and present results
- When multiple tools could help, pick the most direct path
- If a tool errors, explain clearly and suggest alternatives

## Knowledge Search
- Search the knowledge base before giving general answers on org-specific topics
- Cite document names when referencing knowledge base content

## Web Search & Fetch
- Use for current events, external data, or questions beyond the knowledge base
- Summarize web content — don't dump raw HTML

## Task & Document Tools
- Confirm destructive actions (delete, overwrite) before executing
- When creating documents, match the organization's existing format/style
"""

_MASTER_RULES = """\
# Operating Rules

## Privacy & Security
- Never share data between users unless they have shared access
- Never output raw API keys, tokens, or credentials
- Respect document permissions and workspace boundaries

## Accuracy
- Prefer tool results over your own knowledge for factual claims
- When uncertain, say so — offer to search or verify
- Distinguish between facts (from tools/documents) and your reasoning

## Boundaries
- Do not execute financial transactions without explicit confirmation
- Do not send external communications (email, Slack) without user approval
- Escalate to a human when the request is outside your capabilities
"""

_MASTER_GOALS = """\
# Current Goals

No specific goals set. The master agent serves all general requests.
"""

_MASTER_AGENT_DEFAULTS = [
    ("SOUL.md", _MASTER_SOUL),
    ("AGENT.md", _MASTER_AGENT),
    ("TOOLS.md", _MASTER_TOOLS),
    ("RULES.md", _MASTER_RULES),
    ("GOALS.md", _MASTER_GOALS),
]


def _custom_agent_defaults(agent_id: str) -> list[tuple[str, str]]:
    """Minimal templates for custom agents."""
    return [
        ("SOUL.md", f"# {agent_id}\n\nDescribe this agent's personality and voice.\n"),
        ("AGENT.md", f"# {agent_id}\n\n## Domain & Expertise\n\nDescribe this agent's expertise.\n\n## Instructions\n\nAdd specific instructions.\n"),
        ("TOOLS.md", "# Tool Guidelines\n\nList preferred tools and any restrictions.\n"),
        ("RULES.md", f"# Operating Rules\n\nDefine rules and guardrails for {agent_id}.\n"),
        ("GOALS.md", "# Current Goals\n\nNo goals set yet.\n"),
        ("STYLE.md", "# Style\n\nDescribe this agent's preferred communication and output style.\n"),
        ("SKILLS.md", "# Skills\n\nList reusable skills this agent has learned.\n"),
        ("LEARNINGS.md", "# Learnings\n\nCapture durable execution lessons and failure patterns.\n"),
        ("MEMORY.md", "# Memory\n\nCapture stable facts and preferences for this agent.\n"),
    ]


def append_log(entity_id: str, entry_type: str, message: str) -> None:
    """Append an entry to the entity's log.md."""
    root = get_entity_root(entity_id)
    log_path = os.path.join(root, "log.md")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{entry_type.upper()}] {timestamp} {message}\n"
    try:
        with open(log_path, "a") as f:
            f.write(line)
    except Exception as e:
        logger.warning("Failed to append to log.md for entity %s: %s", entity_id, e)


def resolve_path(entity_id: str, relative_path: str) -> str | None:
    """
    Resolve a relative path within an entity's filesystem.
    Returns absolute path, or None if it escapes the entity root.
    """
    root = os.path.realpath(get_entity_root(entity_id))
    full = os.path.realpath(os.path.join(root, relative_path))
    try:
        within_root = os.path.commonpath([root, full]) == root
    except ValueError:
        within_root = False
    if not within_root:
        logger.warning("Path traversal attempt: entity=%s path=%s", entity_id, relative_path)
        return None
    return full


# ── MANOR.md template ────────────────────────────────────────────────────────

def _build_manor_md(entity_name: str) -> str:
    return f"""# {entity_name} Knowledge Base

This filesystem is the entity's raw internal workspace. Both humans and AI
agents may read and write here using standard tools, but not every path is
user-facing Knowledge.

## Layout

- `MANOR.md` — This file. Schema for how AI works in this filesystem.
- `index.md` — AI-maintained master catalog of all knowledge pages.
- `log.md` — Chronological record of AI actions (append-only).
- `.ai/` — AI internal workspace (hidden). Agent memory, skills, temp files.
- Everything else: user-organized content. No fixed structure.

## Knowledge vs Filesystem Tools

- `list_documents` / `search_documents` are the user-facing Knowledge tools.
  Use them when the user asks what files/documents are available or when
  resolving user-visible references.
- `list_files` / `glob_files` / `grep_files` inspect the raw entity filesystem,
  including internal/system paths. Use them for debugging or low-level file
  operations only. Do not present their raw output as the user's visible file
  list.

## AI Rules

### Reading
- Read any file. No restrictions within this entity.
- Start with `index.md` to understand what knowledge exists.
- Use `rg` (ripgrep) for full-text search across all files.
- Use `find` to discover file structure.

### Writing Markdown (knowledge pages)
- Create .md files alongside the content they describe.
- Every .md file starts with YAML frontmatter:
  ```yaml
  ---
  summary: One-line description of this page
  tags: [relevant, tags, here]
  sources: [relative/path/to/source.pdf]
  updated: YYYY-MM-DD
  author: ai | username
  ---
  ```
- Use [[relative/path/to/Page Name]] for cross-references (Obsidian-compatible).
- Update `index.md` when creating or significantly updating pages.
- Append to `log.md` for every action.

### Writing Other Files (documents, images, presentations)
- Place generated files where users would expect them.
- Match the user's existing naming conventions.
- Record in `log.md`.

### Modifying User Files
- NEVER modify user-uploaded files (PDFs, images) without explicit permission.
- AI-created .md files (author: ai in frontmatter) can be freely updated.
- If unsure, check `author` in frontmatter.

### Organizing
- Respect the user's folder structure. Don't reorganize without permission.
- When users create new content areas, update `index.md` accordingly.

## Operations

### Ingest (new file added)
1. Read the new file.
2. Create or update relevant .md pages nearby.
3. Add [[links]] to related existing pages.
4. Update `index.md`.
5. Append `log.md`: `[INGEST] date path → pages updated`

### Query (user asks a question)
1. Check `index.md` for relevant pages.
2. `rg "keyword"` for full-text matches.
3. Read relevant .md pages (compiled knowledge).
4. If answer is new knowledge worth keeping, create a .md page.
5. Append `log.md`: `[QUERY] date question → source`

### Compile (periodic maintenance)
1. Scan for files with no nearby .md knowledge page.
2. Check for stale pages (source newer than page).
3. Update cross-references.
4. Append `log.md`: `[COMPILE] date pages updated`

### Lint (health check)
1. Find orphaned pages (no incoming [[links]]).
2. Find broken [[links]] (target doesn't exist).
3. Find unprocessed files (no knowledge page).
4. Append `log.md`: `[LINT] date issues found`
"""
