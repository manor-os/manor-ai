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

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from packages.core.ai.runtime.control import RuntimeTurnAborted
from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.plans import is_cloud
from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
    uses_ui_runtime_mode,
)
from packages.core.cron import validate_cron_expression
from packages.core.services.workspace_goal_measurements import (
    GOAL_MEASUREMENT_SCHEMA,
    measurement_stat_definition,
    resolve_draft_goal_measurements,
)

logger = logging.getLogger(__name__)

_INTERNAL_SKILL_MCP_PROVIDERS = {
    "chrome_knowledge_local",
    "knowledge_local",
}
_AGENT_CAPABILITY_PLANS_FIELD = "agent_capability_plans"
_AGENT_CAPABILITY_CONTEXT_FIELDS = frozenset({
    "kind",
    "operating_context",
    "primary_work",
    "category",
})


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

CADENCE_VALUES = ["daily", "weekly", "monthly", "quarterly", "yearly"]
AUTONOMY_CADENCE_VALUES = ["hourly", "daily", "weekly", "biweekly"]
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
            "this 1-5 times to cover the workspace's primary_work. Re-calling "
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
            "goal_key replaces the prior entry. goal_key, description, "
            "target, cadence, and measurement are required — never omit "
            "target or cadence. Call only after the user supplied or confirmed "
            "the measurable target and measurement definition; never infer or invent a KPI. "
            "Include its formula/rubric, evidence source and manual/automatic collection mode."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "goal_key", "title", "description", "target", "cadence", "measurement"],
            "properties": {
                "draft_id": {"type": "string"},
                "goal_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "title": {"type": "string", "minLength": 2, "description": "Short headline, e.g. 'Follower growth'"},
                "description": {"type": "string", "minLength": 10},
                "target": {"type": "string", "minLength": 1, "description": "Target value as string: '10000', '5%', '45%'"},
                "cadence": {"enum": CADENCE_VALUES},
                "measurement": GOAL_MEASUREMENT_SCHEMA,
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
            "entity-level or public Marketplace Agent. Use the agent_id returned by "
            "ws_search_entity_agents. If no good match exists, call "
            "ws_request_custom_agent instead."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key", "agent_id", "rationale"],
            "properties": {
                "draft_id": {"type": "string"},
                "service_key": {"type": "string"},
                "agent_id": {"type": "string", "minLength": 1, "description": "Agent ID returned by ws_search_entity_agents."},
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
            "bind real tools / skills / integrations the entity owns. The "
            "server reuses that service's stored exact Factory plan; do not "
            "copy capability ids or legacy binding fields into this call. "
            "On finalize, the platform creates the Agent, materializes the "
            "stored bindings, auto-creates any "
            "missing skills you specified, and surfaces any missing "
            "integrations on the workspace as a 'needs setup' warning."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key", "agent_name", "system_prompt"],
            "additionalProperties": False,
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
            "Semantically match one Workspace service against the complete "
            "actor-scoped Tool, MCP, Skill, and business-capability catalog. "
            "The shared Factory scans the catalog in bounded rounds and "
            "returns a compact least-privilege plan with exact ids, plus the "
            "entity's staff and knowledge resources. Call once for each custom "
            "service, then call ws_request_custom_agent with the same service_key. "
            "The server reuses the stored validated plan; do not transcribe ids."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "service_key"],
            "properties": {
                "draft_id": {"type": "string"},
                "service_key": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact service_key already stored by ws_propose_service.",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Optional proposed reusable Agent name used as semantic context.",
                },
                "intent": {
                    "type": "string",
                    "description": "Optional extra capability requirements or exclusions from the user.",
                },
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


SET_AUTONOMY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_set_autonomy",
        "description": (
            "Set the workspace-wide autonomous runtime choice. This controls "
            "automatic Strategist reviews and evolution schedules; it is "
            "independent from service autonomy levels and from whether Goals "
            "are configured. New drafts default to automatic. Preserve the "
            "current mode: the user switches Automatic/Manual in the creation "
            "panel. This tool may adjust cadence, not override that mode."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "enabled"],
            "properties": {
                "draft_id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "cadence": {
                    "enum": AUTONOMY_CADENCE_VALUES,
                    "description": "Strategist review cadence when enabled. Defaults to daily.",
                },
            },
        },
    },
}


CONFIRM_CREATION_PREFERENCES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_confirm_creation_preferences",
        "description": (
            "Record the user's Goal choice. New drafts default to automatic; "
            "omit autonomous_enabled to preserve the creation panel's mode. "
            "Never infer a manual-mode choice from a Goal answer. For a new "
            "Goal, call ws_propose_goal and wait for its result first."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "goal_choice"],
            "properties": {
                "draft_id": {"type": "string"},
                "goal_choice": {"enum": ["configured", "none"]},
                "autonomous_enabled": {"type": "boolean"},
                "autonomy_cadence": {
                    "enum": AUTONOMY_CADENCE_VALUES,
                    "description": "Strategist review cadence when autonomous mode is enabled. Defaults to daily.",
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
            "List every materializable entity or global platform agent, including "
            "each candidate's actual tool, skill, and MCP "
            "bindings, so you can pick a real agent_id for "
            "ws_propose_agent_mapping. Always call this BEFORE proposing "
            "any agent mapping. No keyword filtering is performed; the "
            "architect LLM compares every candidate semantically."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
            },
        },
    },
}


SEARCH_BLUEPRINTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_search_blueprints",
        "description": (
            "List the workspace blueprint marketplace for semantic selection. Use early in the "
            "conversation: if a published blueprint clearly matches the "
            "user's intent, suggest it instead of building from scratch. "
            "The architect LLM evaluates the returned candidates; this tool "
            "does not score keywords."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
            },
        },
    },
}


