from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.memory import (
    runtime_allows_agent_memory_reader,
    runtime_allows_workspace_memory_reader,
)

logger = logging.getLogger(__name__)


DEFAULT_MANOR_IDENTITY = (
    "You are Manor AI, an intelligent assistant for enterprise management. "
    "You help users manage tasks, documents, agents, and business operations. "
    "When discussing files, use Knowledge/document tools for user-visible "
    "files and treat raw filesystem tools as internal inspection only. "
    "You are helpful, accurate, and concise."
)


@dataclass(frozen=True)
class RuntimeAgentIdentityPrompt:
    prompt: str
    source: str
    agent_files_loaded: dict[str, str] = field(default_factory=dict)


_AGENT_FILE_SECTIONS = (
    ("SOUL.md", "soul", "Supplemental Identity Memory"),
    ("AGENT.md", "agent", "Supplemental Agent Notes"),
    ("RULES.md", "rules", "Supplemental Operating Rules"),
    ("TOOLS.md", "tools", "Supplemental Tool Guidelines"),
    ("GOALS.md", "goals", "Supplemental Current Goals"),
    ("STYLE.md", "style", "Supplemental Style Memory"),
    ("SKILLS.md", "skills", "Supplemental Skills Memory"),
    ("LEARNINGS.md", "learnings", "Supplemental Learning Memory"),
    ("MEMORY.md", "memory", "Supplemental Agent Memory"),
)


def runtime_agent_identity_prompt(
    *,
    agent_system_prompt: str | None = None,
    entity_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    allow_agent_files: bool = True,
) -> RuntimeAgentIdentityPrompt:
    """Render the base agent identity and optional file-backed supplements."""
    base_prompt = str(agent_system_prompt or "").strip()
    parts: list[str] = []
    source = "default"
    loaded_levels: dict[str, str] = {}

    if base_prompt:
        source = "db"
        parts.append(base_prompt)

    if entity_id and allow_agent_files:
        try:
            from packages.core.services.agent_files import (
                agent_file_has_custom_content,
                agent_files_have_custom_content,
                effective_agent_id,
                load_agent_files_with_levels,
            )

            effective_id = effective_agent_id(agent_id)
            files, levels = load_agent_files_with_levels(
                entity_id,
                effective_id,
                user_id=user_id,
            )
            if files.has_content() and agent_files_have_custom_content(files, effective_id):
                source = "db+files" if base_prompt else "files"
                loaded_levels = dict(levels or {})
                file_parts: list[str] = []
                for filename, attr, title in _AGENT_FILE_SECTIONS:
                    value = getattr(files, attr, "")
                    if agent_file_has_custom_content(effective_id, filename, value):
                        file_parts.append(f"## {title}\n{value}")
                if file_parts:
                    parts.append("\n\n".join(file_parts))
        except Exception:
            logger.debug("Runtime agent identity file loading failed", exc_info=True)

    if parts:
        return RuntimeAgentIdentityPrompt(
            prompt="\n\n".join(parts),
            source=source,
            agent_files_loaded=loaded_levels,
        )

    return RuntimeAgentIdentityPrompt(
        prompt=DEFAULT_MANOR_IDENTITY,
        source="default",
        agent_files_loaded={},
    )


def runtime_user_context_prompt(user: Any) -> str | None:
    if not user:
        return None
    name = (
        getattr(user, "display_name", None)
        or " ".join(
            filter(
                None,
                [getattr(user, "first_name", None), getattr(user, "last_name", None)],
            )
        )
        or getattr(user, "email", None)
    )
    parts = [f"## Current User: {name}"]
    items: list[str] = []
    role = getattr(user, "role", None)
    email = getattr(user, "email", None)
    timezone = getattr(user, "timezone", None)
    locale = getattr(user, "locale", None)
    if role:
        items.append(f"Role: {role}")
    if email:
        items.append(f"Email: {email}")
    if timezone and timezone != "UTC":
        items.append(f"Timezone: {timezone}")
    if locale and locale != "en":
        items.append(f"Locale: {locale}")
    if items:
        parts.extend(f"- {item}" for item in items)
    return "\n".join(parts)


def runtime_entity_context_prompt(entity: Any) -> str | None:
    if not entity:
        return None
    name = getattr(entity, "name", None)
    parts = [f"## Organization: {name}"]
    slug = getattr(entity, "slug", None)
    email = getattr(entity, "email", None)
    if slug:
        parts.append(f"- Slug: {slug}")
    if email:
        parts.append(f"- Contact: {email}")
    return "\n".join(parts)


def runtime_workspace_context_prompt(workspace: Any) -> str | None:
    if not workspace:
        return None
    name = getattr(workspace, "name", None)
    parts = [f"## Workspace: {name}"]
    description = getattr(workspace, "description", None)
    if description:
        parts.append(f"- Description: {description}")
    return "\n".join(parts)


def runtime_workspace_operating_memory_prompt(
    *,
    envelope,
    entity_id: str | None,
    workspace_id: str | None,
    workspace: Any = None,
    agent_id: str | None = None,
) -> str | None:
    """Render workspace memory only when the runtime mounted workspace memory."""
    if not entity_id or not workspace_id:
        return None
    if not runtime_allows_workspace_memory_reader(envelope, workspace_id):
        return None
    try:
        from packages.core.memory.canonical import (
            ensure_workspace_memory_docs,
            load_workspace_agent_memory,
            load_workspace_operating_memory,
        )
        from packages.core.services.agent_files import effective_agent_id
        from packages.core.services.entity_fs import is_fs_enabled

        if not is_fs_enabled():
            return None

        ensure_workspace_memory_docs(
            entity_id,
            workspace_id,
            workspace_name=getattr(workspace, "name", None),
            workspace_kind=getattr(workspace, "kind", None),
        )
        workspace_memory = load_workspace_operating_memory(entity_id, workspace_id)
        agent_memory = load_workspace_agent_memory(
            entity_id,
            workspace_id,
            effective_agent_id(agent_id),
        )
        sections: list[str] = []
        if workspace_memory:
            sections.append("## Workspace Operating Memory\n" + workspace_memory)
        if agent_memory:
            sections.append("## Workspace-Agent Override Memory\n" + agent_memory)
        return "\n\n".join(sections) if sections else None
    except Exception:
        logger.debug("Runtime workspace operating memory loading failed", exc_info=True)
        return None


async def runtime_agent_memories_prompt(
    db: Any,
    *,
    envelope,
    entity_id: str | None,
    agent_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Render agent/user memories only when the runtime mounted them."""
    if not db or not entity_id:
        return None
    if not runtime_allows_agent_memory_reader(envelope):
        return None
    try:
        from packages.core.services.memory_service import get_context_memories

        memories = await get_context_memories(
            db,
            entity_id,
            agent_id=agent_id,
            user_id=user_id,
        )
        if memories:
            return f"## Agent Memories\n{memories}"
        return None
    except Exception:
        logger.debug("Runtime agent memory loading failed", exc_info=True)
        return None
