"""Workspace Architect tools — typed builder operations the architect skill
uses to incrementally fill a ``workspace_drafts`` row.

All handlers:
  * accept ``draft_id`` as a tool-call argument so the same tools work
    when invoked from any chat (the architect's system prompt instructs
    the LLM to pass it on every call).
  * scope every read/write by ``entity_id`` so a draft from another
    entity can never be mutated, even if the LLM hallucinates an id.
  * never return ``None`` — they always return a JSON string the LLM
    can read back, including on validation failure (so the LLM sees
    "field X required" and retries).

The strict required-field schemas are the precision lever: the LLM can't
omit ``target`` on a goal because the function-calling layer rejects the
call before it reaches the handler.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

CADENCE_VALUES = ["daily", "weekly", "monthly", "quarterly", "yearly"]
AUTONOMY_VALUES = ["full", "assisted", "supervised", "manual"]
CHANNEL_TYPES = [
    "twilio_sms", "twilio_voice", "wechat", "wechat_personal",
    "telegram", "whatsapp", "email", "slack", "discord", "webchat",
    "internal_chat", "voice_stream", "generic_http", "other",
]


COMMIT_BASICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_commit_basics",
        "description": (
            "Persist the workspace's top-level identity to the draft. "
            "Call this once you have a clear picture of name + kind + "
            "operating_context + primary_work. Calling it again overwrites "
            "the previous values."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "name", "kind", "operating_context", "primary_work"],
            "properties": {
                "draft_id": {"type": "string", "description": "ULID of the draft you are editing."},
                "name": {"type": "string", "minLength": 2, "description": "Human-readable workspace name."},
                "kind": {"type": "string", "minLength": 2, "description": "Type: property / project / campaign / channel / support_desk / etc."},
                "operating_context": {"type": "string", "minLength": 5, "description": "Where / for whom this workspace runs."},
                "primary_work": {"type": "string", "minLength": 5, "description": "Core responsibilities in 1-3 sentences."},
                "category": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    },
}


PROPOSE_SERVICE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_service",
        "description": (
            "Add or replace one service the workspace will perform. Call "
            "this 2-5 times to cover the workspace's primary_work. Re-calling "
            "with the same service_key replaces the prior entry."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key", "name", "description", "autonomy_level", "owner_role"],
            "properties": {
                "draft_id": {"type": "string"},
                "service_key": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]*$",
                    "description": "snake_case identifier, e.g. 'content_creation'",
                },
                "name": {"type": "string", "minLength": 2, "description": "Human-readable service name, e.g. 'Content Creation'"},
                "description": {"type": "string", "minLength": 10},
                "autonomy_level": {"enum": AUTONOMY_VALUES},
                "owner_role": {"type": "string", "minLength": 2, "description": "e.g. 'content_strategist'"},
                "rationale": {"type": "string", "description": "Why this service is needed (links to primary_work)."},
            },
        },
    },
}


PROPOSE_GOAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_goal",
        "description": (
            "Add or replace one measurable goal. Re-calling with the same "
            "goal_key replaces the prior entry. ALL four of goal_key, "
            "description, target, and cadence are required — never omit "
            "target or cadence even if the user did not mention numbers; "
            "infer a reasonable default from the description and flag "
            "rationale=\"inferred\"."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "goal_key", "title", "description", "target", "cadence"],
            "properties": {
                "draft_id": {"type": "string"},
                "goal_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "title": {"type": "string", "minLength": 2, "description": "Short headline, e.g. 'Follower growth'"},
                "description": {"type": "string", "minLength": 10},
                "target": {"type": "string", "minLength": 1, "description": "Target value as string: '10000', '5%', '45%'"},
                "cadence": {"enum": CADENCE_VALUES},
                "metric_key": {"type": "string", "description": "Canonical metric, e.g. 'follower_count'"},
                "rationale": {"type": "string"},
            },
        },
    },
}


PROPOSE_AGENT_MAPPING_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_agent_mapping",
        "description": (
            "Suggest a workspace service should be handled by a specific "
            "entity-level Agent. Use the agent_id returned by "
            "ws_search_entity_agents. If no good match exists, call "
            "ws_request_custom_agent instead."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key", "agent_id", "rationale"],
            "properties": {
                "draft_id": {"type": "string"},
                "service_key": {"type": "string"},
                "agent_id": {"type": "string", "pattern": "^[A-Z0-9]{26}$", "description": "ULID returned by ws_search_entity_agents."},
                "rationale": {"type": "string"},
            },
        },
    },
}


REQUEST_CUSTOM_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_request_custom_agent",
        "description": (
            "Design a custom agent for a service when no existing agent "
            "fits. ALWAYS call ws_search_capabilities first so you can "
            "bind real tools / skills / integrations the entity owns. "
            "On finalize, the platform creates the Agent + binds every "
            "tool / skill / mcp_server you list, auto-creates any "
            "missing skills you specified, and surfaces any missing "
            "integrations on the workspace as a 'needs setup' warning."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key", "agent_name", "system_prompt"],
            "properties": {
                "draft_id": {"type": "string"},
                "service_key": {"type": "string"},
                "agent_name": {"type": "string", "minLength": 2},
                "system_prompt": {
                    "type": "string",
                    "minLength": 80,
                    "description": (
                        "Full system prompt -- not a seed. Describe the "
                        "agent's CAPABILITY as a general specialist; the "
                        "Agent is owned by the entity and reusable across "
                        "workspaces. Do NOT bake a specific workspace's "
                        "name / context into this prompt -- that lives in "
                        "the subscription's custom_prompt layer. State "
                        "the agent's name, core skills, and end with a "
                        "one-line scope guard."
                    ),
                },
                "agent_description": {"type": "string"},
                "tool_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names from ws_search_capabilities.tools that this agent should be allowed to call.",
                },
                "business_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Runtime BusinessCapability ids from "
                        "ws_search_capabilities.business_capabilities. Prefer "
                        "these for platform/workspace abilities; the runtime "
                        "expands them into tool bindings during provisioning."
                    ),
                },
                "skill_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill ids OR slugs from ws_search_capabilities.skills the agent may invoke.",
                },
                "mcp_bindings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "MCP server keys (e.g. 'twitter','gmail','manor_pms') "
                        "to bind. Only include servers backed by an active "
                        "integration -- check ws_search_capabilities.integrations."
                    ),
                },
                "missing_skill_specs": {
                    "type": "array",
                    "description": (
                        "Skills this agent needs that don't yet exist. The "
                        "platform will auto-create each one and bind it to "
                        "the agent. Only use when the capability is NOT "
                        "covered by an existing skill or tool."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["name", "system_prompt"],
                        "properties": {
                            "name": {"type": "string", "minLength": 2},
                            "slug": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"},
                            "description": {"type": "string"},
                            "system_prompt": {"type": "string", "minLength": 60},
                            "tools": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "missing_integrations": {
                    "type": "array",
                    "description": (
                        "Integrations the operator must set up before this "
                        "agent can fully serve the service (e.g. user did "
                        "not yet connect their Twitter API). The workspace "
                        "will display these as warnings; the agent is still "
                        "created but the missing integrations will block "
                        "the relevant capabilities."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["provider", "purpose"],
                        "properties": {
                            "provider": {
                                "type": "string",
                                "description": "MCP server key / integration slug (e.g. 'twitter','wechat_mp').",
                            },
                            "purpose": {"type": "string", "minLength": 5},
                            "required": {"type": "boolean", "description": "True = blocks the service from working at all."},
                        },
                    },
                },
                "rationale": {"type": "string"},
            },
        },
    },
}


ASSIGN_STAFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_assign_staff",
        "description": (
            "Assign a real staff member from the entity's roster to the "
            "workspace, optionally bound to a specific service. Call "
            "ws_search_capabilities first so you have valid staff_ids. "
            "Re-calling with the same staff_id replaces the prior role."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "staff_id", "role"],
            "properties": {
                "draft_id": {"type": "string"},
                "staff_id": {"type": "string", "pattern": "^[A-Z0-9]{26}$"},
                "role": {
                    "type": "string",
                    "minLength": 2,
                    "description": "Friendly role label, e.g. 'owner', 'editor', 'reviewer'.",
                },
                "service_key": {
                    "type": "string",
                    "description": "Optional — bind staff to one workspace service.",
                },
                "rationale": {"type": "string"},
            },
        },
    },
}


ATTACH_KNOWLEDGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_attach_knowledge",
        "description": (
            "Attach knowledge to the workspace so agents have RAG "
            "context from day one. Two modes:\n"
            "- mode='create_new': spin up a fresh Knowledge Net (DocumentGroup) bound "
            "to the workspace (the operator uploads / drops in docs "
            "later from the Documents tab). Use when the user's "
            "service needs a knowledge network but no existing group "
            "matches. It generates a starter markdown doc by default.\n"
            "- mode='clone_template': copy a template document group "
            "(seed playbooks). Pass template_group_id from "
            "ws_search_capabilities.knowledge. It does not generate a "
            "starter doc unless generate_starter_doc=true.\n"
            "Re-calling with the same name replaces the prior entry."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "name", "purpose", "mode"],
            "properties": {
                "draft_id": {"type": "string"},
                "name": {"type": "string", "minLength": 2},
                "purpose": {
                    "type": "string",
                    "minLength": 5,
                    "description": "What this group is for, e.g. 'Brand voice + posting examples'.",
                },
                "mode": {"enum": ["create_new", "clone_template"]},
                "template_group_id": {"type": "string"},
                "linked_service_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Services that should read this group (informational).",
                },
                "generate_starter_doc": {
                    "type": "boolean",
                    "description": (
                        "Whether to create an AI-generated starter markdown document "
                        "and bind it to this group after workspace creation. Defaults "
                        "true for create_new and false for clone_template."
                    ),
                },
                "rationale": {"type": "string"},
            },
        },
    },
}


FLAG_MISSING_INTEGRATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_flag_missing_integration",
        "description": (
            "Explicitly flag an integration the workspace needs but the "
            "entity hasn't set up yet. Use independent of agent creation "
            "when an integration is workspace-wide (e.g. an analytics "
            "platform multiple services consume)."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "provider", "purpose"],
            "properties": {
                "draft_id": {"type": "string"},
                "provider": {"type": "string"},
                "purpose": {"type": "string", "minLength": 5},
                "required": {"type": "boolean"},
                "linked_service_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Which workspace services this gap blocks.",
                },
            },
        },
    },
}


SEARCH_CAPABILITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_search_capabilities",
        "description": (
            "Single-shot inventory of everything the architect can bind "
            "to this workspace: runtime business capabilities, tools "
            "(from the platform tool pool), skills (entity + public "
            "templates), integrations + their MCP servers, the entity's "
            "staff roster (real ulids), and available knowledge groups "
            "(entity-level + public templates). Prefer "
            "business_capabilities[].id for workspace operation bindings; "
            "use direct tool names only for narrow custom agent bindings. "
            "Always call this BEFORE proposing custom agents, staff "
            "assignments, or knowledge attachments so you reference real "
            "ids. ``query`` narrows results across all categories."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "query": {"type": "string"},
                "limit_per_kind": {"type": "integer", "minimum": 5, "maximum": 100},
            },
        },
    },
}


PROPOSE_CHANNEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_channel",
        "description": (
            "Set or update one channel binding for the workspace. The "
            "primary_external channel is what the workspace publishes to / "
            "receives from in the world; internal_chat is the operator's "
            "control surface. Repeated calls with role='secondary_external' "
            "stack into a list."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "role", "channel_type", "purpose"],
            "properties": {
                "draft_id": {"type": "string"},
                "role": {"enum": ["primary_external", "secondary_external", "internal"]},
                "channel_type": {"enum": CHANNEL_TYPES},
                "purpose": {"type": "string", "minLength": 5},
                "login_required": {"type": "boolean"},
                "linked_service_key": {"type": "string", "description": "Service that handles messages from this channel."},
                "notes": {"type": "string"},
            },
        },
    },
}


PROPOSE_RULE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_rule",
        "description": (
            "Add a behavioral rule the workspace must follow (e.g. "
            "'never post on Sundays', 'escalate complaints over $1000 to "
            "human', 'require approval before publishing social posts'). "
            "Only call this if the user explicitly described a policy or "
            "escalation path. If the rule governs external actions, include "
            "rule_type and action_patterns so creation can install runtime "
            "guardrails."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "rule_key", "description"],
            "properties": {
                "draft_id": {"type": "string"},
                "rule_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "description": {"type": "string", "minLength": 10},
                "scope": {"type": "string", "description": "Service key the rule applies to, or 'all'."},
                "severity": {"enum": ["block", "warn", "log"]},
                "rule_type": {
                    "enum": ["approval_required", "deny", "draft_only", "notice"],
                    "description": "Runtime meaning when this can be enforced by action key.",
                },
                "action_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Governance action keys, e.g. social_post.publish, email.send, external_message.send, workspace.file.modify, workspace.file.delete.",
                },
            },
        },
    },
}


PROPOSE_AUTOMATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_automation",
        "description": (
            "Schedule a recurring or triggered automation. Only call this "
            "when the user described a schedule or event-based trigger."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "automation_key", "description", "trigger"],
            "properties": {
                "draft_id": {"type": "string"},
                "automation_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "description": {"type": "string", "minLength": 10},
                "trigger": {"type": "string", "description": "e.g. 'daily 08:00', 'on_message_received', 'weekly mon 09:00'"},
                "service_key": {"type": "string"},
            },
        },
    },
}


SET_EVALUATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_set_evaluation",
        "description": (
            "Set the workspace's evaluation scorecard. Should map every "
            "goal to a metric and include a cadence + target_score."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "cadence", "scorecard"],
            "properties": {
                "draft_id": {"type": "string"},
                "cadence": {"enum": CADENCE_VALUES},
                "scorecard": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["metric_key", "weight"],
                        "properties": {
                            "metric_key": {"type": "string"},
                            "weight": {"type": "number", "minimum": 0, "maximum": 1},
                            "goal_key": {"type": "string"},
                        },
                    },
                },
                "target_score": {"type": "number"},
                "warning_score": {"type": "number"},
            },
        },
    },
}


SET_BUDGET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_set_budget",
        "description": (
            "Set the draft's optional monthly workspace budget cap. "
            "Budget is user-facing credits, not USD. Use 0 or null to "
            "leave the workspace uncapped."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "monthly_budget_credits": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Monthly credit cap. 0 means no cap.",
                },
                "auto_pause_on_budget": {
                    "type": "boolean",
                    "description": "Pause the workspace automatically when the cap is reached. Defaults true.",
                },
                "notes": {
                    "type": "string",
                    "description": "Brief rationale or user instruction for the cap.",
                },
            },
        },
    },
}


REMOVE_FIELD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_remove",
        "description": (
            "Remove a previously-proposed item by its key. Use when the "
            "user changes their mind about a service/goal/rule/automation "
            "or wants to clear a missing-integration warning."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "kind", "key"],
            "properties": {
                "draft_id": {"type": "string"},
                "kind": {"enum": ["service", "goal", "rule", "automation", "channel", "agent_mapping", "integration"]},
                "key": {"type": "string"},
            },
        },
    },
}


SEARCH_ENTITY_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_search_entity_agents",
        "description": (
            "List the entity's available agents (and platform marketplace "
            "templates) so you can pick a real agent_id for "
            "ws_propose_agent_mapping. Always call this BEFORE proposing "
            "any agent mapping. Returns id + name + description for each."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "query": {"type": "string", "description": "Optional substring filter on name/description."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
}


SEARCH_BLUEPRINTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_search_blueprints",
        "description": (
            "Search the workspace blueprint marketplace. Use early in the "
            "conversation: if a published blueprint clearly matches the "
            "user's intent, suggest it instead of building from scratch."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "query"],
            "properties": {
                "draft_id": {"type": "string"},
                "query": {"type": "string", "minLength": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
    },
}


GET_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_get_draft",
        "description": (
            "Read the current draft state. Use to recover context if "
            "you've forgotten what's already been committed."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {"draft_id": {"type": "string"}},
        },
    },
}


LINT_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_lint_draft",
        "description": (
            "Run a self-check pass. Returns issues like services without "
            "agent_mapping, goals without cadence/target, channels without "
            "linked_service. Call near the end before mark_ready, and fix "
            "the issues you find."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {"draft_id": {"type": "string"}},
        },
    },
}


MARK_READY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_mark_ready",
        "description": (
            "Flip the draft to ready=true so the user can click Create. "
            "Only call this AFTER ws_lint_draft returns no P0 issues. "
            "If lint still reports unfixable issues, do not mark ready."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {"draft_id": {"type": "string"}},
        },
    },
}


ALL_TOOL_SCHEMAS = [
    COMMIT_BASICS_SCHEMA,
    PROPOSE_SERVICE_SCHEMA,
    PROPOSE_GOAL_SCHEMA,
    PROPOSE_AGENT_MAPPING_SCHEMA,
    REQUEST_CUSTOM_AGENT_SCHEMA,
    ASSIGN_STAFF_SCHEMA,
    ATTACH_KNOWLEDGE_SCHEMA,
    FLAG_MISSING_INTEGRATION_SCHEMA,
    SEARCH_CAPABILITIES_SCHEMA,
    PROPOSE_CHANNEL_SCHEMA,
    PROPOSE_RULE_SCHEMA,
    PROPOSE_AUTOMATION_SCHEMA,
    SET_EVALUATION_SCHEMA,
    SET_BUDGET_SCHEMA,
    REMOVE_FIELD_SCHEMA,
    SEARCH_ENTITY_AGENTS_SCHEMA,
    SEARCH_BLUEPRINTS_SCHEMA,
    GET_DRAFT_SCHEMA,
    LINT_DRAFT_SCHEMA,
    MARK_READY_SCHEMA,
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ULID_RE = re.compile(r"^[A-Z0-9]{26}$")
_CHANNEL_ACTION_PATTERNS = {
    "email": {"email.send", "email.delete"},
}
_CHANNEL_BINDING_ALIASES = {
    "email": {"email", "gmail", "outlook", "smtp", "sendgrid"},
}
_CHANNEL_TEXT_SCRUB_SKIP_KEYS = {
    "_removed_channels",
    "agent_id",
    "automation_key",
    "cadence",
    "channel_type",
    "goal_key",
    "integration_ids",
    "linked_service_key",
    "linked_service_keys",
    "metric_key",
    "mode",
    "provider",
    "recommended_agent_id",
    "rule_key",
    "service_key",
    "staff_id",
    "status",
    "strategy",
    "template_group_id",
}
_CHANNEL_BINDING_FILTER_KEYS = {"action_patterns", "tool_bindings", "mcp_bindings"}


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False)


async def _load_draft(db, draft_id: str, entity_id: str):
    """Load a draft scoped to entity_id. Returns None if not found / mismatched."""
    from packages.core.services.workspace_draft_service import get_draft
    if not draft_id:
        return None
    return await get_draft(db, draft_id, entity_id)


def _replace_in_list(lst: List[Dict[str, Any]], key_field: str, key_value: str, new_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Replace an item with matching key, or append if absent."""
    out: List[Dict[str, Any]] = []
    replaced = False
    for item in lst:
        if (item or {}).get(key_field) == key_value:
            out.append(new_item)
            replaced = True
        else:
            out.append(item)
    if not replaced:
        out.append(new_item)
    return out