SUGGEST_BLUEPRINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_suggest_blueprint",
        "description": (
            "Record the single published blueprint you selected after semantic "
            "comparison of the complete ws_search_blueprints inventory. Use only "
            "for a strong capability/workflow fit; never choose by keyword overlap."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "blueprint_id", "rationale"],
            "properties": {
                "draft_id": {"type": "string"},
                "blueprint_id": {"type": "string"},
                "rationale": {"type": "string", "minLength": 10},
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
    SET_AUTONOMY_SCHEMA,
    CONFIRM_CREATION_PREFERENCES_SCHEMA,
    REMOVE_FIELD_SCHEMA,
    SEARCH_ENTITY_AGENTS_SCHEMA,
    SEARCH_BLUEPRINTS_SCHEMA,
    SUGGEST_BLUEPRINT_SCHEMA,
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


async def _load_draft(db, draft_id: str, entity_id: str, user_id: str):
    """Load a draft scoped to its entity and creator."""
    from packages.core.services.workspace_draft_service import get_draft
    if not draft_id:
        return None
    return await get_draft(db, draft_id, entity_id, user_id or None)


async def _readable_entity_resource_ids(
    db,
    *,
    rows: List[Any],
    resource_type: str,
    entity_id: str,
    user_id: str,
) -> set[str]:
    """Return same-entity rows visible to the Architect's caller."""
    from packages.core.services.resource_access import (
        ResourceDescriptor,
        readable_resource_ids,
    )

    entity_rows = [
        row for row in rows
        if str(getattr(row, "entity_id", "") or "") == entity_id
    ]
    return await readable_resource_ids(
        db,
        descriptors=[
            ResourceDescriptor.from_row(row, resource_type)
            for row in entity_rows
        ],
        entity_id=entity_id,
        user_id=user_id or None,
    )


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


def _stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _agent_capability_service(
    fields: Dict[str, Any],
    service_key: str,
) -> Optional[Dict[str, Any]]:
    return next(
        (
            item
            for item in (fields.get("services") or [])
            if isinstance(item, dict)
            and _as_nonempty_str(item.get("service_key")) == service_key
        ),
        None,
    )


def _agent_capability_context(
    fields: Dict[str, Any],
    *,
    service: Dict[str, Any],
    service_key: str,
    intent: str,
) -> Dict[str, Any]:
    rules = [
        item
        for item in (fields.get("rules") or [])
        if isinstance(item, dict)
        and _as_nonempty_str(item.get("scope", item.get("service_key")))
        in {"", "all", service_key}
    ]
    rules.sort(
        key=lambda item: (
            _as_nonempty_str(item.get("rule_key")),
            _stable_fingerprint(item),
        )
    )
    return {
        "primary_work": fields.get("primary_work") or "",
        "operating_context": fields.get("operating_context") or "",
        "category": fields.get("category") or "",
        "kind": fields.get("kind") or "",
        "service": dict(service),
        "rules": rules,
        "intent": intent,
    }


def _agent_capability_prompt(
    context: Dict[str, Any],
    *,
    service_key: str,
) -> str:
    service = context["service"]
    prompt_parts = [
        f"Workspace primary work: {context['primary_work']}",
        f"Operating context: {context['operating_context']}",
        f"Service: {service.get('name') or service_key}",
        f"Service description: {service.get('description') or ''}",
        f"Service inputs: {service.get('inputs') or []}",
        f"Service outputs: {service.get('outputs') or []}",
        f"Service autonomy: {service.get('autonomy_level') or ''}",
    ]
    if context["rules"]:
        prompt_parts.append(f"Applicable rules: {context['rules']}")
    if context["intent"]:
        prompt_parts.append(f"Additional user requirements: {context['intent']}")
    return "\n".join(prompt_parts)


def _agent_capability_catalog_fingerprint(agent_catalog: Any) -> str:
    """Hash semantic catalog shape without transient connection readiness."""
    semantic_candidates = []
    for raw_item in agent_catalog.prompt_payload():
        item = dict(raw_item)
        item.pop("readiness", None)
        item.pop("setup_required_reason", None)
        semantic_candidates.append(item)
    semantic_candidates.sort(key=lambda item: _as_nonempty_str(item.get("id")))
    return _stable_fingerprint(semantic_candidates)


def _clear_agent_capability_plans(
    fields: Dict[str, Any],
    *,
    service_key: str | None = None,
) -> None:
    if service_key is None:
        fields[_AGENT_CAPABILITY_PLANS_FIELD] = []
        return
    fields[_AGENT_CAPABILITY_PLANS_FIELD] = [
        item
        for item in (fields.get(_AGENT_CAPABILITY_PLANS_FIELD) or [])
        if not isinstance(item, dict)
        or _as_nonempty_str(item.get("service_key")) != service_key
    ]


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
            if existing.get("source") == "agent_design":
                existing["blocks_creation"] = False
            if resolved.covered_provider:
                existing.setdefault("covered_provider", resolved.covered_provider)
            for key in (
                "setup_kind",
                "setup_reason",
                "connection_scope",
                "binding_kind",
                "binding_ref",
            ):
                if mi.get(key):
                    existing.setdefault(key, mi[key])
            continue
        flag = {
            "provider": provider,
            "purpose": mi.get("purpose", ""),
            "required": bool(mi.get("required", True)),
            "linked_service_keys": [service_key],
            "source": "agent_design",
            "agent_name": create_draft.get("agent_name", ""),
            # Capability setup gates execution, not creation. Persist the
            # warning so the Workspace can guide setup after its Agent and
            # bindings have been materialized.
            "blocks_creation": False,
        }
        if resolved.covered_provider:
            flag["covered_provider"] = resolved.covered_provider
        for key in (
            "setup_kind",
            "setup_reason",
            "connection_scope",
            "binding_kind",
            "binding_ref",
        ):
            if mi.get(key):
                flag[key] = mi[key]
        flagged.append(flag)
    fields["flagged_integrations"] = flagged


def _mcp_provider_keys_from_tool_names(tool_names: List[Any]) -> set[str]:
    providers: set[str] = set()
    for raw_name in tool_names or []:
        parts = str(raw_name or "").split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp" and parts[1]:
            provider = parts[1]
            if provider in _INTERNAL_SKILL_MCP_PROVIDERS:
                continue
            providers.add(provider)
    return providers


async def _augment_missing_integrations_from_agent_bindings(
    db,
    *,
    entity_id: str,
    user_id: str,
    service_key: str,
    agent_name: str,
    skill_bindings: List[str],
    mcp_bindings: List[str],
    missing_integrations: List[Any],
) -> List[Dict[str, Any]]:
    """Derive setup blockers from direct MCPs and skill-declared MCP dependencies."""
    from sqlalchemy import and_, or_, select

    from packages.core.models.mcp import MCPServer
    from packages.core.models.skill import Skill
    from packages.core.services.integration_resolution import integration_provider_readiness
    from packages.core.services.provider_keys import canonical_provider_key

    refs = list(dict.fromkeys(
        str(ref).strip() for ref in (mcp_bindings or []) if str(ref or "").strip()
    ))
    out = [dict(item) for item in (missing_integrations or []) if isinstance(item, dict)]
    skill_refs = list(dict.fromkeys(
        str(ref).strip() for ref in (skill_bindings or []) if str(ref or "").strip()
    ))
    if not refs and not skill_refs:
        return out

    skill_rows = []
    if skill_refs:
        skill_rows = list((await db.execute(
            select(Skill).where(
                Skill.status == "active",
                or_(Skill.id.in_(skill_refs), Skill.slug.in_(skill_refs)),
                or_(
                    Skill.entity_id == entity_id,
                    and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
                ),
            )
        )).scalars().all())
    providers_from_skills: dict[str, str] = {}
    for skill in skill_rows:
        for provider in _mcp_provider_keys_from_tool_names(list(skill.tools or [])):
            providers_from_skills.setdefault(
                canonical_provider_key(provider),
                skill.slug or skill.id,
            )

    servers = list((await db.execute(
        select(MCPServer).where(
            MCPServer.status == "active",
            (MCPServer.id.in_(refs))
            | (MCPServer.server_key.in_(refs))
            | (MCPServer.server_key.in_(list(providers_from_skills))),
        )
    )).scalars().all())
    readiness = await integration_provider_readiness(
        db,
        entity_id=entity_id,
        user_id=user_id or None,
        provider_keys=[server.server_key for server in servers],
    )
    existing_by_provider = {
        canonical_provider_key(item.get("provider")): item
        for item in out
        if canonical_provider_key(item.get("provider"))
    }
    for server in servers:
        provider = canonical_provider_key(server.server_key)
        status = readiness.get(provider)
        if status is None or status.ready:
            continue
        purpose = status.reason or (
            f"Connect {server.name or server.server_key} before this agent can use it."
        )
        existing = existing_by_provider.get(provider)
        if existing is None:
            existing = {
                "provider": provider,
                "purpose": purpose,
                "required": True,
                "source": "mcp_binding_readiness",
            }
            out.append(existing)
            existing_by_provider[provider] = existing
        else:
            existing.setdefault("purpose", purpose)
            existing["required"] = True
        skill_ref = providers_from_skills.get(provider)
        if skill_ref:
            existing["source"] = "skill_binding_readiness"
            existing["binding_kind"] = "skill"
            existing["binding_ref"] = skill_ref
        else:
            existing.setdefault("binding_kind", "mcp")
            existing.setdefault("binding_ref", server.server_key)
        existing["setup_reason"] = status.reason
        existing["connection_scope"] = status.scope
        if status.setup_kind:
            existing["setup_kind"] = status.setup_kind
        if service_key:
            existing.setdefault("linked_service_keys", [service_key])
        if agent_name:
            existing.setdefault("agent_name", agent_name)
    return out


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


def _confirm_creation_preference(fields: Dict[str, Any], preference: str) -> None:
    """Record an explicit conversational choice on new-style drafts only."""
    current = fields.get(CREATION_PREFERENCES_FIELD)
    if not isinstance(current, dict):
        if (
            WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD not in fields
            and CREATION_PREFERENCES_FIELD not in fields
        ):
            return
        current = {}
    preferences = dict(current)
    preferences[f"{preference}_confirmed"] = True
    fields[CREATION_PREFERENCES_FIELD] = preferences


def _validated_autonomy_cadence(
    value: Any,
    *,
    allow_cron: bool = False,
) -> str:
    """Return a normalized cadence accepted by autonomous scheduling."""
    if not isinstance(value, str):
        raise ValueError("autonomy cadence must be a string")
    cadence = value.strip().lower()
    if cadence in AUTONOMY_CADENCE_VALUES:
        return cadence
    if allow_cron:
        try:
            return validate_cron_expression(cadence)
        except ValueError:
            pass
    raise ValueError(
        "autonomy cadence must be hourly, daily, weekly, or biweekly"
    )


# ── ws_commit_basics ────────────────────────────────────────────────────────

async def _commit_basics(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    capability_context_changed = False
    for k in ("name", "kind", "operating_context", "primary_work", "category", "description"):
        v = kwargs.get(k)
        if v is not None and v != "":
            if k in _AGENT_CAPABILITY_CONTEXT_FIELDS and fields.get(k) != v:
                capability_context_changed = True
            fields[k] = v
    if capability_context_changed:
        _clear_agent_capability_plans(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"committed": ["name", "kind", "operating_context", "primary_work"]})


# ── ws_propose_service ──────────────────────────────────────────────────────

async def _propose_service(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    _clear_agent_capability_plans(fields, service_key=service_key)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"service_key": service_key, "service_count": len(fields["services"])})


