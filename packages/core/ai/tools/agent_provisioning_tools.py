"""Global tool: ``provision_agent`` — usable from any chat / skill.

Wraps the same ``agent_provisioning_service`` the workspace architect
uses on finalize. Lets a master agent (or any skill bound to it) spin
up a fully-configured custom agent: tool bindings, skill bindings,
MCP bindings, plus auto-create missing skills.

This is the chat-level companion to ``ws_request_custom_agent``. The
architect's tool emits a *spec* into a workspace draft (deferred
provisioning); this tool *immediately* creates the agent in the entity.
"""
from __future__ import annotations

from typing import Any

from packages.core.ai.runtime import runtime_provision_agent_action


PROVISION_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "provision_agent",
        "description": (
            "Create a custom Agent in the entity with full bindings: "
            "tools, skills, and MCP servers. Auto-creates any "
            "``missing_skill_specs`` you list. Use this when the user "
            "asks for an agent to handle a specific job and no existing "
            "agent fits. Returns agent_id, agent_name, and a list of "
            "warnings (e.g. unknown skill ref). Does NOT subscribe the "
            "agent to a workspace -- pair with the workspace mapping API "
            "if you need that."
        ),
        "parameters": {
            "type": "object",
            "required": ["agent_name", "system_prompt"],
            "properties": {
                "agent_name": {"type": "string", "minLength": 2},
                "system_prompt": {
                    "type": "string",
                    "minLength": 60,
                    "description": (
                        "Full system prompt -- 5-10 sentences anchoring "
                        "the agent's role and scope. End with a one-line "
                        "scope guard."
                    ),
                },
                "description": {"type": "string"},
                "category": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tool_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names from the platform tool pool.",
                },
                "skill_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill ids OR slugs (entity + public).",
                },
                "mcp_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "MCP server ids or server_keys.",
                },
                "missing_skill_specs": {
                    "type": "array",
                    "description": "Skills to auto-create + bind.",
                    "items": {
                        "type": "object",
                        "required": ["name", "system_prompt"],
                        "properties": {
                            "name": {"type": "string"},
                            "slug": {"type": "string"},
                            "description": {"type": "string"},
                            "system_prompt": {"type": "string"},
                            "tools": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
}


async def _provision_agent_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    return await runtime_provision_agent_action(entity_id=entity_id, params=kwargs)


def get_tools():
    return [
        (PROVISION_AGENT_SCHEMA, _provision_agent_handler),
    ]