def _channel_key(value: Any) -> str:
    return _as_nonempty_str(value).lower().replace("-", "_")


def _channel_matches(block: Any, key: str) -> bool:
    if not isinstance(block, dict):
        return False
    return _channel_key(block.get("channel_type") or block.get("provider")) == key


def _replacement_for_match(match: re.Match[str], replacement: str) -> str:
    source = match.group(0)
    words = [w for w in re.split(r"[\s-]+", source) if w]
    if source.isupper():
        return replacement.upper()
    if words and all(w[:1].isupper() for w in words):
        return " ".join(part.capitalize() for part in replacement.split(" "))
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _scrub_removed_channel_text(value: Any, key: str) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if key != "email":
        return value
    text = value
    replacements = [
        (r"\bwebchat\s+(?:or|and)\s+e-?mail\b", "webchat"),
        (r"\be-?mail\s+(?:or|and)\s+webchat\b", "webchat"),
        (r"\bvia\s+e-?mail\b", "via the approved external channel"),
        (r"\be-?mail\s*,\s*chat reply\s*,?\s*(?:or\s*)?", "webchat reply, "),
        (r"\be-?mail\s+drafting\b", "message drafting"),
        (r"\be-?mail\s+drafter\b", "message drafter"),
        (r"\be-?mail\s+templates?\b", "message templates"),
        (r"\be-?mail\s+drafts?\b", "message drafts"),
        (r"\be-?mail\s+references\b", "removed-channel references"),
        (r"\ba\s+drafted\s+follow-up\s+e-?mail\b", "a drafted follow-up message"),
        (r"\bdrafted\s+follow-up\s+e-?mails\b", "drafted follow-up messages"),
        (r"\bdrafted\s+follow-up\s+e-?mail\b", "drafted follow-up message"),
        (r"\bfollow-up\s+e-?mails\b", "follow-up messages"),
        (r"\bfollow-up\s+e-?mail\b", "follow-up message"),
        (r"\be-?mail\s+send(?:ing)?\b", "external message send"),
        (r"\bsending\s+e-?mail\b", "sending external messages"),
        (r"\be-?mails\b", "messages"),
        (r"\be-?mail\b", "message"),
    ]
    for pattern, repl in replacements:
        text = re.sub(
            pattern,
            lambda match, replacement=repl: _replacement_for_match(match, replacement),
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(
        r"\ba\s+drafted\s+follow-up\s+messages\b",
        "a drafted follow-up message",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _filter_removed_channel_bindings(value: Any, key: str, parent_key: str) -> tuple[Any, int]:
    if not isinstance(value, list):
        return value, 0
    removed_patterns = _CHANNEL_ACTION_PATTERNS.get(key, set())
    aliases = _CHANNEL_BINDING_ALIASES.get(key, {key})
    out: List[Any] = []
    removed_count = 0
    for item in value:
        text = _as_nonempty_str(item).lower()
        if parent_key == "action_patterns":
            should_remove = text in removed_patterns
        else:
            should_remove = any(alias in text for alias in aliases)
        if should_remove:
            removed_count += 1
            continue
        out.append(item)
    return out, removed_count


def _scrub_removed_channel_value(value: Any, key: str, parent_key: str = "") -> tuple[Any, int]:
    if parent_key in _CHANNEL_BINDING_FILTER_KEYS:
        return _filter_removed_channel_bindings(value, key, parent_key)
    if parent_key in _CHANNEL_TEXT_SCRUB_SKIP_KEYS:
        return value, 0
    if isinstance(value, str):
        scrubbed = _scrub_removed_channel_text(value, key)
        return scrubbed, int(scrubbed != value)
    if isinstance(value, list):
        changed = 0
        out = []
        for item in value:
            scrubbed, count = _scrub_removed_channel_value(item, key, parent_key)
            out.append(scrubbed)
            changed += count
        return out, changed
    if isinstance(value, dict):
        changed = 0
        out = {}
        for child_key, child_value in value.items():
            scrubbed, count = _scrub_removed_channel_value(child_value, key, str(child_key))
            out[child_key] = scrubbed
            changed += count
        return out, changed
    return value, 0


def _automation_signature(item: Dict[str, Any]) -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    trigger = re.sub(r"\s+", " ", _as_nonempty_str(item.get("trigger")).lower())
    service_key = _as_nonempty_str(item.get("service_key")).lower()
    return trigger, service_key


def _dedupe_automations(automations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the last automation for a trigger/service pair.

    Architect revisions often rename an automation while keeping the same
    trigger and service. Treat that as replacement so the UI does not show
    both stale and updated rows after a user asks for a change.
    """
    latest: dict[tuple[str, str], int] = {}
    for idx, item in enumerate(automations):
        sig = _automation_signature(item)
        if sig[0]:
            latest[sig] = idx
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(automations):
        sig = _automation_signature(item)
        if sig[0] and latest.get(sig) != idx:
            continue
        out.append(item)
    return out


def _dedupe_by_string_field(items: List[Any], field_name: str) -> tuple[List[Any], int]:
    latest: dict[str, int] = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = re.sub(r"\s+", " ", _as_nonempty_str(item.get(field_name)).lower())
        if key:
            latest[key] = idx
    out: List[Any] = []
    removed = 0
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            out.append(item)
            continue
        key = re.sub(r"\s+", " ", _as_nonempty_str(item.get(field_name)).lower())
        if key and latest.get(key) != idx:
            removed += 1
            continue
        out.append(item)
    return out, removed


def _cleanup_removed_channel_references(fields: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Remove rule/automation references to a channel the user removed.

    ``ws_remove(kind="channel")`` is a semantic operation, not just a list
    edit. If a channel disappears, action keys and automation text that
    still reference it would make the draft preview lie to the user.
    """
    key = _channel_key(key)
    if not key:
        return {"rules": 0, "automations": 0, "action_patterns": 0}

    removed = list(fields.get("_removed_channels") or [])
    if key not in removed:
        removed.append(key)
    fields["_removed_channels"] = removed

    removed_patterns = _CHANNEL_ACTION_PATTERNS.get(key, set())
    rule_changes = 0
    pattern_changes = 0
    rules: List[Dict[str, Any]] = []
    for raw in fields.get("rules") or []:
        if not isinstance(raw, dict):
            rules.append(raw)
            continue
        rule = dict(raw)
        before_patterns = list(rule.get("action_patterns") or [])
        if before_patterns and removed_patterns:
            after_patterns = [
                p for p in before_patterns
                if _as_nonempty_str(p).lower() not in removed_patterns
            ]
            if after_patterns != before_patterns:
                pattern_changes += len(before_patterns) - len(after_patterns)
                if after_patterns:
                    rule["action_patterns"] = after_patterns
                else:
                    rule.pop("action_patterns", None)
                rule_changes += 1
        for field_name in ("description", "scope", "notes"):
            scrubbed = _scrub_removed_channel_text(rule.get(field_name), key)
            if scrubbed != rule.get(field_name):
                rule[field_name] = scrubbed
                rule_changes += 1
        rules.append(rule)
    fields["rules"] = rules

    automation_changes = 0
    automations: List[Dict[str, Any]] = []
    for raw in fields.get("automations") or []:
        if not isinstance(raw, dict):
            automations.append(raw)
            continue
        automation = dict(raw)
        for field_name in ("description", "trigger", "notes"):
            scrubbed = _scrub_removed_channel_text(automation.get(field_name), key)
            if scrubbed != automation.get(field_name):
                automation[field_name] = scrubbed
                automation_changes += 1
        automations.append(automation)
    deduped = _dedupe_automations(automations)
    automation_changes += max(0, len(automations) - len(deduped))
    fields["automations"] = deduped

    scrubbed_fields, text_changes = _scrub_removed_channel_value(fields, key)
    if isinstance(scrubbed_fields, dict):
        fields.clear()
        fields.update(scrubbed_fields)

    knowledge = list(fields.get("knowledge_attachments") or [])
    deduped_knowledge, knowledge_removed = _dedupe_by_string_field(knowledge, "name")
    if knowledge_removed:
        fields["knowledge_attachments"] = deduped_knowledge

    return {
        "rules": rule_changes,
        "automations": automation_changes,
        "action_patterns": pattern_changes,
        "text_fields": text_changes,
        "knowledge_attachments": knowledge_removed,
    }


def _reconcile_removed_channel_references(fields: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "rules": 0,
        "automations": 0,
        "action_patterns": 0,
        "text_fields": 0,
        "knowledge_attachments": 0,
    }
    for channel in list(fields.get("_removed_channels") or []):
        cleanup = _cleanup_removed_channel_references(fields, _channel_key(channel))
        for key, value in cleanup.items():
            summary[key] = summary.get(key, 0) + int(value or 0)
    return summary


def _scrub_new_item_for_removed_channels(item: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Any = item
    for channel in list(fields.get("_removed_channels") or []):
        cleaned, _ = _scrub_removed_channel_value(cleaned, _channel_key(channel))
    return cleaned if isinstance(cleaned, dict) else item


def _as_nonempty_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _as_optional_nonnegative_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _service_key_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [_as_nonempty_str(v) for v in value if _as_nonempty_str(v)]
    return []


def _mapping_missing_integrations(mapping: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(mapping, dict):
        return []
    create_draft = mapping.get("create_agent_draft") or {}
    raw = create_draft.get("missing_integrations") or mapping.get("missing_integrations") or []
    return [item for item in raw if isinstance(item, dict)]


def _remove_stale_agent_design_flags(
    fields: Dict[str, Any],
    *,
    service_key: str,
    previous_mapping: Optional[Dict[str, Any]],
) -> None:
    """Remove integration warnings that belonged to the prior agent design.

    A custom-agent redesign can turn a previously missing integration into a
    normal bound capability. If we only replace the agent mapping, the sidebar
    keeps showing stale "needs setup" warnings from the old design.
    """
    stale_providers = {
        _as_nonempty_str(mi.get("provider"))
        for mi in _mapping_missing_integrations(previous_mapping)
        if _as_nonempty_str(mi.get("provider"))
    }
    if not stale_providers:
        return

    previous_agent_name = _as_nonempty_str(
        ((previous_mapping or {}).get("create_agent_draft") or {}).get("agent_name")
    )
    next_flags: List[Any] = []
    changed = False
    for raw_flag in list(fields.get("flagged_integrations") or []):
        if not isinstance(raw_flag, dict):
            next_flags.append(raw_flag)
            continue
        provider = _as_nonempty_str(raw_flag.get("provider"))
        linked_service_keys = _service_key_list(raw_flag.get("linked_service_keys"))
        if provider not in stale_providers or service_key not in linked_service_keys:
            next_flags.append(raw_flag)
            continue

        source = _as_nonempty_str(raw_flag.get("source"))
        agent_name = _as_nonempty_str(raw_flag.get("agent_name"))
        if source and source != "agent_design":
            next_flags.append(raw_flag)
            continue
        if previous_agent_name and agent_name and agent_name != previous_agent_name:
            next_flags.append(raw_flag)
            continue

        remaining_service_keys = [sk for sk in linked_service_keys if sk != service_key]
        if remaining_service_keys:
            updated = dict(raw_flag)
            updated["linked_service_keys"] = remaining_service_keys
            next_flags.append(updated)
        changed = True

    if changed:
        fields["flagged_integrations"] = next_flags


async def _merge_agent_missing_integration_flags(
    db,
    fields: Dict[str, Any],
    *,
    entity_id: str,
    user_id: str,
    service_key: str,
    create_draft: Dict[str, Any],
) -> None:
    from packages.core.services.integration_resolution import resolve_missing_integration_provider

    missing_integrations = [
        mi for mi in list(create_draft.get("missing_integrations") or [])
        if isinstance(mi, dict)
    ]
    flagged: List[Any] = list(fields.get("flagged_integrations") or [])
    for mi in missing_integrations:
        requested_provider = _as_nonempty_str(mi.get("provider"))
        if not requested_provider:
            continue
        resolved = await resolve_missing_integration_provider(
            db,
            entity_id=entity_id,
            user_id=user_id or None,
            provider=requested_provider,
        )
        if resolved is None:
            continue
        provider = resolved.provider
        existing: Optional[Dict[str, Any]] = None
        for flag in flagged:
            if isinstance(flag, dict) and _as_nonempty_str(flag.get("provider")) == provider:
                existing = flag
                break
        if existing is not None:
            linked = _service_key_list(existing.get("linked_service_keys"))
            if service_key not in linked:
                linked.append(service_key)
            existing["linked_service_keys"] = linked
            if not existing.get("purpose") and mi.get("purpose"):
                existing["purpose"] = mi.get("purpose", "")
            existing["required"] = bool(existing.get("required", False) or mi.get("required", True))
            existing.setdefault("source", "agent_design")
            existing.setdefault("agent_name", create_draft.get("agent_name", ""))
            if resolved.covered_provider:
                existing.setdefault("covered_provider", resolved.covered_provider)
            continue
        flag = {
            "provider": provider,
            "purpose": mi.get("purpose", ""),
            "required": bool(mi.get("required", True)),
            "linked_service_keys": [service_key],
            "source": "agent_design",
            "agent_name": create_draft.get("agent_name", ""),
        }
        if resolved.covered_provider:
            flag["covered_provider"] = resolved.covered_provider
        flagged.append(flag)
    fields["flagged_integrations"] = flagged


def _reconcile_agent_design_flags(fields: Dict[str, Any]) -> None:
    """Keep agent-design integration flags aligned with current mappings."""
    expected: set[tuple[str, str]] = set()
    expected_by_provider: Dict[str, Dict[str, Any]] = {}
    for mapping in list(fields.get("agent_mappings") or []):
        if not isinstance(mapping, dict):
            continue
        service_key = _as_nonempty_str(mapping.get("service_key"))
        if not service_key:
            continue
        for mi in _mapping_missing_integrations(mapping):
            provider = _as_nonempty_str(mi.get("provider"))
            if provider:
                expected.add((provider, service_key))
                provider_info = expected_by_provider.setdefault(provider, {
                    "provider": provider,
                    "purpose": mi.get("purpose", ""),
                    "required": bool(mi.get("required", True)),
                    "linked_service_keys": [],
                    "source": "agent_design",
                    "agent_name": ((mapping.get("create_agent_draft") or {}).get("agent_name") or ""),
                })
                if service_key not in provider_info["linked_service_keys"]:
                    provider_info["linked_service_keys"].append(service_key)
                provider_info["required"] = bool(provider_info.get("required", False) or mi.get("required", True))
                if not provider_info.get("purpose") and mi.get("purpose"):
                    provider_info["purpose"] = mi.get("purpose", "")

    next_flags: List[Any] = []
    changed = False
    for raw_flag in list(fields.get("flagged_integrations") or []):
        if not isinstance(raw_flag, dict):
            next_flags.append(raw_flag)
            continue
        source = _as_nonempty_str(raw_flag.get("source"))
        agent_name = _as_nonempty_str(raw_flag.get("agent_name"))
        if source != "agent_design" and not (agent_name and not source):
            next_flags.append(raw_flag)
            continue

        provider = _as_nonempty_str(raw_flag.get("provider"))
        linked_service_keys = _service_key_list(raw_flag.get("linked_service_keys"))
        retained_service_keys = [
            sk for sk in linked_service_keys
            if provider and (provider, sk) in expected
        ]
        if retained_service_keys:
            if retained_service_keys != linked_service_keys:
                updated = dict(raw_flag)
                updated["linked_service_keys"] = retained_service_keys
                next_flags.append(updated)
                changed = True
            else:
                next_flags.append(raw_flag)
            continue
        changed = True

    for provider, provider_info in expected_by_provider.items():
        existing = next(
            (
                flag for flag in next_flags
                if isinstance(flag, dict) and _as_nonempty_str(flag.get("provider")) == provider
            ),
            None,
        )
        if existing is None:
            next_flags.append(provider_info)
            changed = True
            continue
        linked_service_keys = _service_key_list(existing.get("linked_service_keys"))
        for service_key in provider_info["linked_service_keys"]:
            if service_key not in linked_service_keys:
                linked_service_keys.append(service_key)
                changed = True
        existing["linked_service_keys"] = linked_service_keys
        if not existing.get("purpose") and provider_info.get("purpose"):
            existing["purpose"] = provider_info["purpose"]
            changed = True
        merged_required = bool(existing.get("required", False) or provider_info.get("required", True))
        if existing.get("required") != merged_required:
            existing["required"] = merged_required
            changed = True
        if not existing.get("source"):
            existing["source"] = "agent_design"
            changed = True
        if not existing.get("agent_name") and provider_info.get("agent_name"):
            existing["agent_name"] = provider_info["agent_name"]
            changed = True

    if changed:
        fields["flagged_integrations"] = next_flags


async def _persist(db, draft) -> None:
    """Mark fields dirty so the session tracks the mutation.

    We intentionally do NOT flush here — the agentic loop may execute
    multiple tool calls concurrently (asyncio.gather), and concurrent
    flushes on the same session raise "Session is already flushing".
    The session auto-flushes before any subsequent SELECT (_load_draft)
    or at commit time, so data visibility is preserved.
    """
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(draft, "fields")


# ── ws_commit_basics ────────────────────────────────────────────────────────

async def _commit_basics(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    for k in ("name", "kind", "operating_context", "primary_work", "category", "description"):
        v = kwargs.get(k)
        if v is not None and v != "":
            fields[k] = v
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"committed": ["name", "kind", "operating_context", "primary_work"]})


# ── ws_propose_service ──────────────────────────────────────────────────────

async def _propose_service(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    service_key = kwargs.get("service_key", "")
    if not _SLUG_RE.match(service_key):
        return _err("service_key must be snake_case", got=service_key)

    new_service = {
        "service_key": service_key,
        "name": kwargs.get("name", ""),
        "description": kwargs.get("description", ""),
        "autonomy_level": kwargs.get("autonomy_level", "supervised"),
        "owner_role": kwargs.get("owner_role", ""),
    }
    if kwargs.get("rationale"):
        new_service["rationale"] = kwargs["rationale"]

    fields = dict(draft.fields or {})
    new_service = _scrub_new_item_for_removed_channels(new_service, fields)
    fields["services"] = _replace_in_list(
        fields.get("services") or [], "service_key", service_key, new_service,
    )
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"service_key": service_key, "service_count": len(fields["services"])})


# ── ws_propose_goal ─────────────────────────────────────────────────────────

async def _propose_goal(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    goal_key = kwargs.get("goal_key", "")
    if not _SLUG_RE.match(goal_key):
        return _err("goal_key must be snake_case", got=goal_key)
    target = kwargs.get("target", "")
    cadence = kwargs.get("cadence", "")
    if not target:
        return _err("target is required (e.g. '10000', '5%') -- infer from description if user didn't specify")
    if cadence not in CADENCE_VALUES:
        return _err(f"cadence must be one of {CADENCE_VALUES}", got=cadence)

    goal = {
        "goal_key": goal_key,
        "title": kwargs.get("title", ""),
        "description": kwargs.get("description", ""),
        "target": str(target),
        "cadence": cadence,
    }
    if kwargs.get("metric_key"):
        goal["metric_key"] = kwargs["metric_key"]
    if kwargs.get("rationale"):
        goal["rationale"] = kwargs["rationale"]

    fields = dict(draft.fields or {})
    goal = _scrub_new_item_for_removed_channels(goal, fields)
    fields["goals"] = _replace_in_list(
        fields.get("goals") or [], "goal_key", goal_key, goal,
    )
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"goal_key": goal_key, "goal_count": len(fields["goals"])})


# ── ws_propose_agent_mapping ────────────────────────────────────────────────

async def _propose_agent_mapping(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.workspace import Agent

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    agent_id = kwargs.get("agent_id", "")
    if not _ULID_RE.match(agent_id):
        return _err("agent_id is not a valid ULID", got=agent_id, hint="call ws_search_entity_agents to get valid ids")

    # Verify the agent really exists and is in scope (entity_id match OR public template).
    result = await db.execute(
        select(Agent).where(
            Agent.id == agent_id,
            Agent.deleted_at.is_(None),
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        return _err("agent_id does not exist", got=agent_id)
    if agent.entity_id and agent.entity_id != entity_id:
        return _err("agent belongs to another entity", got=agent_id)

    service_key = kwargs.get("service_key", "")
    mapping = {
        "service_key": service_key,
        "agent_id": agent_id,
        "recommended_agent_id": agent_id,
        "recommended_agent_name": agent.name,
        "strategy": "match",
        "rationale": kwargs.get("rationale", ""),
    }
    fields = dict(draft.fields or {})
    previous_mapping = next(
        (
            item for item in (fields.get("agent_mappings") or [])
            if (item or {}).get("service_key") == service_key
        ),
        None,
    )
    _remove_stale_agent_design_flags(
        fields,
        service_key=service_key,
        previous_mapping=previous_mapping,
    )
    fields["agent_mappings"] = _replace_in_list(
        fields.get("agent_mappings") or [], "service_key", service_key, mapping,
    )
    _reconcile_agent_design_flags(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"service_key": service_key, "agent_id": agent_id, "agent_name": agent.name})


# ── ws_request_custom_agent ─────────────────────────────────────────────────

async def _request_custom_agent(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    service_key = kwargs.get("service_key", "")
    # Accept both new richer fields and the old "system_prompt_seed" name
    # so older calls keep working while the architect upgrades.
    system_prompt = kwargs.get("system_prompt") or kwargs.get("system_prompt_seed", "")

    create_draft = {
        "agent_name": kwargs.get("agent_name", ""),
        "agent_description": kwargs.get("agent_description", ""),
        "system_prompt": system_prompt,
        "tool_bindings": list(kwargs.get("tool_bindings") or []),
        "business_capabilities": list(kwargs.get("business_capabilities") or []),
        "skill_bindings": list(kwargs.get("skill_bindings") or []),
        "mcp_bindings": list(kwargs.get("mcp_bindings") or []),
        "missing_skill_specs": list(kwargs.get("missing_skill_specs") or []),
        "missing_integrations": list(kwargs.get("missing_integrations") or []),
    }
    mapping = {
        "service_key": service_key,
        "strategy": "create_custom",
        "create_agent_draft": create_draft,
        "rationale": kwargs.get("rationale", ""),
    }
    fields = dict(draft.fields or {})
    previous_mapping = next(
        (
            item for item in (fields.get("agent_mappings") or [])
            if (item or {}).get("service_key") == service_key
        ),
        None,
    )
    _remove_stale_agent_design_flags(
        fields,
        service_key=service_key,
        previous_mapping=previous_mapping,
    )
    fields["agent_mappings"] = _replace_in_list(
        fields.get("agent_mappings") or [], "service_key", service_key, mapping,
    )
    # Roll any current agent-level missing_integrations up into a
    # workspace-wide flagged_integrations list, after removing stale
    # warnings created by the previous design for this service.
    await _merge_agent_missing_integration_flags(
        db,
        fields,
        entity_id=entity_id,
        user_id=user_id,
        service_key=service_key,
        create_draft=create_draft,
    )
    _reconcile_agent_design_flags(fields)
    _reconcile_removed_channel_references(fields)

    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "service_key": service_key,
        "strategy": "create_custom",
        "tool_bindings": len(create_draft["tool_bindings"]),
        "business_capabilities": len(create_draft["business_capabilities"]),
        "skill_bindings": len(create_draft["skill_bindings"]),
        "mcp_bindings": len(create_draft["mcp_bindings"]),
        "missing_skill_specs": len(create_draft["missing_skill_specs"]),
        "missing_integrations": len(create_draft["missing_integrations"]),
    })


async def _assign_staff(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.staff import Staff

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    staff_id = kwargs.get("staff_id", "")
    if not _ULID_RE.match(staff_id):
        return _err("staff_id must be a 26-char ULID", got=staff_id, hint="call ws_search_capabilities for staff ids")

    # Verify the staff member exists in this entity.
    result = await db.execute(
        select(Staff).where(Staff.id == staff_id, Staff.deleted_at.is_(None))
    )
    staff = result.scalar_one_or_none()
    if staff is None:
        return _err("staff_id not found", got=staff_id)
    if staff.entity_id != entity_id:
        return _err("staff belongs to another entity", got=staff_id)

    role = (kwargs.get("role") or "").strip() or "member"
    assignment = {
        "staff_id": staff_id,
        "staff_name": staff.name or staff.display_name or staff_id,
        "role": role,
        "service_key": kwargs.get("service_key") or None,
        "rationale": kwargs.get("rationale") or "",
    }

    fields = dict(draft.fields or {})
    assignments = list(fields.get("staff_assignments") or [])
    assignments = [a for a in assignments if (a or {}).get("staff_id") != staff_id]
    assignments.append(assignment)
    fields["staff_assignments"] = assignments
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"staff_id": staff_id, "staff_name": assignment["staff_name"], "role": role})


async def _attach_knowledge(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    name = (kwargs.get("name") or "").strip()
    if not name:
        return _err("name is required")
    purpose = (kwargs.get("purpose") or "").strip()
    mode = kwargs.get("mode") or "create_new"
    if mode not in ("create_new", "clone_template"):
        return _err("mode must be create_new | clone_template", got=mode)

    if mode == "clone_template":
        template_group_id = (kwargs.get("template_group_id") or "").strip()
        if not _ULID_RE.match(template_group_id):
            return _err("template_group_id must be a ULID for mode=clone_template")
    else:
        template_group_id = None

    attachment = {
        "name": name,
        "purpose": purpose,
        "mode": mode,
        "template_group_id": template_group_id,
        "linked_service_keys": list(kwargs.get("linked_service_keys") or []),
        "generate_starter_doc": kwargs.get("generate_starter_doc") if kwargs.get("generate_starter_doc") is not None else (mode == "create_new"),
        "approved": True,
        "rationale": kwargs.get("rationale") or "",
    }

    fields = dict(draft.fields or {})
    items = list(fields.get("knowledge_attachments") or [])
    items = [k for k in items if (k or {}).get("name") != name]
    attachment = _scrub_new_item_for_removed_channels(attachment, fields)
    items.append(attachment)
    fields["knowledge_attachments"] = items
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "name": name,
        "mode": mode,
        "generate_starter_doc": attachment["generate_starter_doc"],
    })


async def _flag_missing_integration(db, *, entity_id: str, user_id: str = "", **kwargs):
    from packages.core.services.integration_resolution import resolve_missing_integration_provider

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    requested_provider = kwargs.get("provider", "")
    if not requested_provider:
        return _err("provider required")
    resolved = await resolve_missing_integration_provider(
        db,
        entity_id=entity_id,
        user_id=user_id or None,
        provider=requested_provider,
    )
    if resolved is None:
        return _ok({
            "provider": requested_provider,
            "flagged_count": len((draft.fields or {}).get("flagged_integrations") or []),
            "skipped": True,
            "reason": "unsupported_or_already_connected",
        })
    provider = resolved.provider

    fields = dict(draft.fields or {})
    flagged = list(fields.get("flagged_integrations") or [])
    # Replace if already flagged (e.g. add more linked_service_keys).
    flagged = [f for f in flagged if (f or {}).get("provider") != provider]
    flagged.append({
        "provider": provider,
        "purpose": kwargs.get("purpose", ""),
        "required": bool(kwargs.get("required", True)),
        "linked_service_keys": list(kwargs.get("linked_service_keys") or []),
        "source": "explicit",
    })
    if resolved.covered_provider:
        flagged[-1]["covered_provider"] = resolved.covered_provider
    fields["flagged_integrations"] = flagged
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "provider": provider,
        "requested_provider": requested_provider,
        "covered_provider": resolved.covered_provider,
        "flagged_count": len(flagged),
    })


