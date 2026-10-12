from __future__ import annotations

import json
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
    sections: list[dict] = field(default_factory=list)


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
    strict: bool = False,
) -> RuntimeAgentIdentityPrompt:
    """Render the base agent identity and optional file-backed supplements."""
    base_prompt = str(agent_system_prompt or "").strip()
    parts: list[str] = []
    source = "default"
    loaded_levels: dict[str, str] = {}
    from packages.core.ai.runtime.context_window import context_section
    sections: list[dict] = []

    if base_prompt:
        source = "db"
        parts.append(base_prompt)
        sections.append(context_section("agent_identity", base_prompt, source="agent.system_prompt"))

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
                **({"strict": True} if strict else {}),
            )
            if files.has_content() and agent_files_have_custom_content(files, effective_id):
                source = "db+files" if base_prompt else "files"
                loaded_levels = dict(levels or {})
                file_parts: list[str] = []
                for filename, attr, title in _AGENT_FILE_SECTIONS:
                    value = getattr(files, attr, "")
                    if agent_file_has_custom_content(effective_id, filename, value):
                        rendered = f"## {title}\n{value}"
                        file_parts.append(rendered)
                        section = context_section(
                            filename, rendered,
                            category="memory_files" if filename in {"MEMORY.md", "LEARNINGS.md"} else "agent_instructions",
                            source=f"agent_files:{entity_id}:{effective_id}:{levels.get(filename, 'entity')}:{user_id if levels.get(filename) == 'user' else ''}:{filename}",
                        )
                        section["resolved_scope"] = levels.get(filename)
                        sections.append(section)
                if file_parts:
                    parts.append("\n\n".join(file_parts))
        except Exception:
            if strict:
                raise
            logger.debug("Runtime agent identity file loading failed", exc_info=True)

    if parts:
        return RuntimeAgentIdentityPrompt(
            prompt="\n\n".join(parts),
            source=source,
            agent_files_loaded=loaded_levels,
            sections=sections,
        )

    return RuntimeAgentIdentityPrompt(
        prompt=DEFAULT_MANOR_IDENTITY,
        source="default",
        agent_files_loaded={},
        sections=[context_section("agent_identity", DEFAULT_MANOR_IDENTITY, source="runtime.default_identity")],
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
    settings = getattr(workspace, "settings", None)
    personalization = (
        settings.get("blueprint_personalization")
        if isinstance(settings, dict)
        else None
    )
    if isinstance(personalization, dict) and personalization:
        parts.append(
            "- Blueprint personalization: "
            + json.dumps(
                personalization,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )[:4000]
        )
    return "\n".join(parts)


def runtime_workspace_operating_memory_prompt(
    *,
    envelope,
    entity_id: str | None,
    workspace_id: str | None,
    workspace: Any = None,
    agent_id: str | None = None,
    sections_out: list[dict] | None = None,
    strict: bool = False,
) -> str | None:
    """Render workspace memory only when the runtime mounted workspace memory."""
    if not entity_id or not workspace_id:
        return None
    if not runtime_allows_workspace_memory_reader(envelope, workspace_id):
        return None
    try:
        from packages.core.memory.canonical import (
            ensure_workspace_memory_docs,
            WORKSPACE_MEMORY_DEFAULT_LOAD_ORDER, WORKSPACE_AGENT_MEMORY_FILES,
            read_workspace_memory_file, read_workspace_agent_memory_file,
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
        from packages.core.ai.runtime.context_window import context_section, manage_prompt_sections
        sections = []
        aid = effective_agent_id(agent_id)
        for scope, filenames in (("workspace", WORKSPACE_MEMORY_DEFAULT_LOAD_ORDER),
                                 ("workspace_agent", WORKSPACE_AGENT_MEMORY_FILES)):
            for filename in filenames:
                content = (
                    read_workspace_memory_file(entity_id, workspace_id, filename)
                    if scope == "workspace" else
                    read_workspace_agent_memory_file(entity_id, workspace_id, aid, filename)
                )
                if content:
                    title = "Workspace Operating Memory" if scope == "workspace" else "Workspace-Agent Override Memory"
                    sections.append(context_section(
                        f"{scope}:{filename}", f"## {title}\n### {filename}\n{content}",
                        category="rules" if filename == "RULES.md" else "memory_files",
                        source=f"{scope}:{entity_id}:{workspace_id}:{aid if scope == 'workspace_agent' else ''}:{filename}",
                    ))
        if sections_out is not None:
            sections_out.extend(sections)
        else:
            sections = manage_prompt_sections(sections)
        return "\n\n".join(section["content"] for section in sections) or None
    except Exception:
        if strict:
            raise
        logger.debug("Runtime workspace operating memory loading failed", exc_info=True)
        return None


async def runtime_agent_memories_prompt(
    db: Any,
    *,
    envelope,
    entity_id: str | None,
    agent_id: str | None = None,
    user_id: str | None = None,
    active_user_message: str | None = None,
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
            query=active_user_message,
        )
        if memories:
            return f"## Agent Memories\n{memories}"
        return None
    except Exception:
        logger.debug("Runtime agent memory loading failed", exc_info=True)
        return None
