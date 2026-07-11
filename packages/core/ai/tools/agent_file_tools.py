"""Agent file tools — read/write agent definition files (SOUL.md, AGENT.md, TOOLS.md, etc.).

These tools let agents read and edit their own identity/configuration files on the
entity filesystem. Files are plain Markdown, editable by bash/patch as well.

Per-user overrides: set level="user" to write a user-specific version of a file.
Loading priority: user-level > entity-level > DB > default.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.agent_files import (
    RUNTIME_AGENT_FILE_NAMES,
    runtime_effective_agent_file_id,
    runtime_list_agent_files,
    runtime_read_agent_file,
    runtime_write_agent_file,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

READ_AGENT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_agent_file",
        "description": (
            "Read an agent definition file (SOUL.md, AGENT.md, TOOLS.md, RULES.md, GOALS.md, "
            "STYLE.md, SKILLS.md, LEARNINGS.md, MEMORY.md). "
            "These files define the agent's identity, instructions, tool guidelines, rules, goals, "
            "style, reusable skills, learnings, and stable memory. "
            "Omit agent_id to read your own files. "
            "Per-user files override entity-level defaults automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID. Omit to read your own files.",
                },
                "filename": {
                    "type": "string",
                    "enum": RUNTIME_AGENT_FILE_NAMES,
                    "description": "Which definition file to read.",
                },
            },
            "required": ["filename"],
        },
    },
}

WRITE_AGENT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_agent_file",
        "description": (
            "Write (create or update) an agent definition file. "
            "Use this to update identity, instructions, tool guidelines, rules, goals, "
            "style, reusable skills, learnings, or stable memory. "
            "Set level='user' to write a per-user override (different soul/instructions per user). "
            "Set level='entity' (default) for the shared entity-level file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID. Omit to write your own files.",
                },
                "filename": {
                    "type": "string",
                    "enum": RUNTIME_AGENT_FILE_NAMES,
                    "description": "Which definition file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The full Markdown content for the file.",
                },
                "level": {
                    "type": "string",
                    "enum": ["entity", "user"],
                    "description": "Write at entity level (shared default) or user level (per-user override). Default: entity.",
                },
            },
            "required": ["filename", "content"],
        },
    },
}

LIST_AGENT_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_agent_files",
        "description": (
            "List all agent definition files with their sizes and descriptions. "
            "Shows which files exist at entity and user level."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID. Omit to list your own files.",
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _resolve_context(kwargs: dict) -> tuple[str | None, str | None]:
    """Resolve agent_id and user_id from kwargs + injected context."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    agent_id = kwargs.get("agent_id")
    if not agent_id or agent_id == "self":
        agent_id = runtime_context.agent_id
    user_id = runtime_context.user_id
    return agent_id, user_id


def handle_read_agent_file(entity_id: str = "", **kwargs: Any) -> str:
    filename = kwargs.get("filename", "")
    if not entity_id:
        return json.dumps({"error": "No entity context available"})
    if not filename:
        return json.dumps({"error": "filename is required"})

    agent_id, user_id = _resolve_context(kwargs)
    aid = runtime_effective_agent_file_id(agent_id)

    # Read with per-user override resolution
    content = runtime_read_agent_file(
        entity_id=entity_id,
        agent_id=aid,
        filename=filename,
        user_id=user_id,
    )
    if content is None:
        return json.dumps({
            "filename": filename,
            "exists": False,
            "hint": f"File {filename} doesn't exist yet. Use write_agent_file to create it.",
        })

    return json.dumps({
        "filename": filename,
        "content": content,
        "chars": len(content),
    }, ensure_ascii=False)


def handle_write_agent_file(entity_id: str = "", **kwargs: Any) -> str:
    filename = kwargs.get("filename", "")
    content = kwargs.get("content", "")
    level = kwargs.get("level", "entity")

    if not entity_id:
        return json.dumps({"error": "No entity context available"})
    if not filename:
        return json.dumps({"error": "filename is required"})
    if not content:
        return json.dumps({"error": "content is required"})

    agent_id, user_id = _resolve_context(kwargs)
    aid = runtime_effective_agent_file_id(agent_id)

    # Per-user write requires user context
    write_user_id = user_id if level == "user" else None
    if level == "user" and not write_user_id:
        return json.dumps({"error": "Cannot write user-level file: no user context available"})

    path = runtime_write_agent_file(
        entity_id=entity_id,
        agent_id=aid,
        filename=filename,
        content=content,
        user_id=write_user_id,
    )
    return json.dumps({
        "filename": filename,
        "path": path,
        "chars": len(content),
        "level": level,
        "status": "written",
    })


def handle_list_agent_files(entity_id: str = "", **kwargs: Any) -> str:
    if not entity_id:
        return json.dumps({"error": "No entity context available"})

    agent_id, user_id = _resolve_context(kwargs)
    aid = runtime_effective_agent_file_id(agent_id)

    files = runtime_list_agent_files(entity_id=entity_id, agent_id=aid, user_id=user_id)
    return json.dumps(files, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, Any]]:
    """Return agent file tool schemas and handlers."""
    return [
        (READ_AGENT_FILE_SCHEMA, handle_read_agent_file),
        (WRITE_AGENT_FILE_SCHEMA, handle_write_agent_file),
        (LIST_AGENT_FILES_SCHEMA, handle_list_agent_files),
    ]