# ── ws_propose_goal ─────────────────────────────────────────────────────────

async def _propose_goal(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    goal_key = kwargs.get("goal_key", "")
    if not _SLUG_RE.match(goal_key):
        return _err("goal_key must be snake_case", got=goal_key)
    target = kwargs.get("target", "")
    cadence = kwargs.get("cadence", "")
    if target in (None, ""):
        return _err("target is required (e.g. '10000', '5%') -- ask the user instead of inferring a target")
    if cadence not in CADENCE_VALUES:
        return _err(f"cadence must be one of {CADENCE_VALUES}", got=cadence)
    try:
        measurement = kwargs.get("measurement")
        stat_definition = measurement_stat_definition(measurement, cadence=cadence)
    except ValueError as exc:
        return _err(str(exc))

    goal = {
        "goal_key": goal_key,
        "title": kwargs.get("title", ""),
        "description": kwargs.get("description", ""),
        "target": str(target),
        "cadence": cadence,
        "measurement": measurement,
        "stat_key": stat_definition["key"],
        "metric_key": stat_definition["key"],
    }
    if kwargs.get("rationale"):
        goal["rationale"] = kwargs["rationale"]

    fields = dict(draft.fields or {})
    goal = _scrub_new_item_for_removed_channels(goal, fields)
    fields["goals"] = _replace_in_list(
        fields.get("goals") or [], "goal_key", goal_key, goal,
    )
    # A Blueprint may already define this Stat separately. An explicitly
    # confirmed Goal edit must update that definition, not leave two
    # conflicting formulas for the same key.
    if any(
        (stat.get("key") or stat.get("library_key")) == stat_definition["key"]
        for stat in fields.get("stats") or [] if isinstance(stat, dict)
    ):
        fields["stats"] = [
            stat_definition if (stat.get("key") or stat.get("library_key")) == stat_definition["key"] else stat
            for stat in fields["stats"]
        ]
    _confirm_creation_preference(fields, "goal")
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"goal_key": goal_key, "goal_count": len(fields["goals"])})


# ── ws_propose_agent_mapping ────────────────────────────────────────────────