async def _search_capabilities(db, *, entity_id: str, user_id: str = "", **kwargs):
    """One-shot capability discovery for the architect.

    Returns three parallel lists -- tools (from tool_pool), skills
    (entity + public), and integrations (with their backing MCP servers
    so the architect can bind ``mcp_bindings`` correctly).
    """
    from sqlalchemy import case, select, or_
    from packages.core.models.skill import Skill
    from packages.core.models.document import Integration
    from packages.core.models.mcp import MCPServer
    from packages.core.ai.runtime import legacy_tool_is_eager_for_profile
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.runtime.tool_registry import (
        runtime_registered_tool_schemas,
    )

    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id)
        if draft is None:
            return _err("draft not found")

    q = (kwargs.get("query") or "").strip().lower()
    limit = int(kwargs.get("limit_per_kind", 30))

    # ── Business capabilities ── preferred runtime-level bindings.
    business_capabilities_out = []
    for capability in CORE_CAPABILITIES.values():
        if capability.id in {"workspace.architect", "file.patch"}:
            continue
        text = " ".join([capability.id, capability.name, capability.description]).lower()
        if q and q not in text:
            continue
        business_capabilities_out.append({
            "id": capability.id,
            "name": capability.name,
            "description": capability.description,
            "tool_names": list(capability.tool_names),
            "risk_level": capability.risk_level,
            "required_approval": capability.required_approval,
        })
        if len(business_capabilities_out) >= limit:
            break

    # ── Tools ── only safely-bindable ones the platform pre-approves.
    tools_out = []
    try:
        registered_tool_schemas = runtime_registered_tool_schemas()
    except Exception:
        registered_tool_schemas = ()
    for name, schema in registered_tool_schemas:
        if name.startswith("mcp__"):
            continue  # MCP tools are addressed via mcp_bindings, not direct tool_bindings.
        if name.startswith("ws_"):
            continue  # workspace_architect's own tools are private to this skill.
        fn = (schema.get("function") or {})
        desc = (fn.get("description") or "")[:240]
        if q and q not in name.lower() and q not in desc.lower():
            continue
        tools_out.append({
            "name": name,
            "description": desc,
            "always_loaded": legacy_tool_is_eager_for_profile(name, is_master=True),
        })
        if len(tools_out) >= limit:
            break

    # ── Skills ── public templates + this entity's private skills.
    skill_priority = case((Skill.entity_id == entity_id, 0), else_=1)
    skill_stmt = select(Skill).where(
        Skill.status == "active",
        or_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),
    ).order_by(skill_priority.asc(), Skill.created_at.desc()).limit(max(limit, 80))
    skill_rows = (await db.execute(skill_stmt)).scalars().all()
    skills_out = []
    for s in skill_rows:
        skills_out.append({
            "id": s.id,
            "slug": s.slug,
            "name": s.name,
            "description": (s.description or "")[:200],
            "tools": list(s.tools or []),
            "scope": "entity" if s.entity_id else "public",
        })
        if len(skills_out) >= max(limit, 80):
            break

    # ── Integrations + MCP servers ──
    integ_stmt = select(Integration).where(
        Integration.entity_id == entity_id,
        Integration.status == "active",
    )
    integ_rows = (await db.execute(integ_stmt)).scalars().all()
    integ_by_provider: dict[str, list] = {}
    for i in integ_rows:
        integ_by_provider.setdefault(i.provider, []).append(i)

    mcp_stmt = select(MCPServer).where(MCPServer.status == "active")
    mcp_rows = (await db.execute(mcp_stmt)).scalars().all()

    integrations_out = []
    for m in mcp_rows:
        backing = integ_by_provider.get(m.server_key) or []
        text = " ".join([m.name or "", m.server_key or "", m.description or ""]).lower()
        if q and q not in text:
            continue
        integrations_out.append({
            "mcp_server_key": m.server_key,
            "name": m.name,
            "description": (m.description or "")[:200],
            "auth_type": m.auth_type,
            "active_integration": bool(backing),
            "integration_ids": [i.id for i in backing],
        })

    # ── Nango aggregator ── if the entity has a Nango Integration, list
    # the providers it has configured + the connections it already
    # holds. Lets the architect propose mcp_bindings for platforms that
    # don't have a dedicated MCP server (long-tail SaaS).
    nango_block = await _list_nango_aggregator(db, entity_id, q)
    if nango_block:
        integrations_out.append(nango_block)

    # ── Staff ── entity members the architect can assign to the workspace.
    from packages.core.models.staff import Staff
    from packages.core.models.document import DocumentGroup

    staff_stmt = select(Staff).where(
        Staff.entity_id == entity_id,
        Staff.deleted_at.is_(None),
    ).limit(limit)
    staff_rows = (await db.execute(staff_stmt)).scalars().all()
    staff_out = []
    for s in staff_rows:
        display_name = getattr(s, "display_name", None) or s.name
        role_label = getattr(s, "role", None) or getattr(s, "title", None)
        text = " ".join([s.name or "", display_name or "", s.email or "", role_label or ""]).lower()
        if q and q not in text:
            continue
        staff_out.append({
            "id": s.id,
            "name": display_name,
            "email": s.email,
            "role": role_label,
        })

    # ── Knowledge groups ── entity-level groups the architect can
    # propose as templates to clone, plus any not-yet-assigned groups.
    kn_stmt = select(DocumentGroup).where(
        DocumentGroup.entity_id == entity_id,
    ).limit(limit)
    kn_rows = (await db.execute(kn_stmt)).scalars().all()
    knowledge_out = []
    for k in kn_rows:
        text = " ".join([k.name or ""]).lower()
        if q and q not in text:
            continue
        knowledge_out.append({
            "id": k.id,
            "name": k.name,
            "workspace_id": k.workspace_id,
            "indexed": bool(k.vector_store_id),
        })

    return _ok({
        "business_capabilities": business_capabilities_out,
        "tools": tools_out,
        "skills": skills_out,
        "integrations": integrations_out,
        "staff": staff_out,
        "knowledge": knowledge_out,
    })


