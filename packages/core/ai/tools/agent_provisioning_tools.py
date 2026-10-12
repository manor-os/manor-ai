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

# Import from the defining submodule, NOT the package root. This module is
# lazily imported on first tool use; in a long-running worker booted before a
# deploy, the old `packages.core.ai.runtime` __init__ stays cached in
# sys.modules and lacks newly added re-exports — a root import then dies with
# "ImportError: cannot import name ...". A fresh submodule import reads the
# new code from disk and survives the deploy version-skew window.
from packages.core.ai.runtime.agent_provisioning import (
    runtime_provision_agent_action,
    runtime_query_agent_capabilities_action,
    runtime_query_entity_agents_action,
)
from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_trusted_workspace_id_from_context,
)
from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.constants.agents import is_master_agent


PROVISION_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "provision_agent",
        "description": (
            "Create a custom Agent in the entity with full bindings: "
            "tools, skills, and MCP actions. Call query_agent_capabilities "
            "first, semantically choose the smallest sufficient set, and pass "
            "its exact ids unchanged in capability_ids. Auto-creates any "
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
                "capability_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": AGENT_CAPABILITY_SELECTION_LIMIT,
                    "description": (
                        "Exact ids returned by query_agent_capabilities. "
                        "Never invent, shorten, or translate these ids."
                    ),
                },
                "tool_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Legacy exact tool names. Prefer capability_ids from "
                        "query_agent_capabilities. Unknown names fail before creation."
                    ),
                },
                "skill_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Legacy exact Skill ids only. Prefer capability_ids; "
                        "slugs and fuzzy names are not accepted."
                    ),
                },
                "skill_binding_refs": {
                    "type": "array",
                    "description": (
                        "Exact Marketplace Skill identities. Prefer these over "
                        "slugs when binding a Marketplace Skill."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["marketplace_source", "marketplace_id"],
                        "properties": {
                            "marketplace_source": {
                                "type": "string",
                                "enum": ["platform", "manor"],
                            },
                            "marketplace_id": {"type": "string", "minLength": 1},
                            "slug": {"type": "string"},
                        },
                    },
                },
                "mcp_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Legacy exact MCP server_keys. Prefer action-level "
                        "capability_ids from query_agent_capabilities."
                    ),
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


QUERY_AGENT_CAPABILITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_agent_capabilities",
        "description": (
            "Return one actor-scoped catalog of exact Tool, Skill, BusinessCapability, "
            "and MCP action ids. This catalog already accounts for resource visibility "
            "and reports connection readiness. Use semantic reasoning over the returned "
            "descriptions; do not keyword-match or invent ids."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


QUERY_ENTITY_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_entity_agents",
        "description": (
            "Query read-only Agents by statuses, categories, sources, Workspaces, owners, "
            "created or updated dates, and deployment count in one call. Values in plural "
            "filters are ORed; different filters and inclusive ranges are ANDed. Filtering "
            "happens before pagination and total is exact. Never returns system prompts. "
            "When selecting an Agent for create_scheduled_job, call this first and copy "
            "an exact returned id; custom Agents see only Agents with one active "
            "deployment in their current Workspace or personal scope, and automation "
            "creation revalidates that scope."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional name, description, or category filter.",
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "uniqueItems": True,
                    "description": "Match any listed Agent status (OR).",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "description": "Match any listed category (OR).",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "uniqueItems": True,
                    "description": "Match any stored source or the virtual source 'template' (OR).",
                },
                "workspace_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                },
                "owner_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                },
                "include_templates": {
                    "type": "boolean",
                    "description": "Include public platform templates when true.",
                },
                "deployment_count_min": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Inclusive active-deployment lower bound.",
                },
                "deployment_count_max": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Inclusive active-deployment upper bound.",
                },
                **{
                    field: {
                        "type": "string",
                        "format": "date-time",
                        "description": f"Inclusive {label} bound.",
                    }
                    for field, label in (
                        ("created_after", "created-at lower"),
                        ("created_before", "created-at upper"),
                        ("updated_after", "updated-at lower"),
                        ("updated_before", "updated-at upper"),
                    )
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                },
            },
        },
    },
}


async def _provision_agent_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    return await runtime_provision_agent_action(
        entity_id=entity_id,
        user_id=user_id,
        params=kwargs,
    )


async def _query_entity_agents_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    query_kwargs: dict[str, Any] = {
        "entity_id": entity_id,
        "query": str(kwargs.get("query") or ""),
        "statuses": list(kwargs.get("statuses") or []),
        "categories": list(kwargs.get("categories") or []),
        "sources": list(kwargs.get("sources") or []),
        "workspace_ids": list(kwargs.get("workspace_ids") or []),
        "owner_user_ids": list(kwargs.get("owner_user_ids") or []),
        "include_templates": bool(kwargs.get("include_templates", False)),
        "created_after": str(kwargs.get("created_after") or ""),
        "created_before": str(kwargs.get("created_before") or ""),
        "updated_after": str(kwargs.get("updated_after") or ""),
        "updated_before": str(kwargs.get("updated_before") or ""),
        "deployment_count_min": kwargs.get("deployment_count_min"),
        "deployment_count_max": kwargs.get("deployment_count_max"),
        "limit": int(kwargs.get("limit") or 50),
        "offset": int(kwargs.get("offset") or 0),
        "viewer_user_id": runtime_context.user_id or user_id or None,
        "runtime_workspace_id": runtime_trusted_workspace_id_from_context(
            runtime_context
        ),
        "enforce_viewer_scope": True,
    }
    if runtime_context.agent_id and not is_master_agent(runtime_context.agent_id):
        query_kwargs.update({
            "require_schedulable_scope": True,
            "schedulable_workspace_id": runtime_context.workspace_id,
            "schedulable_user_id": runtime_context.user_id,
        })
    return await runtime_query_entity_agents_action(
        **query_kwargs,
    )


async def _query_agent_capabilities_handler(
    entity_id: str = "", user_id: str = "", **_kwargs: Any,
) -> str:
    return await runtime_query_agent_capabilities_action(
        entity_id=entity_id,
        user_id=user_id,
    )


def get_tools():
    return [
        (QUERY_AGENT_CAPABILITIES_SCHEMA, _query_agent_capabilities_handler),
        (PROVISION_AGENT_SCHEMA, _provision_agent_handler),
        (QUERY_ENTITY_AGENTS_SCHEMA, _query_entity_agents_handler),
    ]