async def _propose_agent_mapping(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.workspace import Agent

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    agent_id = str(kwargs.get("agent_id") or "").strip()
    if not agent_id:
        return _err("agent_id is required", hint="call ws_search_entity_agents to get valid ids")

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
    if agent.entity_id == entity_id:
        readable_ids = await _readable_entity_resource_ids(
            db,
            rows=[agent],
            resource_type="agent",
            entity_id=entity_id,
            user_id=user_id,
        )
        if agent.id not in readable_ids:
            return _err("agent is not accessible", got=agent_id)
    if agent.entity_id is None and not (agent.is_template and agent.is_public):
        return _err("agent is not available from the Marketplace", got=agent_id)

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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    service_key = kwargs.get("service_key", "")
    # Accept both new richer fields and the old "system_prompt_seed" name
    # so older calls keep working while the architect upgrades.
    system_prompt = kwargs.get("system_prompt") or kwargs.get("system_prompt_seed", "")

    fields = dict(draft.fields or {})
    stored_plan = next(
        (
            item for item in (fields.get(_AGENT_CAPABILITY_PLANS_FIELD) or [])
            if isinstance(item, dict) and item.get("service_key") == service_key
        ),
        None,
    )
    requested_capability_ids = list(kwargs.get("capability_ids") or [])
    stored_capability_ids = (
        list(stored_plan.get("capability_ids") or [])
        if stored_plan is not None else []
    )
    capability_ids = (
        stored_capability_ids
        if stored_plan is not None
        else requested_capability_ids
    )
    if (
        stored_plan is not None
        and requested_capability_ids
        and requested_capability_ids != stored_capability_ids
    ):
        logger.warning(
            "Workspace Agent capability ids for service %s differed from the "
            "stored Factory plan; using the validated plan",
            service_key,
        )
    legacy_bindings_requested = any(
        kwargs.get(key)
        for key in (
            "tool_bindings",
            "business_capabilities",
            "skill_bindings",
            "mcp_bindings",
        )
    )
    if (
        stored_plan is None
        and not requested_capability_ids
        and not legacy_bindings_requested
    ):
        return _err(
            "agent capability plan is required",
            hint=(
                "call ws_search_capabilities for this service_key before "
                "ws_request_custom_agent"
            ),
            service_key=service_key,
        )

    capability_plan = None
    if stored_plan is not None or capability_ids:
        from packages.core.services.agent_capability_catalog import (
            AgentCapabilityCatalogFactory,
            AgentCapabilitySelectionError,
        )

        try:
            capability_catalog = await AgentCapabilityCatalogFactory.create(
                db,
                entity_id=entity_id,
                user_id=user_id,
            )
            if stored_plan is not None:
                service = _agent_capability_service(fields, service_key)
                if service is None:
                    return _err(
                        "stored agent capability plan is stale",
                        detail="the matched service no longer exists",
                        hint=(
                            "restore the service, then call "
                            "ws_search_capabilities again"
                        ),
                        service_key=service_key,
                    )
                context = _agent_capability_context(
                    fields,
                    service=service,
                    service_key=service_key,
                    intent=_as_nonempty_str(stored_plan.get("intent")),
                )
                context_fingerprint = _stable_fingerprint(context)
                catalog_fingerprint = _agent_capability_catalog_fingerprint(
                    capability_catalog
                )
                if (
                    stored_plan.get("context_fingerprint") != context_fingerprint
                    or stored_plan.get("catalog_fingerprint") != catalog_fingerprint
                ):
                    return _err(
                        "stored agent capability plan is stale",
                        hint=(
                            "call ws_search_capabilities again for this "
                            "service_key before requesting the Agent"
                        ),
                        service_key=service_key,
                    )
            capability_plan = capability_catalog.resolve(capability_ids)
        except AgentCapabilitySelectionError as exc:
            return _err(
                "invalid agent capability selection",
                detail=str(exc),
                hint=(
                    "call ws_search_capabilities again; the server will reuse "
                    "its exact stored ids"
                ),
            )

    create_draft = {
        "agent_name": kwargs.get("agent_name", ""),
        "agent_description": kwargs.get("agent_description", ""),
        "system_prompt": system_prompt,
        "tool_bindings": (
            list(capability_plan.tool_names)
            if capability_plan is not None else list(kwargs.get("tool_bindings") or [])
        ),
        "business_capabilities": (
            list(capability_plan.business_capability_ids)
            if capability_plan is not None else list(kwargs.get("business_capabilities") or [])
        ),
        "skill_bindings": (
            list(capability_plan.skill_ids)
            if capability_plan is not None else list(kwargs.get("skill_bindings") or [])
        ),
        "mcp_bindings": (
            list(capability_plan.mcp_server_keys)
            if capability_plan is not None else list(kwargs.get("mcp_bindings") or [])
        ),
        "mcp_allowed_tools": (
            {
                key: (list(value) if value is not None else None)
                for key, value in capability_plan.mcp_allowed_tools.items()
            }
            if capability_plan is not None else {}
        ),
        "missing_skill_specs": list(kwargs.get("missing_skill_specs") or []),
        "missing_integrations": list(kwargs.get("missing_integrations") or []),
    }
    create_draft["missing_integrations"] = await _augment_missing_integrations_from_agent_bindings(
        db,
        entity_id=entity_id,
        user_id=user_id,
        service_key=service_key,
        agent_name=create_draft["agent_name"],
        skill_bindings=create_draft["skill_bindings"],
        mcp_bindings=create_draft["mcp_bindings"],
        missing_integrations=create_draft["missing_integrations"],
    )
    mapping = {
        "service_key": service_key,
        "strategy": "create_custom",
        "create_agent_draft": create_draft,
        "rationale": kwargs.get("rationale", ""),
    }
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
        "capability_selection_source": (
            "stored_factory_plan" if stored_plan is not None else "request"
        ),
    })