# ── ws_propose_channel ──────────────────────────────────────────────────────

async def _propose_channel(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    role = kwargs.get("role", "")
    block = {
        "channel_type": kwargs.get("channel_type", ""),
        "purpose": kwargs.get("purpose", ""),
        "login_required": bool(kwargs.get("login_required", False)),
        "linked_service_key": kwargs.get("linked_service_key", ""),
        "notes": kwargs.get("notes", ""),
    }

    fields = dict(draft.fields or {})
    block_channel_key = _channel_key(block.get("channel_type"))
    if block_channel_key:
        removed_channels = [
            ch for ch in list(fields.get("_removed_channels") or [])
            if _channel_key(ch) != block_channel_key
        ]
        if removed_channels:
            fields["_removed_channels"] = removed_channels
        else:
            fields.pop("_removed_channels", None)
    cc = dict(fields.get("channel_config") or {})
    if role == "primary_external":
        cc["primary_external_channel"] = block
    elif role == "internal":
        cc["internal_channel"] = block
    elif role == "secondary_external":
        secondary = list(cc.get("secondary_external_channels") or [])
        secondary = [
            s for s in secondary if (s or {}).get("channel_type") != block["channel_type"]
        ]
        secondary.append(block)
        cc["secondary_external_channels"] = secondary
    else:
        return _err("role must be primary_external | secondary_external | internal", got=role)
    fields["channel_config"] = cc
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"role": role, "channel_type": block["channel_type"]})


