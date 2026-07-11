"""Agent file-based definitions — read/write AGENT.md, SOUL.md, TOOLS.md, etc.

Agent memory and supplemental configuration lives as plain Markdown files on
the entity filesystem, editable by bash, patch, or any tool that writes files.

File layout per agent (with per-user overrides):

    /mnt/manor/{entity_id}/.ai/agents/{agent_id}/
    ├── SOUL.md              ← entity-level defaults
    ├── AGENT.md
    ├── TOOLS.md
    ├── RULES.md
    ├── GOALS.md
    ├── STYLE.md
    ├── SKILLS.md
    ├── LEARNINGS.md
    ├── MEMORY.md
    ├── memory/
    └── users/
        └── {user_id}/
            ├── SOUL.md      ← per-user overrides (takes priority)
            ├── AGENT.md
            └── ...

Loading model: DB system_prompt is the base identity. User/entity files are
flexible supplements layered on top; user-level files override entity-level
files per file. Same agent can have different supplemental context per user.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from packages.core.constants.agents import MANOR_AGENT_FS_ID as MASTER_AGENT_ID
from packages.core.services.entity_fs import (
    append_log,
    get_agent_dir,
    provision_agent_workspace,
)

logger = logging.getLogger(__name__)

# Canonical agent definition files and their purposes
AGENT_FILES = {
    "SOUL.md": "Agent identity — personality, style, voice, things to avoid",
    "AGENT.md": "Agent capabilities — domain knowledge, instructions, expertise",
    "TOOLS.md": "Tool usage — preferred tools, tool-specific guidelines, examples",
    "RULES.md": "Operating rules — constraints, guardrails, compliance requirements",
    "GOALS.md": "Current goals — active objectives and priorities",
    "STYLE.md": "Communication and output style — formatting, tone, interaction preferences",
    "SKILLS.md": "Reusable skills — durable procedures the agent has learned",
    "LEARNINGS.md": "Agent learnings — failure modes, calibration, and improvement notes",
    "MEMORY.md": "Agent memory — stable facts and preferences for this agent",
}

# Sorted list for tool schema enums (generated from AGENT_FILES)
AGENT_FILE_NAMES: list[str] = sorted(AGENT_FILES.keys())

# In-memory cache of provisioned agent workspaces (process-lifetime)
_provisioned: set[tuple[str, str]] = set()


def effective_agent_id(agent_id: str | None) -> str:
    """Return agent_id or MASTER_AGENT_ID for the master agent."""
    return agent_id or MASTER_AGENT_ID


def _safe_user_dir(entity_id: str, agent_id: str, user_id: str) -> str:
    """Return the per-user override directory for an agent."""
    agent_dir = get_agent_dir(entity_id, agent_id)
    safe_user = re.sub(r'[/\\]', '_', str(user_id).strip())
    return os.path.join(agent_dir, "users", safe_user)


@dataclass
class AgentFileSet:
    """All agent definition files loaded as strings."""
    soul: str | None = None
    agent: str | None = None
    tools: str | None = None
    rules: str | None = None
    goals: str | None = None
    style: str | None = None
    skills: str | None = None
    learnings: str | None = None
    memory: str | None = None

    def has_content(self) -> bool:
        return any([
            self.soul, self.agent, self.tools, self.rules, self.goals,
            self.style, self.skills, self.learnings, self.memory,
        ])


def read_agent_file(
    entity_id: str,
    agent_id: str,
    filename: str,
    *,
    user_id: str | None = None,
) -> str | None:
    """Read a single agent definition file. Returns None if missing or empty.

    When user_id is provided, checks the per-user override first, then
    falls back to the entity-level default.
    """
    # Try per-user override first
    if user_id:
        user_dir = _safe_user_dir(entity_id, agent_id, user_id)
        content = _read_file(os.path.join(user_dir, filename))
        if content is not None:
            return content

    # Fall back to entity-level default
    agent_dir = get_agent_dir(entity_id, agent_id)
    return _read_file(os.path.join(agent_dir, filename))


def write_agent_file(
    entity_id: str,
    agent_id: str,
    filename: str,
    content: str,
    *,
    user_id: str | None = None,
) -> str:
    """Write (create or overwrite) an agent definition file. Returns the file path.

    When user_id is provided, writes to the per-user override directory.
    Otherwise writes to the entity-level default.
    """
    if filename not in AGENT_FILES:
        raise ValueError(f"Unknown agent file: {filename}. Valid: {AGENT_FILE_NAMES}")

    if user_id:
        target_dir = _safe_user_dir(entity_id, agent_id, user_id)
    else:
        target_dir = get_agent_dir(entity_id, agent_id)

    os.makedirs(target_dir, exist_ok=True)
    filepath = os.path.join(target_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    level = "user" if user_id else "entity"
    logger.info("Wrote agent file: %s (%d chars, level=%s)", filepath, len(content), level)

    # Record in entity activity log
    try:
        who = f"user:{user_id}" if user_id else "system"
        append_log(
            entity_id, "AGENT_FILE",
            f"{who} wrote {filename} for agent {agent_id} ({level} level, {len(content)} chars)",
        )
    except Exception:
        pass  # best-effort logging

    return filepath


def _read_with_level(
    entity_id: str,
    agent_id: str,
    filename: str,
    user_id: str | None,
) -> tuple[str | None, str]:
    """Read a file and return (content, level) where level is 'user'|'entity'|'none'."""
    if user_id:
        user_dir = _safe_user_dir(entity_id, agent_id, user_id)
        content = _read_file(os.path.join(user_dir, filename))
        if content is not None:
            return content, "user"

    agent_dir = get_agent_dir(entity_id, agent_id)
    content = _read_file(os.path.join(agent_dir, filename))
    if content is not None:
        return content, "entity"
    return None, "none"


def load_agent_files(
    entity_id: str,
    agent_id: str,
    *,
    user_id: str | None = None,
) -> AgentFileSet:
    """Load all agent definition files into an AgentFileSet.

    Per-user files override entity-level defaults per file.
    """
    files, _ = load_agent_files_with_levels(entity_id, agent_id, user_id=user_id)
    return files


def load_agent_files_with_levels(
    entity_id: str,
    agent_id: str,
    *,
    user_id: str | None = None,
) -> tuple[AgentFileSet, dict[str, str]]:
    """Load all agent definition files and return (files, levels).

    levels is a dict of {filename: "user"|"entity"|"none"} showing
    where each file was loaded from.
    """
    mapping = [
        ("SOUL.md", "soul"),
        ("AGENT.md", "agent"),
        ("TOOLS.md", "tools"),
        ("RULES.md", "rules"),
        ("GOALS.md", "goals"),
        ("STYLE.md", "style"),
        ("SKILLS.md", "skills"),
        ("LEARNINGS.md", "learnings"),
        ("MEMORY.md", "memory"),
    ]
    levels: dict[str, str] = {}
    values: dict[str, str | None] = {}
    for fname, attr in mapping:
        content, level = _read_with_level(entity_id, agent_id, fname, user_id)
        values[attr] = content
        if content is not None:
            levels[fname] = level
    return AgentFileSet(**values), levels


def agent_files_have_custom_content(files: AgentFileSet, agent_id: str) -> bool:
    """Return True when agent definition files contain real user-authored content.

    Custom agent workspaces are seeded with placeholder files so agents can edit
    their own SOUL.md / AGENT.md later. Those placeholders should not override
    the DB ``system_prompt`` configured from the Agents page.
    """
    try:
        loaded = {
            "SOUL.md": files.soul,
            "AGENT.md": files.agent,
            "TOOLS.md": files.tools,
            "RULES.md": files.rules,
            "GOALS.md": files.goals,
            "STYLE.md": files.style,
            "SKILLS.md": files.skills,
            "LEARNINGS.md": files.learnings,
            "MEMORY.md": files.memory,
        }
        return any(agent_file_has_custom_content(agent_id, filename, content) for filename, content in loaded.items())
    except Exception:
        # Be conservative: if the placeholder check itself fails, preserve the
        # old behavior rather than dropping potentially important instructions.
        return files.has_content()


def agent_file_has_custom_content(agent_id: str, filename: str, content: str | None) -> bool:
    """Return whether one loaded file is meaningful runtime memory."""
    if not content or not content.strip():
        return False
    try:
        from packages.core.constants.agents import MANOR_AGENT_FS_ID

        # Master defaults are intentional prompt content, not placeholders.
        if agent_id == MANOR_AGENT_FS_ID:
            return True

        from packages.core.services.entity_fs import _custom_agent_defaults

        defaults = {
            name: default_content.strip()
            for name, default_content in _custom_agent_defaults(agent_id)
        }
        return content.strip() != defaults.get(filename, "")
    except Exception:
        return True


def list_agent_files(
    entity_id: str,
    agent_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, dict]:
    """List all agent definition files with metadata.

    Shows both entity-level and per-user files when user_id is provided.
    """
    agent_dir = get_agent_dir(entity_id, agent_id)
    user_dir = _safe_user_dir(entity_id, agent_id, user_id) if user_id else None

    result = {}
    for filename, description in AGENT_FILES.items():
        entity_path = os.path.join(agent_dir, filename)
        user_path = os.path.join(user_dir, filename) if user_dir else None

        # Check user-level first
        has_user = False
        if user_path:
            try:
                user_size = os.path.getsize(user_path)
                has_user = True
            except OSError:
                user_size = 0

        try:
            entity_size = os.path.getsize(entity_path)
            has_entity = True
        except OSError:
            entity_size = 0
            has_entity = False

        entry: dict = {
            "exists": has_user or has_entity,
            "size": user_size if has_user else entity_size,
            "description": description,
            "path": user_path if has_user else entity_path,
            "level": "user" if has_user else ("entity" if has_entity else "none"),
        }
        if user_id:
            entry["entity_exists"] = has_entity
            entry["user_exists"] = has_user
        result[filename] = entry
    return result


def ensure_agent_workspace(entity_id: str, agent_id: str) -> str:
    """Ensure the agent workspace exists with all definition files.

    Uses an in-memory cache so repeated calls (e.g. per chat message)
    skip filesystem checks after the first invocation.
    """
    key = (entity_id, agent_id)
    if key in _provisioned:
        return get_agent_dir(entity_id, agent_id)

    agent_dir = provision_agent_workspace(entity_id, agent_id)
    _provisioned.add(key)
    return agent_dir


def build_agent_prompt_from_files(
    entity_id: str,
    agent_id: str,
    *,
    user_id: str | None = None,
    include_tools: bool = True,
    include_goals: bool = True,
) -> Optional[str]:
    """Build agent identity prompt sections from definition files.

    Returns a combined string suitable for injection into the system prompt,
    or None if no files have content beyond default templates.
    Per-user files override entity-level defaults per file.
    """
    files = load_agent_files(entity_id, agent_id, user_id=user_id)
    if not files.has_content():
        return None

    parts: list[str] = []

    if agent_file_has_custom_content(agent_id, "SOUL.md", files.soul):
        parts.append(f"## Agent Identity\n{files.soul}")

    if agent_file_has_custom_content(agent_id, "AGENT.md", files.agent):
        parts.append(f"## Agent Instructions\n{files.agent}")

    if agent_file_has_custom_content(agent_id, "RULES.md", files.rules):
        parts.append(f"## Operating Rules\n{files.rules}")

    if include_tools and agent_file_has_custom_content(agent_id, "TOOLS.md", files.tools):
        parts.append(f"## Tool Guidelines\n{files.tools}")

    if include_goals and agent_file_has_custom_content(agent_id, "GOALS.md", files.goals):
        parts.append(f"## Current Goals\n{files.goals}")

    if agent_file_has_custom_content(agent_id, "STYLE.md", files.style):
        parts.append(f"## Agent Style Memory\n{files.style}")

    if agent_file_has_custom_content(agent_id, "SKILLS.md", files.skills):
        parts.append(f"## Agent Skills Memory\n{files.skills}")

    if agent_file_has_custom_content(agent_id, "LEARNINGS.md", files.learnings):
        parts.append(f"## Agent Learning Memory\n{files.learnings}")

    if agent_file_has_custom_content(agent_id, "MEMORY.md", files.memory):
        parts.append(f"## Agent Stable Memory\n{files.memory}")

    return "\n\n".join(parts) if parts else None


# ── Internal helpers ─────────────────────────────────────────────────────────

def _read_file(filepath: str) -> str | None:
    """Read a text file. Returns None if missing or empty."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content if content and content.strip() else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read agent file %s: %s", filepath, e)
        return None