async def _assign_staff(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.staff import Staff

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    """Match one service semantically, plus return Workspace-only resources.

    Direct callers that omit ``service_key`` retain the full legacy inventory.
    The Architect schema requires it, keeping the large actor catalog inside
    the bounded matcher instead of truncating it in the outer chat context.
    """
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup
    from packages.core.models.staff import Staff
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )

    draft_id = kwargs.get("draft_id", "")
    draft = None
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")

    agent_catalog = await AgentCapabilityCatalogFactory.create(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    shared_payload = agent_catalog.workspace_payload()
    integrations_out = shared_payload["integrations"]

    # Nango is an aggregator rather than a bindable MCP server, so it stays a
    # Workspace-only companion block outside the exact Agent catalog.
    nango_block = await _list_nango_aggregator(db, entity_id)
    if nango_block:
        integrations_out.append(nango_block)

    staff_rows = (await db.execute(
        select(Staff).where(
            Staff.entity_id == entity_id,
            Staff.deleted_at.is_(None),
        )
    )).scalars().all()
    staff_out = [
        {
            "id": staff.id,
            "name": getattr(staff, "display_name", None) or staff.name,
            "email": staff.email,
            "role": getattr(staff, "role", None) or getattr(staff, "title", None),
        }
        for staff in staff_rows
    ]

    knowledge_rows = (await db.execute(
        select(DocumentGroup).where(DocumentGroup.entity_id == entity_id)
    )).scalars().all()
    knowledge_out = [
        {
            "id": group.id,
            "name": group.name,
            "workspace_id": group.workspace_id,
            "indexed": bool(group.vector_store_id),
        }
        for group in knowledge_rows
    ]

    payload = {
        "business_capabilities": shared_payload["business_capabilities"],
        "tools": shared_payload["tools"],
        "skills": shared_payload["skills"],
        "integrations": integrations_out,
        "agent_capability_catalog": agent_catalog.prompt_payload(),
        "staff": staff_out,
        "knowledge": knowledge_out,
    }

    service_key = str(kwargs.get("service_key") or "").strip()
    if not service_key:
        return _ok(payload)
    if draft is None:
        return _err("draft_id is required for service capability matching")

    fields = dict(draft.fields or {})
    service = _agent_capability_service(fields, service_key)
    if service is None:
        return _err(
            "service_key does not exist in this draft",
            got=service_key,
            available_service_keys=[
                str(item.get("service_key") or "")
                for item in (fields.get("services") or [])
                if isinstance(item, dict)
            ],
        )

    from packages.core.services.agent_generator import match_agent_capabilities

    extra_intent = str(kwargs.get("intent") or "").strip()
    context = _agent_capability_context(
        fields,
        service=service,
        service_key=service_key,
        intent=extra_intent,
    )

    agent_name = str(kwargs.get("agent_name") or "").strip()
    if not agent_name:
        agent_name = f"{service.get('name') or service_key} Agent"

    # The semantic matcher can make several provider calls.  Do not keep the
    # Architect's draft lock (or any pooled connection) checked out while that
    # external work runs.  Re-lock and compare the complete editable state
    # afterwards so a concurrent turn cannot be overwritten by a stale plan.
    baseline = _stable_fingerprint({
        "status": draft.status, "fields": fields, "messages": draft.messages,
    })
    await db.commit()
    match_error = None
    try:
        plan = await match_agent_capabilities(
            prompt=_agent_capability_prompt(context, service_key=service_key),
            spec={
                "name": agent_name,
                "description": str(service.get("description") or "").strip(),
                "category": str(fields.get("category") or fields.get("kind") or "").strip(),
            },
            entity_id=entity_id,
            capability_catalog=agent_catalog,
        )
    except Exception as exc:
        # Even a recoverable provider failure must re-establish the turn lock
        # before the model can retry or execute another Draft mutation.
        match_error = exc

    try:
        draft = (await db.execute(
            select(WorkspaceDraft)
            .where(
                WorkspaceDraft.id == draft_id,
                WorkspaceDraft.entity_id == entity_id,
                WorkspaceDraft.user_id == (user_id or None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    except Exception as exc:
        raise RuntimeTurnAborted(
            "draft state could not be revalidated after capability matching; retry this turn"
        ) from exc
    if draft is None:
        await db.rollback()
        raise RuntimeTurnAborted("draft not found after capability matching")
    current_fields = dict(draft.fields or {})
    current = _stable_fingerprint({
        "status": draft.status,
        "fields": current_fields,
        "messages": draft.messages,
    })
    if current != baseline or draft.status not in {"active", "ready"}:
        await db.rollback()
        raise RuntimeTurnAborted(
            "draft changed while capabilities were being matched; retry this turn",
        )
    if match_error is not None:
        raise match_error
    fields = current_fields
    fields[_AGENT_CAPABILITY_PLANS_FIELD] = _replace_in_list(
        fields.get(_AGENT_CAPABILITY_PLANS_FIELD) or [],
        "service_key",
        service_key,
        {
            "service_key": service_key,
            "agent_name": agent_name,
            "intent": extra_intent,
            "capability_ids": list(plan.selected_catalog_ids),
            "status": plan.status.value,
            "setup_required": [dict(item) for item in plan.setup_required],
            "context_fingerprint": _stable_fingerprint(context),
            "catalog_fingerprint": _agent_capability_catalog_fingerprint(
                agent_catalog
            ),
        },
    )
    draft.fields = fields
    await _persist(db, draft)
    # Keep the re-acquired row lock through the rest of the Architect turn.
    # The caller owns the final commit, including the visible conversation.
    selected_ids = set(plan.selected_catalog_ids)
    payload.update({
        "business_capabilities": [
            item for item in shared_payload["business_capabilities"]
            if str(item.get("id") or "") in set(plan.business_capability_ids)
        ],
        "tools": [
            {key: value for key, value in item.items() if key != "parameters"}
            for item in shared_payload["tools"]
            if str(item.get("name") or "") in set(plan.tool_names)
        ],
        "skills": [
            item for item in shared_payload["skills"]
            if str(item.get("id") or "") in set(plan.skill_ids)
        ],
        "integrations": [
            item for item in integrations_out
            if str(item.get("mcp_server_key") or item.get("provider") or "")
            in set(plan.mcp_server_keys)
        ],
        "agent_capability_catalog": [
            candidate.prompt_dict()
            for candidate in agent_catalog.candidates
            if candidate.catalog_id in selected_ids
        ],
        "agent_capability_plan": plan.public_dict(),
        "catalog_stats": {
            "total_candidates": len(agent_catalog.candidates),
            "selected_candidates": len(plan.selected_catalog_ids),
            "selection_limit": AGENT_CAPABILITY_SELECTION_LIMIT,
        },
        "selection_scope": {"service_key": service_key, "agent_name": agent_name},
    })
    return _ok(payload)


# ── ws_propose_channel ──────────────────────────────────────────────────────

async def _propose_channel(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    _clear_agent_capability_plans(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"rule_key": rule_key})


# ── ws_propose_automation ───────────────────────────────────────────────────

async def _propose_automation(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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


# ── ws_set_autonomy ─────────────────────────────────────────────────

async def _set_autonomy(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    enabled = kwargs.get("enabled")
    if not isinstance(enabled, bool):
        return _err("enabled must be a boolean")

    fields = dict(draft.fields or {})
    cadence: str | None = None
    if uses_ui_runtime_mode(fields) and enabled != fields.get("heartbeat_enabled"):
        return _err("Runtime mode is controlled by the creation panel. Ask the user to switch Automatic/Manual there.")
    if "cadence" in kwargs:
        try:
            cadence = _validated_autonomy_cadence(kwargs.get("cadence"))
        except ValueError as exc:
            return _err(str(exc))
    elif enabled:
        try:
            cadence = _validated_autonomy_cadence(
                fields.get("heartbeat_cadence") or "daily",
                allow_cron=True,
            )
        except ValueError as exc:
            return _err(str(exc))

    fields["heartbeat_enabled"] = enabled
    if enabled:
        fields["heartbeat_cadence"] = cadence
    if not uses_ui_runtime_mode(fields):
        _confirm_creation_preference(fields, "autonomy")
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "heartbeat_enabled": enabled,
        "heartbeat_cadence": fields.get("heartbeat_cadence"),
    })


# ── ws_confirm_creation_preferences ──────────────────────────────────

async def _confirm_creation_preferences(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    goal_choice = str(kwargs.get("goal_choice") or "")
    if goal_choice not in {"configured", "none"}:
        return _err("goal_choice must be 'configured' or 'none'", got=goal_choice)
    if goal_choice == "configured" and not (fields.get("goals") or []):
        return _err("no Goal is configured -- call ws_propose_goal with the user's confirmed details")
    if not uses_ui_runtime_mode(fields) and "autonomous_enabled" not in kwargs:
        return _err("autonomous_enabled must be a boolean")
    autonomous_enabled = kwargs.get("autonomous_enabled", fields.get("heartbeat_enabled"))
    if not isinstance(autonomous_enabled, bool):
        return _err("autonomous_enabled must be a boolean")
    if uses_ui_runtime_mode(fields) and autonomous_enabled != fields.get("heartbeat_enabled"):
        return _err("Runtime mode is controlled by the creation panel. Omit autonomous_enabled to preserve the user's mode.")
    autonomy_cadence: str | None = None
    if "autonomy_cadence" in kwargs:
        try:
            autonomy_cadence = _validated_autonomy_cadence(
                kwargs.get("autonomy_cadence")
            )
        except ValueError as exc:
            return _err(str(exc))
    elif autonomous_enabled:
        try:
            autonomy_cadence = _validated_autonomy_cadence(
                fields.get("heartbeat_cadence") or "daily",
                allow_cron=True,
            )
        except ValueError as exc:
            return _err(str(exc))

    if goal_choice == "none":
        fields["goals"] = []
    fields["heartbeat_enabled"] = autonomous_enabled
    if autonomous_enabled:
        fields["heartbeat_cadence"] = autonomy_cadence
    _confirm_creation_preference(fields, "goal")
    if not uses_ui_runtime_mode(fields):
        _confirm_creation_preference(fields, "autonomy")
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "goal_count": len(fields.get("goals") or []),
        "heartbeat_enabled": autonomous_enabled,
        "creation_preferences_confirmed": True,
    })


# ── ws_remove ───────────────────────────────────────────────────────────────

async def _remove(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
        _clear_agent_capability_plans(fields)
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
    if kind == "service":
        _clear_agent_capability_plans(fields, service_key=key)
    elif kind == "rule":
        _clear_agent_capability_plans(fields)
    if kind == "agent_mapping":
        _reconcile_agent_design_flags(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"kind": kind, "key": key, "remaining": len(fields[list_key])})


# ── ws_search_entity_agents ────────────────────────────────────────────────

async def _search_entity_agents(db, *, entity_id: str, user_id: str = "", **kwargs):
    from collections import defaultdict

    from sqlalchemy import and_, or_, select

    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import Agent, AgentToolBinding, ToolDefinition

    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")

    stmt = select(Agent).where(
        Agent.deleted_at.is_(None),
        Agent.status == "active",
        or_(
            Agent.entity_id == entity_id,
            and_(
                Agent.entity_id.is_(None),
                Agent.is_template.is_(True),
                Agent.is_public.is_(True),
            ),
        ),
    )
    rows = (await db.execute(stmt)).scalars().all()
    readable_agent_ids = await _readable_entity_resource_ids(
        db,
        rows=rows,
        resource_type="agent",
        entity_id=entity_id,
        user_id=user_id,
    )
    rows = [
        agent for agent in rows
        if agent.entity_id is None or agent.id in readable_agent_ids
    ]
    agent_ids = [agent.id for agent in rows]
    tools_by_agent: dict[str, list[str]] = defaultdict(list)
    skills_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mcp_by_agent: dict[str, list[str]] = defaultdict(list)
    if agent_ids:
        tool_rows = (await db.execute(
            select(AgentToolBinding.agent_id, ToolDefinition.name)
            .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
            .where(
                AgentToolBinding.agent_id.in_(agent_ids),
                ToolDefinition.status == "active",
            )
        )).all()
        for agent_id, tool_name in tool_rows:
            tools_by_agent[str(agent_id)].append(str(tool_name))

        skill_rows = (await db.execute(
            select(AgentSkillBinding.agent_id, Skill.id, Skill.slug, Skill.name)
            .join(Skill, Skill.id == AgentSkillBinding.skill_id)
            .where(
                AgentSkillBinding.agent_id.in_(agent_ids),
                AgentSkillBinding.status == "active",
                Skill.status == "active",
                or_(
                    Skill.entity_id == entity_id,
                    and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
                ),
            )
        )).all()
        bound_entity_skill_ids = {
            str(skill_id)
            for _agent_id, skill_id, _skill_slug, _skill_name in skill_rows
        }
        bound_entity_skills = list((await db.execute(
            select(Skill).where(
                Skill.id.in_(bound_entity_skill_ids),
                Skill.entity_id == entity_id,
            )
        )).scalars().all()) if bound_entity_skill_ids else []
        bound_entity_skill_by_id = {
            skill.id: skill for skill in bound_entity_skills
        }
        readable_bound_skill_ids = await _readable_entity_resource_ids(
            db,
            rows=bound_entity_skills,
            resource_type="skill",
            entity_id=entity_id,
            user_id=user_id,
        )
        for agent_id, skill_id, skill_slug, skill_name in skill_rows:
            skill_row = bound_entity_skill_by_id.get(skill_id)
            if skill_row is not None and skill_id not in readable_bound_skill_ids:
                continue
            skills_by_agent[str(agent_id)].append({
                "id": skill_id,
                "slug": skill_slug,
                "name": skill_name,
            })

        mcp_rows = (await db.execute(
            select(AgentMCPBinding.agent_id, MCPServer.server_key)
            .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
            .where(
                AgentMCPBinding.agent_id.in_(agent_ids),
                AgentMCPBinding.status == "active",
                MCPServer.status == "active",
            )
        )).all()
        for agent_id, server_key in mcp_rows:
            mcp_by_agent[str(agent_id)].append(str(server_key))

    installed_source_ids = {
        str((agent.config or {}).get("source_agent_id") or "")
        for agent in rows
        if agent.entity_id == entity_id
    }
    candidates = [
        agent
        for agent in rows
        if not (
            agent.entity_id is None
            and agent.id in installed_source_ids
        )
    ]
    candidates.sort(key=lambda agent: (agent.entity_id != entity_id, agent.name.lower(), agent.id))

    out = []
    for a in candidates:
        config = a.config or {}
        scope = (
            "entity"
            if a.entity_id == entity_id
            else "template"
            if a.is_template
            else "global"
        )
        out.append({
            "id": a.id,
            "name": a.name,
            "description": (a.description or "")[:200],
            "instructions_excerpt": (a.system_prompt or "")[:600],
            "category": a.category,
            "source": a.source or scope,
            "scope": scope,
            "source_agent_id": str(config.get("source_agent_id") or "") or None,
            "tool_bindings": sorted(tools_by_agent[a.id]),
            "skill_bindings": sorted(
                skills_by_agent[a.id],
                key=lambda skill: (str(skill.get("slug") or ""), str(skill.get("id") or "")),
            ),
            "mcp_bindings": sorted(mcp_by_agent[a.id]),
        })
    return _ok({"agents": out, "total": len(out)})


# ── ws_search_blueprints ────────────────────────────────────────────────────

async def _search_blueprints(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.blueprint import WorkspaceBlueprint

    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")

    stmt = (
        select(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED)
        .order_by(WorkspaceBlueprint.install_count.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    out = [
        {
            "id": bp.id,
            "title": bp.title,
            "summary": bp.summary,
            "tags": list(bp.tags or []),
            "install_count": int(bp.install_count or 0),
        }
        for bp in rows
    ]
    return _ok({"blueprints": out, "total": len(out)})


async def _suggest_blueprint(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select

    from packages.core.models.blueprint import WorkspaceBlueprint

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    blueprint_id = str(kwargs.get("blueprint_id") or "").strip()
    blueprint = (await db.execute(
        select(WorkspaceBlueprint).where(
            WorkspaceBlueprint.id == blueprint_id,
            WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED,
        )
    )).scalar_one_or_none()
    if blueprint is None:
        return _err("blueprint is not published or does not exist", blueprint_id=blueprint_id)

    draft.suggested_blueprint_id = blueprint.id
    await db.flush()
    return _ok({
        "blueprint_id": blueprint.id,
        "title": blueprint.title,
        "rationale": str(kwargs.get("rationale") or "").strip(),
    })


# ── ws_get_draft ────────────────────────────────────────────────────────────

async def _get_draft(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    from sqlalchemy import and_, or_, select

    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
    from packages.core.models.mcp import MCPServer
    from packages.core.models.skill import Skill
    from packages.core.models.workspace import Agent, ToolDefinition
    from packages.core.services.integration_resolution import supported_integration_provider_keys
    from packages.core.services.provider_keys import canonical_provider_key

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
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
    referenced_agent_ids = {
        str((mapping or {}).get("agent_id") or (mapping or {}).get("recommended_agent_id") or "").strip()
        for mapping in mappings
        if not str((mapping or {}).get("marketplace_agent_id") or "").strip()
        if str((mapping or {}).get("agent_id") or (mapping or {}).get("recommended_agent_id") or "").strip()
    }
    available_agent_rows = list((await db.execute(
        select(Agent).where(
            Agent.id.in_(referenced_agent_ids),
            Agent.deleted_at.is_(None),
            Agent.status == "active",
            or_(
                Agent.entity_id == entity_id,
                and_(
                    Agent.entity_id.is_(None),
                    Agent.is_template.is_(True),
                    Agent.is_public.is_(True),
                ),
            ),
        )
    )).scalars().all()) if referenced_agent_ids else []
    readable_agent_ids = await _readable_entity_resource_ids(
        db,
        rows=available_agent_rows,
        resource_type="agent",
        entity_id=entity_id,
        user_id=user_id,
    )
    available_agent_ids = {
        agent.id for agent in available_agent_rows
        if agent.entity_id is None or agent.id in readable_agent_ids
    }
    marketplace_agent_ids = {
        str((mapping or {}).get("marketplace_agent_id") or "").strip()
        for mapping in mappings
        if str((mapping or {}).get("marketplace_agent_id") or "").strip()
    }
    available_marketplace_agent_ids = set((await db.execute(
        select(Agent.id).where(
            Agent.id.in_(marketplace_agent_ids),
            Agent.deleted_at.is_(None),
            Agent.status == "active",
            Agent.entity_id.is_(None),
            Agent.is_template.is_(True),
            Agent.is_public.is_(True),
        )
    )).scalars().all()) if marketplace_agent_ids else set()

    custom_drafts = [
        dict((mapping or {}).get("create_agent_draft") or {})
        for mapping in mappings
        if (mapping or {}).get("strategy") == "create_custom"
    ]
    tool_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in (custom.get("tool_bindings") or [])
        if str(ref or "").strip()
    }
    tool_rows = set((await db.execute(
        select(ToolDefinition.name).where(
            ToolDefinition.name.in_(tool_refs),
            ToolDefinition.status == "active",
        )
    )).scalars().all()) if tool_refs else set()
    available_tool_refs = tool_rows | set(
        runtime_registered_tool_names(include_undiscoverable=True)
    )

    skill_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in (custom.get("skill_bindings") or [])
        if str(ref or "").strip()
    }
    skill_rows = list((await db.execute(
        select(Skill).where(
            Skill.status == "active",
            or_(
                Skill.entity_id == entity_id,
                and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
            ),
            (Skill.id.in_(skill_refs)) | (Skill.slug.in_(skill_refs)),
        )
    )).scalars().all()) if skill_refs else []
    readable_skill_ids = await _readable_entity_resource_ids(
        db,
        rows=skill_rows,
        resource_type="skill",
        entity_id=entity_id,
        user_id=user_id,
    )
    skill_rows = [
        skill for skill in skill_rows
        if skill.entity_id is None or skill.id in readable_skill_ids
    ]
    available_skill_refs = {
        ref
        for skill in skill_rows
        for ref in (skill.id, skill.slug)
        if ref
    }
    exact_skill_binding_refs = [
        ref
        for custom in custom_drafts
        for ref in (custom.get("skill_binding_refs") or [])
        if isinstance(ref, dict)
    ]
    platform_skill_ids = {
        str(ref.get("marketplace_id") or "").strip()
        for ref in exact_skill_binding_refs
        if str(ref.get("marketplace_source") or "platform").strip() == "platform"
        and str(ref.get("marketplace_id") or "").strip()
    }
    available_platform_skill_ids = set((await db.execute(
        select(Skill.id).where(
            Skill.id.in_(platform_skill_ids),
            Skill.status == "active",
            Skill.entity_id.is_(None),
            Skill.is_public.is_(True),
        )
    )).scalars().all()) if platform_skill_ids else set()
    manor_skill_ids = {
        str(ref.get("marketplace_id") or "").strip()
        for ref in exact_skill_binding_refs
        if str(ref.get("marketplace_source") or "platform").strip() == "manor"
        and str(ref.get("marketplace_id") or "").strip()
    }
    available_manor_skill_ids: set[str] = set()
    if manor_skill_ids and is_cloud():
        pass
    mcp_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in (custom.get("mcp_bindings") or [])
        if str(ref or "").strip()
    }
    mcp_rows = list((await db.execute(
        select(MCPServer).where(
            MCPServer.status == "active",
            (MCPServer.id.in_(mcp_refs)) | (MCPServer.server_key.in_(mcp_refs)),
        )
    )).scalars().all()) if mcp_refs else []
    supported_provider_keys = await supported_integration_provider_keys(db)
    available_mcp_refs = {
        ref
        for server in mcp_rows
        if canonical_provider_key(server.server_key) in supported_provider_keys
        for ref in (server.id, server.server_key)
        if ref
    }

    for m in mappings:
        sk = (m or {}).get("service_key")
        if sk and sk not in service_keys:
            issues.append({
                "severity": "P1",
                "where": f"agent_mappings.{sk}",
                "message": f"mapping references unknown service '{sk}'.",
            })
        strategy = str((m or {}).get("strategy") or "match")
        if strategy == "create_custom":
            custom = dict((m or {}).get("create_agent_draft") or {})
            generated_skill_refs = {
                str(missing_skill.get("slug") or "").strip()
                for missing_skill in (custom.get("missing_skill_specs") or [])
                if isinstance(missing_skill, dict)
                and str(missing_skill.get("slug") or "").strip()
            }
            if not str(custom.get("agent_name") or "").strip():
                issues.append({
                    "severity": "P0",
                    "where": f"agent_mappings.{sk}.agent_name",
                    "message": "custom agent is missing agent_name.",
                })
            if not str(custom.get("system_prompt") or custom.get("system_prompt_seed") or "").strip():
                issues.append({
                    "severity": "P0",
                    "where": f"agent_mappings.{sk}.system_prompt",
                    "message": "custom agent is missing a reusable system_prompt.",
                })
            binding_count = sum(len(custom.get(key) or []) for key in (
                "tool_bindings",
                "business_capabilities",
                "skill_bindings",
                "skill_binding_refs",
                "mcp_bindings",
                "missing_skill_specs",
            ))
            if binding_count == 0:
                issues.append({
                    "severity": "P0",
                    "where": f"agent_mappings.{sk}.bindings",
                    "message": "custom agent has no tool, capability, skill, or MCP binding.",
                })
            for ref in custom.get("tool_bindings") or []:
                if ref not in available_tool_refs:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.tool_bindings",
                        "message": f"tool binding {ref!r} is not available.",
                    })
            for ref in custom.get("business_capabilities") or []:
                if ref not in CORE_CAPABILITIES:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.business_capabilities",
                        "message": f"business capability {ref!r} is not available.",
                    })
            for ref in custom.get("skill_bindings") or []:
                if ref not in available_skill_refs and ref not in generated_skill_refs:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.skill_bindings",
                        "message": f"skill binding {ref!r} is not available to this entity.",
                    })
            for index, ref in enumerate(custom.get("skill_binding_refs") or []):
                source = (
                    str(ref.get("marketplace_source") or "platform").strip()
                    if isinstance(ref, dict) else ""
                )
                marketplace_id = (
                    str(ref.get("marketplace_id") or "").strip()
                    if isinstance(ref, dict) else ""
                )
                if source not in {"manor", "platform"} or not marketplace_id:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.skill_binding_refs.{index}",
                        "message": (
                            "Marketplace skill bindings require a supported "
                            "marketplace_source and exact marketplace_id."
                        ),
                    })
                    continue
                available_ids = (
                    available_platform_skill_ids
                    if source == "platform"
                    else available_manor_skill_ids
                )
                if marketplace_id not in available_ids:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.skill_binding_refs.{index}",
                        "message": (
                            f"Marketplace skill {source}:{marketplace_id} "
                            "is not available to this entity."
                        ),
                    })
            for ref in custom.get("mcp_bindings") or []:
                if ref not in available_mcp_refs:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.mcp_bindings",
                        "message": f"MCP binding {ref!r} is not in the supported integration inventory.",
                    })
            for index, missing_skill in enumerate(custom.get("missing_skill_specs") or []):
                if not isinstance(missing_skill, dict) or not str(missing_skill.get("name") or "").strip() or not str(missing_skill.get("system_prompt") or "").strip():
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.missing_skill_specs.{index}",
                        "message": "missing skill specs require name and system_prompt.",
                    })
                    continue
                for ref in missing_skill.get("tools") or []:
                    if ref not in available_tool_refs:
                        issues.append({
                            "severity": "P0",
                            "where": f"agent_mappings.{sk}.missing_skill_specs.{index}.tools",
                            "message": f"generated skill tool {ref!r} is not available.",
                        })
        else:
            marketplace_agent_id = str(
                (m or {}).get("marketplace_agent_id") or ""
            ).strip()
            agent_id = str(
                (m or {}).get("agent_id")
                or (m or {}).get("recommended_agent_id")
                or ""
            ).strip()
            available_ids = (
                available_marketplace_agent_ids
                if marketplace_agent_id
                else available_agent_ids
            )
            expected_agent_id = marketplace_agent_id or agent_id
            if not expected_agent_id or expected_agent_id not in available_ids:
                issues.append({
                    "severity": "P0",
                    "where": (
                        f"agent_mappings.{sk}.marketplace_agent_id"
                        if marketplace_agent_id
                        else f"agent_mappings.{sk}.agent_id"
                    ),
                    "message": (
                        "mapped Marketplace Agent is missing, private, inactive, "
                        "deleted, or is a local Agent ID."
                        if marketplace_agent_id
                        else "mapped agent is missing, inactive, deleted, or outside this entity."
                    ),
                })

    # Goals are optional. When the user does configure one, keep its
    # measurement contract strict instead of inventing a target or cadence.
    try:
        goals, _ = resolve_draft_goal_measurements(fields)
    except ValueError as exc:
        issues.append({"severity": "P0", "where": "goals.measurement", "message": str(exc)})
        goals = []
    for g in goals:
        gk = (g or {}).get("goal_key", "<unknown>")
        if g.get("target", g.get("target_value")) in (None, ""):
            issues.append({"severity": "P0", "where": f"goals.{gk}", "message": "goal missing target."})
        if not (g.get("cadence") or g.get("measurement_cadence")):
            issues.append({"severity": "P0", "where": f"goals.{gk}", "message": "goal missing cadence."})

    # New Workspace drafts require explicit conversational decisions. Both
    # internal markers are absent on legacy drafts, which remain compatible.
    # Once either marker exists, malformed or missing confirmation data must
    # fail closed instead of making a new draft ready accidentally.
    requires_creation_preferences = (
        WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD in fields
        or CREATION_PREFERENCES_FIELD in fields
    )
    if requires_creation_preferences:
        creation_preferences = fields.get(CREATION_PREFERENCES_FIELD)
        if (
            not isinstance(creation_preferences, dict)
            or creation_preferences.get("goal_confirmed") is not True
        ):
            issues.append({
                "severity": "P0",
                "where": "creation_preferences.goal",
                "message": "Confirm the user's Goal choice, then call ws_confirm_creation_preferences.",
            })
        if uses_ui_runtime_mode(fields):
            if not isinstance(fields.get("heartbeat_enabled"), bool):
                issues.append({
                    "severity": "P0",
                    "where": "creation_preferences.autonomy",
                    "message": "Choose Automatic or Manual mode in the creation panel.",
                })
        elif (
            not isinstance(creation_preferences, dict)
            or creation_preferences.get("autonomy_confirmed") is not True
        ):
            issues.append({
                "severity": "P0",
                "where": "creation_preferences.autonomy",
                "message": "Ask the combined Goal/autonomous creation question, then call ws_confirm_creation_preferences.",
            })

    # Channels
    cc = fields.get("channel_config") or {}
    pec = cc.get("primary_external_channel") or {}
    if pec and (not pec.get("channel_type") or not pec.get("purpose")):
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
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    # Run lint first as a safety net
    lint = await _lint_draft(
        db,
        entity_id=entity_id,
        user_id=user_id,
        draft_id=draft_id,
    )
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
    "ws_set_autonomy": _set_autonomy,
    "ws_confirm_creation_preferences": _confirm_creation_preferences,
    "ws_remove": _remove,
    "ws_search_entity_agents": _search_entity_agents,
    "ws_search_blueprints": _search_blueprints,
    "ws_suggest_blueprint": _suggest_blueprint,
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

async def _list_nango_aggregator(db, entity_id: str) -> Optional[Dict[str, Any]]:
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