# ── ws_propose_rule ─────────────────────────────────────────────────────────

async def _propose_rule(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    rule_key = kwargs.get("rule_key", "")
    if not _SLUG_RE.match(rule_key):
        return _err("rule_key must be snake_case", got=rule_key)
    fields = dict(draft.fields or {})
    removed_channels = {_channel_key(v) for v in fields.get("_removed_channels") or []}
    rule = {
        "rule_key": rule_key,
        "description": kwargs.get("description", ""),
        "scope": kwargs.get("scope", "all"),
        "severity": kwargs.get("severity", "warn"),
    }
    for removed_channel in removed_channels:
        for field_name in ("description", "scope"):
            rule[field_name] = _scrub_removed_channel_text(rule.get(field_name), removed_channel)
    rule_type = kwargs.get("rule_type")
    if rule_type:
        rule["rule_type"] = rule_type
    action_patterns = [
        str(p).strip() for p in (kwargs.get("action_patterns") or [])
        if str(p or "").strip()
    ]
    if removed_channels:
        removed_patterns = set().union(*(
            _CHANNEL_ACTION_PATTERNS.get(ch, set()) for ch in removed_channels
        ))
        action_patterns = [
            p for p in action_patterns
            if _as_nonempty_str(p).lower() not in removed_patterns
        ]
    if action_patterns:
        rule["action_patterns"] = list(dict.fromkeys(action_patterns))
    fields["rules"] = _replace_in_list(
        fields.get("rules") or [], "rule_key", rule_key, rule,
    )
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"rule_key": rule_key})


# ── ws_propose_automation ───────────────────────────────────────────────────

async def _propose_automation(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    automation_key = kwargs.get("automation_key", "")
    if not _SLUG_RE.match(automation_key):
        return _err("automation_key must be snake_case", got=automation_key)
    automation = {
        "automation_key": automation_key,
        "description": kwargs.get("description", ""),
        "trigger": kwargs.get("trigger", ""),
        "service_key": kwargs.get("service_key", ""),
    }
    fields = dict(draft.fields or {})
    for removed_channel in fields.get("_removed_channels") or []:
        automation["description"] = _scrub_removed_channel_text(automation.get("description"), _channel_key(removed_channel))
        automation["trigger"] = _scrub_removed_channel_text(automation.get("trigger"), _channel_key(removed_channel))
    existing = [
        item for item in (fields.get("automations") or [])
        if _automation_signature(item) != _automation_signature(automation)
    ]
    fields["automations"] = _replace_in_list(existing, "automation_key", automation_key, automation)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"automation_key": automation_key})


# ── ws_set_evaluation ───────────────────────────────────────────────────────

async def _set_evaluation(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")
    fields = dict(draft.fields or {})
    fields["evaluation"] = {
        "enabled": True,
        "cadence": kwargs.get("cadence", "weekly"),
        "scorecard": kwargs.get("scorecard") or [],
        "target_score": kwargs.get("target_score"),
        "warning_score": kwargs.get("warning_score"),
        "notes": kwargs.get("notes", ""),
    }
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"scorecard_size": len(fields["evaluation"]["scorecard"])})


# ── ws_set_budget ───────────────────────────────────────────────────────────

async def _set_budget(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    existing = fields.get("budget_policy") if isinstance(fields.get("budget_policy"), dict) else {}

    if "monthly_budget_credits" in kwargs:
        raw_credits = kwargs.get("monthly_budget_credits")
        monthly_budget_credits = _as_optional_nonnegative_int(raw_credits)
        if monthly_budget_credits is None and raw_credits not in (None, ""):
            return _err("monthly_budget_credits must be a non-negative integer")
    else:
        monthly_budget_credits = _as_optional_nonnegative_int(existing.get("monthly_budget_credits"))

    budget_policy = {
        **existing,
        "monthly_budget_credits": monthly_budget_credits,
        "auto_pause_on_budget": bool(kwargs.get("auto_pause_on_budget", existing.get("auto_pause_on_budget", True))),
        "notes": kwargs.get("notes", existing.get("notes", "")) or "",
    }
    fields["budget_policy"] = budget_policy
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "monthly_budget_credits": monthly_budget_credits,
        "auto_pause_on_budget": budget_policy["auto_pause_on_budget"],
    })


# ── ws_remove ───────────────────────────────────────────────────────────────

async def _remove(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")
    kind = kwargs.get("kind", "")
    key = kwargs.get("key", "")
    fields = dict(draft.fields or {})
    key_field_map = {
        "service": ("services", "service_key"),
        "goal": ("goals", "goal_key"),
        "rule": ("rules", "rule_key"),
        "automation": ("automations", "automation_key"),
        "agent_mapping": ("agent_mappings", "service_key"),
    }
    if kind == "channel":
        channel_key = _channel_key(key)
        cc = dict(fields.get("channel_config") or {})
        if _channel_matches(cc.get("primary_external_channel"), channel_key):
            cc.pop("primary_external_channel", None)
        if _channel_matches(cc.get("internal_channel"), channel_key):
            cc.pop("internal_channel", None)
        secondary = [
            s for s in (cc.get("secondary_external_channels") or [])
            if not _channel_matches(s, channel_key)
        ]
        cc["secondary_external_channels"] = secondary
        if isinstance(cc.get("channels"), list):
            cc["channels"] = [
                s for s in (cc.get("channels") or [])
                if not _channel_matches(s, channel_key)
            ]
        fields["channel_config"] = cc
        cleanup = _cleanup_removed_channel_references(fields, channel_key)
        draft.fields = fields
        await _persist(db, draft)
        return _ok({"kind": "channel", "key": key, "cleanup": cleanup})
    if kind == "integration":
        provider_key = _as_nonempty_str(key)
        before = list(fields.get("flagged_integrations") or [])
        fields["flagged_integrations"] = [
            item for item in before
            if _as_nonempty_str((item or {}).get("provider")).lower() != provider_key.lower()
        ]
        _reconcile_agent_design_flags(fields)
        draft.fields = fields
        await _persist(db, draft)
        return _ok({
            "kind": "integration",
            "key": key,
            "remaining": len(fields.get("flagged_integrations") or []),
        })
    if kind not in key_field_map:
        return _err(f"unsupported kind: {kind}")
    list_key, key_field = key_field_map[kind]
    if kind == "agent_mapping":
        previous_mapping = next(
            (
                item for item in (fields.get(list_key) or [])
                if (item or {}).get(key_field) == key
            ),
            None,
        )
        _remove_stale_agent_design_flags(
            fields,
            service_key=key,
            previous_mapping=previous_mapping,
        )
    fields[list_key] = [
        item for item in (fields.get(list_key) or [])
        if (item or {}).get(key_field) != key
    ]
    if kind == "agent_mapping":
        _reconcile_agent_design_flags(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"kind": kind, "key": key, "remaining": len(fields[list_key])})


# ── ws_search_entity_agents ────────────────────────────────────────────────

async def _search_entity_agents(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select, or_
    from packages.core.models.workspace import Agent

    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id)
        if draft is None:
            return _err("draft not found")

    limit = int(kwargs.get("limit", 30))
    q = (kwargs.get("query") or "").strip().lower()

    stmt = select(Agent).where(
        Agent.deleted_at.is_(None),
        Agent.status == "active",
        or_(
            Agent.entity_id == entity_id,
            Agent.is_template.is_(True),
        ),
    ).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for a in rows:
        if q and q not in (a.name or "").lower() and q not in (a.description or "").lower():
            continue
        out.append({
            "id": a.id,
            "name": a.name,
            "description": (a.description or "")[:200],
            "category": a.category,
            "source": "template" if a.is_template else "entity",
        })
    return _ok({"agents": out, "total": len(out)})


# ── ws_search_blueprints ────────────────────────────────────────────────────

async def _search_blueprints(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.blueprint import WorkspaceBlueprint

    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id)
        if draft is None:
            return _err("draft not found")

    query = (kwargs.get("query") or "").strip().lower()
    limit = int(kwargs.get("limit", 8))
    stmt = (
        select(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.status == "published")
        .order_by(WorkspaceBlueprint.install_count.desc())
        .limit(40)
    )
    rows = (await db.execute(stmt)).scalars().all()
    scored = []
    tokens = set(re.findall(r"[a-z0-9]+", query))
    for bp in rows:
        hay = " ".join([
            bp.title or "", bp.summary or "",
            " ".join(str(t) for t in (bp.tags or [])),
        ]).lower()
        score = sum(1 for t in tokens if t in hay)
        if score == 0 and tokens:
            continue
        scored.append((score, bp))
    scored.sort(key=lambda x: -x[0])
    out = [
        {
            "id": bp.id,
            "title": bp.title,
            "summary": bp.summary,
            "tags": list(bp.tags or []),
            "install_count": int(bp.install_count or 0),
        }
        for _, bp in scored[:limit]
    ]
    return _ok({"blueprints": out, "total": len(out)})


# ── ws_get_draft ────────────────────────────────────────────────────────────

async def _get_draft(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")
    return _ok({
        "draft": {
            "id": draft.id,
            "status": draft.status,
            "ready": bool(draft.ready),
            "missing": list(draft.missing or []),
            "fields": dict(draft.fields or {}),
        },
    })


# ── ws_lint_draft ───────────────────────────────────────────────────────────

async def _lint_draft(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    issues: List[Dict[str, Any]] = []

    # Top-level basics
    for required in ("name", "kind", "operating_context", "primary_work"):
        if not (fields.get(required) or "").strip():
            issues.append({"severity": "P0", "where": required, "message": f"{required} is empty -- call ws_commit_basics."})

    services = fields.get("services") or []
    if not services:
        issues.append({"severity": "P0", "where": "services", "message": "No services defined -- call ws_propose_service at least once."})
    elif len(services) < 2:
        issues.append({"severity": "P1", "where": "services", "message": "Only 1 service -- consider whether the workspace needs at least 2."})

    service_keys = {(s or {}).get("service_key") for s in services}

    # Each service must have a mapping
    mappings = fields.get("agent_mappings") or []
    mapped_keys = {(m or {}).get("service_key") for m in mappings}
    for svc in services:
        sk = (svc or {}).get("service_key")
        if sk and sk not in mapped_keys:
            issues.append({
                "severity": "P0",
                "where": f"agent_mappings.{sk}",
                "message": f"service '{sk}' has no agent_mapping -- call ws_propose_agent_mapping or ws_request_custom_agent.",
            })
        for f in ("name", "description", "autonomy_level", "owner_role"):
            if not (svc or {}).get(f):
                issues.append({"severity": "P1", "where": f"services.{sk}.{f}", "message": f"service field '{f}' is empty."})

    # Each mapping must point to a real service
    for m in mappings:
        sk = (m or {}).get("service_key")
        if sk and sk not in service_keys:
            issues.append({
                "severity": "P1",
                "where": f"agent_mappings.{sk}",
                "message": f"mapping references unknown service '{sk}'.",
            })

    # Goals: required fields
    for g in fields.get("goals") or []:
        gk = (g or {}).get("goal_key", "<unknown>")
        if not (g or {}).get("target"):
            issues.append({"severity": "P0", "where": f"goals.{gk}", "message": "goal missing target."})
        if not (g or {}).get("cadence"):
            issues.append({"severity": "P0", "where": f"goals.{gk}", "message": "goal missing cadence."})

    # Channels
    cc = fields.get("channel_config") or {}
    pec = cc.get("primary_external_channel") or {}
    if not pec.get("channel_type") or not pec.get("purpose"):
        issues.append({"severity": "P1", "where": "channel_config.primary_external_channel", "message": "primary_external_channel needs channel_type and purpose -- call ws_propose_channel role=primary_external."})

    # Staff (informational P1) -- if no human is involved that's fine,
    # but for typical operator workspaces an empty staff roster is
    # usually a sign the architect skipped step G.
    staff_assignments = fields.get("staff_assignments") or []
    if not staff_assignments:
        issues.append({
            "severity": "P1",
            "where": "staff_assignments",
            "message": "No staff assigned. If humans review/own anything in this workspace, call ws_assign_staff (one per owner). Skip only for fully autonomous workspaces.",
        })

    # Knowledge attachments (informational P1) -- agents almost always
    # benefit from at least one knowledge group for retrieval; warn so
    # the architect doesn't ship a workspace with empty RAG context.
    knowledge_attachments = fields.get("knowledge_attachments") or []
    if not knowledge_attachments and services:
        issues.append({
            "severity": "P1",
            "where": "knowledge_attachments",
            "message": "No knowledge groups attached. Agents will start with zero RAG context. Call ws_attach_knowledge for each logical bucket (brand voice, playbooks, FAQ).",
        })

    # Automations referencing nonexistent services
    for a in fields.get("automations") or []:
        sk = (a or {}).get("service_key")
        if sk and sk not in service_keys:
            issues.append({
                "severity": "P1",
                "where": f"automations.{(a or {}).get('automation_key','?')}",
                "message": f"automation references unknown service '{sk}'.",
            })

    p0 = sum(1 for i in issues if i["severity"] == "P0")
    return _ok({
        "ok_to_finalize": p0 == 0,
        "p0": p0,
        "p1": sum(1 for i in issues if i["severity"] == "P1"),
        "issues": issues,
    })


# ── ws_mark_ready ───────────────────────────────────────────────────────────

async def _mark_ready(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id)
    if draft is None:
        return _err("draft not found")
    # Run lint first as a safety net
    lint = await _lint_draft(db, entity_id=entity_id, draft_id=draft_id)
    lint_data = json.loads(lint)
    if not lint_data.get("ok") or not lint_data.get("ok_to_finalize"):
        return _err(
            "draft has P0 issues -- fix them before marking ready",
            issues=lint_data.get("issues"),
        )
    draft.ready = True
    if draft.status == "active":
        draft.status = "ready"
    draft.missing = []
    await db.flush()
    return _ok({"ready": True, "status": draft.status})


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS = {
    "ws_commit_basics": _commit_basics,
    "ws_propose_service": _propose_service,
    "ws_propose_goal": _propose_goal,
    "ws_propose_agent_mapping": _propose_agent_mapping,
    "ws_request_custom_agent": _request_custom_agent,
    "ws_assign_staff": _assign_staff,
    "ws_attach_knowledge": _attach_knowledge,
    "ws_flag_missing_integration": _flag_missing_integration,
    "ws_search_capabilities": _search_capabilities,
    "ws_propose_channel": _propose_channel,
    "ws_propose_rule": _propose_rule,
    "ws_propose_automation": _propose_automation,
    "ws_set_evaluation": _set_evaluation,
    "ws_set_budget": _set_budget,
    "ws_remove": _remove,
    "ws_search_entity_agents": _search_entity_agents,
    "ws_search_blueprints": _search_blueprints,
    "ws_get_draft": _get_draft,
    "ws_lint_draft": _lint_draft,
    "ws_mark_ready": _mark_ready,
}


def get_tools() -> list:
    """Return tools in the (schema, handler) tuple format the tool_pool expects.

    The handlers all need a live DB session, which the tool_pool's stateless
    executor doesn't provide -- so when registered globally these handlers
    open their own short-lived session per call.
    """
    return [(schema, _make_session_wrapped_handler(name)) for schema in ALL_TOOL_SCHEMAS for name in [schema["function"]["name"]]]


def _make_session_wrapped_handler(name: str):
    async def wrapped(entity_id: str = "", user_id: str = "", **kwargs):
        try:
            from packages.core.database import async_session
            from sqlalchemy.exc import SQLAlchemyError
            handler = HANDLERS.get(name)
            if handler is None:
                return _err(f"unknown tool: {name}")
            async with async_session() as db:
                try:
                    result = await handler(db, entity_id=entity_id, user_id=user_id, **kwargs)
                    await db.commit()
                    return result
                except SQLAlchemyError as exc:
                    await db.rollback()
                    return _err(f"db error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s crashed", name)
            return _err(f"tool crashed: {exc}")
    wrapped.__name__ = f"_ws_arch_{name}"
    return wrapped


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Nango aggregator listing — used by ws_search_capabilities so the
# architect knows about the long-tail platforms (200+) that Nango
# unlocks without dedicated MCP servers.
# ---------------------------------------------------------------------------

async def _list_nango_aggregator(db, entity_id: str, query: str) -> Optional[Dict[str, Any]]:
    """Return a single aggregator row describing the Nango platform +
    its providers/connections, or None if Nango is not configured.
    Failure to reach Nango is non-fatal — we just omit the block."""
    from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret

    secret = await get_nango_secret(db, entity_id)
    if not secret:
        return None

    providers: List[Dict[str, Any]] = []
    connections: List[Dict[str, Any]] = []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/config",
                headers={"Authorization": f"Bearer {secret}"},
            )
            r.raise_for_status()
            cfg_body = r.json()
            for cfg in (cfg_body.get("configs") or cfg_body if isinstance(cfg_body, list) else []):
                providers.append({
                    "provider_config_key": cfg.get("unique_key") or cfg.get("provider_config_key"),
                    "provider": cfg.get("provider"),
                })

            r2 = await cx.get(
                f"{_NANGO_BASE}/connection",
                params={"end_user_id": entity_id},
                headers={"Authorization": f"Bearer {secret}"},
            )
            r2.raise_for_status()
            conn_body = r2.json()
            for c in (conn_body.get("connections") or conn_body if isinstance(conn_body, list) else []):
                connections.append({
                    "provider_config_key": c.get("provider_config_key") or c.get("provider"),
                    "connection_id": c.get("connection_id"),
                })
    except Exception as exc:  # noqa: BLE001
        return {
            "mcp_server_key": "nango",
            "name": "Nango (aggregator)",
            "active_integration": True,
            "error": f"Nango listing failed: {exc}",
        }

    if query:
        q = query.lower()
        providers = [p for p in providers if q in (p.get("provider") or "").lower()]
        connections = [c for c in connections if q in (c.get("provider_config_key") or "").lower()]

    return {
        "mcp_server_key": "nango",
        "name": "Nango (aggregator, 200+ apps — fallback only)",
        "description": (
            "FALLBACK aggregator. Prefer per-platform MCP servers "
            "(twitter_x, slack, linear, notion, github, etc.) over this "
            "one whenever they exist in the integrations list — those "
            "expose typed tools, this only exposes a generic HTTP "
            "proxy. Bind nango via mcp__nango__nango_proxy ONLY for "
            "providers that have no dedicated server. "
            "providers_connected lists what the entity has authorized."
        ),
        "auth_type": "api_key",
        "active_integration": True,
        "providers_available": providers,
        "providers_connected": connections,
    }
