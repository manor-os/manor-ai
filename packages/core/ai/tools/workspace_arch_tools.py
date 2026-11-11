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

import contextlib
import copy
import dataclasses
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from packages.core.ai.runtime.control import RuntimeTurnAborted
from packages.core.ai.runtime.task_requirements import (
    STRATEGIST_TASK_CAPABILITY_IDS,
)
from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.plans import is_cloud
from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    STAFF_REQUEST_TEXT_FIELD,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
    uses_ui_runtime_mode,
)
from packages.core.blueprints.workflow_patterns import (
    RECORD_COLLECTION_PARAMETERS as _RECORD_COLLECTION_PARAMETERS,
    RECORD_COLLECTION_REQUIRED as _RECORD_COLLECTION_REQUIRED,
    SOURCE_WATCH_PARAMETERS as _SOURCE_WATCH_PARAMETERS,
    SOURCE_WATCH_REQUIRED as _SOURCE_WATCH_REQUIRED,
)
from packages.core.cron import validate_cron_expression
from packages.core.services.workflow_contracts import workflow_wait_is_confirmation, workflow_wait_options
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
# Every channel type Manor has an adapter for (see
# packages/core/services/channels), plus the built-in surfaces and two
# transport-level types. ``other`` is the escape hatch for a platform with no
# adapter: it records intent only — there is no "other" account to connect —
# and is optional by default. A test asserts this list covers the adapter
# registry so a new adapter cannot silently force the architect back onto
# ``other`` (that is how a workspace ended up with an unsatisfiable
# "Attach an active other channel account" requirement for X).
CHANNEL_TYPES = [
    "twilio_sms", "twilio_voice", "wechat", "wechat_personal",
    "telegram", "whatsapp", "email", "outlook", "slack", "discord",
    "ms_teams", "facebook", "twitter_x", "webchat", "internal_chat",
    "in_app", "inapp", "voice_stream", "generic_http", "other",
]
UNBACKED_CHANNEL_TYPES = frozenset({"other"})


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


def _required_capability_meanings() -> str:
    """What each requirable capability id grants, from the runtime catalog itself."""
    from packages.core.ai.runtime.capabilities import capability_for_id

    parts = []
    for capability_id in STRATEGIST_TASK_CAPABILITY_IDS:
        capability = capability_for_id(capability_id)
        if capability is not None:
            parts.append(f"{capability_id} = {capability.description.rstrip('.')}")
    return "; ".join(parts) + ". Reading Knowledge is workspace.search, not workspace.knowledge."


_REQUIRED_CAPABILITY_MEANINGS = _required_capability_meanings()


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
            "required": [
                "draft_id",
                "service_key",
                "name",
                "description",
                "autonomy_level",
                "owner_role",
                "required_capabilities",
            ],
            "properties": {
                "draft_id": {"type": "string"},
                "page_eyebrow": {"type": "string", "maxLength": 60},
                "service_key": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]*$",
                    "description": "snake_case identifier, e.g. 'content_creation'",
                },
                "name": {"type": "string", "minLength": 2, "description": "Human-readable service name, e.g. 'Content Creation'"},
                "description": {"type": "string", "minLength": 10},
                "autonomy_level": {"enum": AUTONOMY_VALUES},
                "owner_role": {"type": "string", "minLength": 2, "description": "e.g. 'content_strategist'"},
                "required_capabilities": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(STRATEGIST_TASK_CAPABILITY_IDS)},
                    "description": (
                        "Exact runtime capability ids Tasks owned by this service may require. "
                        "This declares needs only; it never grants tools to the Agent. Use [] "
                        "when the service has no capability requirement beyond its work contract. "
                        "Each id means: " + _REQUIRED_CAPABILITY_MEANINGS
                    ),
                },
                "handoff_to": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "description": (
                        "service_keys of the roles that take this service's result next, when the user "
                        "described a hand-off between roles (researcher → writer → editor). A hand-off is "
                        "run by a Workflow with one agent step per service; omit for independent services."
                    ),
                },
                "human_review": {
                    "type": "boolean",
                    "description": (
                        "True when the user wants a person to review or approve this service's result before "
                        "the work moves on. The Workflow that runs this service then needs an approval wait "
                        "after its step."
                    ),
                },
                "rationale": {"type": "string", "description": "Why this service is needed (links to primary_work)."},
            },
        },
    },
}


DESIGN_APP_VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_design_app_view",
        "description": (
            "Command the App designer to design this Draft's App page from the services, Workflows, "
            "tracked numbers and Automations already saved. It composes the page from the App module schematic and recipes "
            "(entries, card actions, data views, sections) and returns a checked candidate with its design notes; "
            "the candidate changes nothing until you accept it with ws_accept_app_view. To send it back, call "
            "this again with feedback."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "brief"],
            "properties": {
                "draft_id": {"type": "string"},
                "brief": {"type": "string", "minLength": 1, "maxLength": 4000, "description": (
                    "What the person wants to do from the App, in their words: the main job, the kinds of work "
                    "they start, what they want to see (progress, results, numbers).")},
                "feedback": {"type": "string", "maxLength": 4000, "description": (
                    "Why the last candidate was not accepted and what to change. The designer revises that "
                    "candidate and sees the brief it answered, so name every change it must make.")},
            },
        },
    },
}

ACCEPT_APP_VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_accept_app_view",
        "description": (
            "Accept the App designer's candidate as this Draft's App page. Accept only when it fits the "
            "brief: the main job is the hero, every visible entry is something the person starts, and the data "
            "views show what the Workflows keep."
        ),
        "parameters": {"type": "object", "required": ["draft_id"], "properties": {"draft_id": {"type": "string"}}},
    },
}

ACCEPT_PARTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_accept_parts",
        "description": (
            "Accept the waiting candidates of these parts (e.g. ['app']) in order. Each is checked again against "
            "the Draft as the parts before it left it, then written; the first that fails stops the rest and is "
            "returned with its errors, so command a new design for it. Accept only candidates that fit the brief."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "parts"],
            "properties": {
                "draft_id": {"type": "string"},
                "parts": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
        },
    },
}

DESIGN_WORKFLOWS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_design_workflows",
        "description": (
            "Command the Workflows designer to design this Draft's Workflows and tracked numbers (Stats) from the "
            "saved services. It composes them from checked patterns (a confirmed plan of dated content, scheduling "
            "each item, approved publishing with a verified receipt, a report that records Stats, a parent project "
            "and its parts) and returns a checked candidate with the Automations its Workflows need; the candidate "
            "changes nothing until you accept it with ws_accept_parts(parts=['workflows']). To send it back, call "
            "this again with feedback."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "brief"],
            "properties": {
                "draft_id": {"type": "string"},
                "brief": {"type": "string", "minLength": 1, "maxLength": 6000, "description": (
                    "The work in the person's words: what repeats, which service does each stage, what the person "
                    "approves before it moves on, what is planned for dates or published where, which numbers they "
                    "track and how often, and their time zone.")},
                "feedback": {"type": "string", "maxLength": 4000, "description": (
                    "Why the last candidate was not accepted and what to change.")},
            },
        },
    },
}

_SPEC_KEYS = {"type": "array", "items": {"type": "string"}}
COMMIT_BUILD_SPEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_commit_build_spec",
        "description": (
            "Commit the build spec after the services and rules are saved: the names every part shares (each "
            "service's Agent, Workflow slugs, Stat keys, Automation keys, Knowledge nets) and a brief per part. "
            "It is checked, without guessing: every reference resolves, a service with 3+ stages or a hand-off "
            "has a Workflow, and every routine the person named has an Automation. Returns mode: solo (a small "
            "Workspace you build yourself) or team (command ws_design_parts), and the parts to design. Commit it "
            "again after a change; only the parts it touches are designed again."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "services", "agents", "briefs"],
            "properties": {
                "draft_id": {"type": "string"},
                "language": {"type": "string", "description": "The person's language, e.g. zh-CN"},
                "timezone": {"type": "string", "description": "IANA zone: <user_timezone> unless they named a place"},
                "business": {"type": "object", "properties": {
                    "summary": {"type": "string"}, "audience": {"type": "string"},
                    "success_signal": {"type": "string"}}},
                "services": {"type": "array", "items": {"type": "object", "required": ["key"], "properties": {
                    "key": {"type": "string"},
                    "stages": {**_SPEC_KEYS, "description": "The stages of the work this service runs, in order"}}}},
                "agents": {"type": "array", "description": "One per service", "items": {
                    "type": "object", "required": ["service_key", "role"], "properties": {
                        "service_key": {"type": "string"}, "role": {"type": "string"},
                        "deliverables": {"type": "array", "items": {"enum": [
                            "docx", "pptx", "xlsx", "pdf", "image", "video", "audio", "website"]},
                            "description": "File kinds this Agent must make"},
                        "integrations": {**_SPEC_KEYS, "description": "Platforms it works on, e.g. instagram"},
                        "reuse_agent_id": {"type": "string"}}}},
                "workflows": {"type": "array", "items": {"type": "object", "required": ["slug", "service_key"], "properties": {
                    "slug": {"type": "string", "description": "snake_case; a pattern's Workflows share it as prefix"},
                    "service_key": {"type": "string"}, "purpose": {"type": "string"},
                    "stages": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "kind": {"enum": ["agent", "approval"]},
                        "service_key": {"type": "string"}}}},
                    "run_inputs": {"type": "array", "items": {"type": "object"}},
                    "stat_keys": _SPEC_KEYS, "publishes_to": _SPEC_KEYS,
                    "dated": {"type": "boolean", "description": (
                        "Items on dates a plan sets, which the person confirms; a fixed rhythm (every Monday) is not dated")}}}},
                "stats": {"type": "array", "items": {"type": "object", "required": ["key"], "properties": {
                    "key": {"type": "string"}, "name": {"type": "string"},
                    "workflow": {"type": "string", "description": "The Workflow that records it"}}}},
                "automations": {"type": "array", "items": {"type": "object", "required": ["key", "cadence"], "properties": {
                    "key": {"type": "string"}, "workflow": {"type": "string"}, "service_key": {"type": "string"},
                    "cadence": {"type": "string", "description": "When it runs, in the person's words"},
                    "timezone": {"type": "string"}, "why": {"type": "string"}}}},
                "routines": {"type": "array", "description": "Every recurring thing the person named", "items": {
                    "type": "object", "required": ["said"], "properties": {
                        "said": {"type": "string", "description": "Their words, e.g. 每月1号复盘"},
                        "automation": {"type": "string", "description": "The automation key that runs it"}}}},
                "knowledge": {"type": "array", "items": {"type": "object", "required": ["key"], "properties": {
                    "key": {"type": "string"}, "name": {"type": "string"}, "purpose": {"type": "string"},
                    "for_services": _SPEC_KEYS, "source_ids": _SPEC_KEYS}}},
                "integrations": {"type": "array", "items": {"type": "object", "properties": {
                    "provider": {"type": "string"}, "required_by": _SPEC_KEYS}}},
                "briefs": {"type": "object", "description": (
                    "A brief per planned part (agents, knowledge, workflows, automations, app) in the person's "
                    "words: what it must do and what they asked for, nothing invented."), "properties": {
                    part: {"type": "string", "maxLength": 6000}
                    for part in ("agents", "knowledge", "workflows", "automations", "app")}},
            },
        },
    },
}

DESIGN_PARTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_design_parts",
        "description": (
            "Command the part designers of the next wave at once (agents, knowledge and workflows first; "
            "automations and app once the Workflows are accepted). Each reads the committed build spec and its "
            "brief and returns a checked candidate that changes nothing until you accept it with ws_accept_parts. "
            "Omit parts to run the next wave. To send a candidate back, name its part with feedback (at most twice). "
            "A part whose designer could not run comes back as status lead: build that part yourself with the "
            "tools its next names, and do not mention it to the person."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "parts": {"type": "array", "items": {"type": "object", "required": ["part"], "properties": {
                    "part": {"enum": ["agents", "knowledge", "workflows", "automations", "app"]},
                    "brief": {"type": "string", "maxLength": 6000,
                              "description": "Replaces the spec's brief for this run"},
                    "feedback": {"type": "string", "maxLength": 4000,
                                 "description": "Why its last candidate was not accepted and what to change"}}}},
            },
        },
    },
}

PROPOSE_WORKFLOW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_workflow",
        "description": (
            "Add or replace a portable native Workflow definition in this draft. Use stable snake_case slug and "
            "structured steps; this creates no run. The start node is added for you: list the first step without "
            "a predecessor and route the rest with next. References are {{name}} for a run input or variable and "
            "{{step_id}} / {{step_id.field}} for an earlier step's result; there is no inputs./steps./vars. prefix. "
            "An agent step whose fields a later step reads ({{edit.deliverable_document_id}}) returns those fields, "
            "unless it sets its own output_schema: then it needs output_format:'json' and its schema must list every "
            "field read, or the graph is rejected. "
            "The same validator that runs at creation checks the graph and returns its errors here."
        ),
        "parameters": {"type": "object", "required": ["draft_id", "slug", "name", "steps"], "properties": {
            "draft_id": {"type": "string"}, "slug": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
            "name": {"type": "string", "minLength": 1}, "description": {"type": "string"},
            "steps": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["id", "type", "config"], "properties": {
                "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "name": {"type": "string", "description": "What the step does, as the person sees it in progress and approvals (e.g. 'Owner approval')."},
                "type": {"type": "string", "description": (
                    "Native Workflow node type: agent (one role's stage of work), wait (a person approves or answers), "
                    "workflow_project (durable project record; never tool/manor), condition ({expression:'plan_approval.choice "
                    "== \"approve\"'} routed by true_next/false_next), switch, code ({language:'python',code,output_var,"
                    "output_format:'json'}; reads the global inputs dict, prints its result), transform, datetime "
                    "({operation:now|add|subtract|format, value?, amount?, unit?, timezone?}), subworkflow ({workflow_id:'<slug of "
                    "another Workflow of this draft>', inputs:[{key,value}]}), foreach_subworkflow ({workflow_id, "
                    "over:'{{plan.items}}', item_key:'id', item_var:'item'}: one child run per item), "
                    "workflow_action_grant (create after an approval: {approval_step_id, project_id, grant_type, scope, "
                    "scope_schema}), workflow_effect_deadline ({deadline_at, action_keys}), publication_receipt "
                    "({receipt:'{{publish.receipt}}', payload, require_verified:true}), notify, stop, merge, end. "
                    "Propose a sub-Flow's Workflow first; a Flow only other Flows run sets internal:true and "
                    "trigger_type:'internal'.")},
                "next": {"type": "array", "items": {"type": "string"}},
                "config": {"type": "object", "description": (
                    "agent: {service_key:'<a draft service>', prompt:'... {{topic}} ... {{research}}', expects?:['files'|'effects']}; "
                    "expects says what the stage must leave behind (a saved file, a completed action) so a stage that saves "
                    "nothing never counts as done. notify: {channel:'workspace_chat', message:'Weekly report: {{write.report_document_id}}'} tells "
                    "the Workspace's people in its chat; an agent step cannot post to chat. wait approval: {wait_type:'approval', message:'Approve the final draft?', "
                    "options:['approve','revise'], approval_values:['approve'], revise_from:'<earlier step id>', "
                    "response_variable:'final_approval'} (later steps read {{final_approval.choice}}). What the person approves "
                    "is delivered as it is, so the step before it saves the deliverable finished and puts notes for the reviewer "
                    "in a separate file. The person sees the "
                    "files earlier steps saved since they last answered; review:{files:['<document id or {{reference}}>']} names "
                    "them instead. A wait where the person confirms something they did themselves (delivered the file, signed): "
                    "{wait_type:'approval', message:'Confirm once the Word file is delivered.', options:['confirm']}. "
                    "workflow_project create: {operation:'create',project_type:'...',schema_version:1,"
                    "state_schema:{type:'object',properties:{status:{type:'string'},deliverable_document_id:{type:'string'}}},"
                    "state:{},current_stage:'new', folder?:{name:'{{topic}}'}}; each run creates its own project; add project_key only "
                    "so a repeat finds the same one, built from a short id, code or period ('{{order_id}}'), never a brief, "
                    "message or fixed word; folder.under is only for a parent project's "
                    "own folder ({{parent.state.project_root}}); a folder sets state.project_root, so declare project_root:{type:'string'} "
                    "in state_schema when the App lists the project's files. State fields used by App must appear in state_schema.properties. "
                    "workflow_project patch (advance a stage): {operation:'patch',project_id:'{{create_article.project_id}}',"
                    "project_type:'...',schema_version:1,expected_revision:'{{<previous project step>.revision}}',"
                    "patch:{status:'writing',draft_document_id:'{{write.draft_document_id}}'},current_stage:'writing',"
                    "state_schema:<same as create>}; after an approval add history_event:{kind:'review',"
                    "decision:'{{final_approval.choice}}',note:'{{final_approval.note}}'} (appended to state.history with "
                    "a timestamp; declare history:{type:'array'}). An approval wait's own id has no fields. list_appends:"
                    "[{path,key,items}], list_upserts and history_event apply only on a patch. Operations are only "
                    "create, claim, get, list, lease_due, complete_lease, patch; a project step outputs project_id, revision, "
                    "current_stage and state (no 'id'); there is no 'update' operation."
                )}
            }}},
            "internal": {"type": "boolean"}, "trigger_type": {"type": "string"}, "binding_config": {"type": "object"},
            "variables": {"type": "array"},
            "run_inputs": {"type": "array", "items": {"type": "object"}, "description": (
                "The inputs a run asks for: {key,label,type,required,hidden,description,schema}; the schema picks "
                "the control. A file is an upload (type:'json', schema {type:'array',items:{type:'object',"
                "required:['document_id'],properties:{document_id:{type:'string'}}},'x-ui':{control:'documents',"
                "accept:['image']}}), a few fixed values an enum, a day format:'date' (a period: an object "
                "{start,end} with 'x-ui':{control:'date_range'}), format:'email'/'phone', 'x-ui' control color, "
                "rating, tags, rich_text, address, signature or line_items, a record of this Workspace schema {type:'string','x-ui':{control:'workflow_project_select',"
                "project_type:'<type>'}} (several records: type:'json', schema {type:'array',items:{type:'string'},"
                "minItems:1,'x-ui':{control:'workflow_project_select',project_type:'<type>'}}); a plain string is only for words the person writes, or a planned code a "
                "hidden input takes. A Flow that changes a record takes only its picker and asks the changes in an "
                "input wait whose defaults are the record's current values ({{<get step>.state.<field>}}).")},
            "output_schema": {"type": "object"}, "enabled": {"type": "boolean"}
        }}
    }
}

PROPOSE_RECORD_COLLECTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_record_collection",
        "description": (
            "Add the Workflows for a collection people keep by hand and change from the App (a menu's dishes, a "
            "catalogue's products, a list of offers): <slug>_add, <slug>_edit (picks a record and opens a form "
            "holding its values; a field left out keeps its value) and <slug>_take_off (moves it to off_stage). "
            "No step runs a model. Use this instead of writing those Flows with ws_propose_workflow."
        ),
        "parameters": {"type": "object", "required": ["draft_id", *_RECORD_COLLECTION_REQUIRED],
                       "properties": {"draft_id": {"type": "string"}, **_RECORD_COLLECTION_PARAMETERS}},
    }
}

PROPOSE_SOURCE_WATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_source_watch",
        "description": (
            "Add the Workflows for work that starts when something new arrives in an account (a customer's DM or "
            "email, a comment on a post): <slug>_watch looks for what is new on a schedule and starts run_workflow "
            "once for each new thing, passing its id as run_input. Write run_workflow first with ws_propose_workflow. "
            "The watch's Automation is added with it. No channel hands such a message to a Workflow, so use this "
            "instead of a Flow that only takes text the person pastes in."
        ),
        "parameters": {"type": "object", "required": ["draft_id", *_SOURCE_WATCH_REQUIRED],
                       "properties": {"draft_id": {"type": "string"}, **_SOURCE_WATCH_PARAMETERS}},
    }
}

PROPOSE_STAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_propose_stat",
        "description": "Add or replace one portable native Stat definition. The collector and key must be explicit; this never creates observations.",
        "parameters": {"type": "object", "required": ["draft_id", "key", "name"], "properties": {
            "draft_id": {"type": "string"}, "key": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
            "name": {"type": "string", "minLength": 1}, "library_key": {"type": "string"}, "description": {"type": "string"},
            "value_type": {"type": "string"}, "unit": {"type": "string"}, "window": {"type": "string"},
            "collector_type": {"type": "string"}, "collector_config": {"type": "object"}, "collection_cadence": {"type": "string"}, "goal_eligible": {"type": "boolean"}
        }}
    }
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
            "stored bindings and resolves missing Skills before binding them. "
            "Only already connected integrations may be selected."
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
                    "maxItems": 8,
                    "description": (
                        "Skills this agent needs that don't yet exist, OR a method / "
                        "playbook the user supplied that this agent must follow "
                        "(set source_ids). The platform resolves the Skill and binds "
                        "its available Tool/MCP dependencies. "
                        "Use only when the capability is NOT "
                        "covered by an existing skill or tool, or for user-supplied methods."
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
                            "source_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Draft source ids (from <sources>) this skill's "
                                    "procedure was distilled from."
                                ),
                            },
                        },
                    },
                },
                "missing_integrations": {
                    "type": "array",
                    "description": (
                        "Legacy setup metadata. Leave empty for conversational "
                        "Workspace creation; connect the integration first."
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
            "Assign a staff member the person named to the workspace, "
            "optionally bound to a specific service. Call "
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
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Draft source ids from <sources> whose documents or pages "
                        "are added to this knowledge net at creation and whose text "
                        "grounds the starter document. Only status=ready sources."
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
            "Record an integration the user asked for that is not connected "
            "yet, when no Agent binding already records it (e.g. an analytics "
            "platform several services read). It never blocks creation: the "
            "Workspace is created with the requirement waiting for connection, "
            "the user is asked to connect it in Workspace chat, and the work "
            "that needs it starts once it is connected. Agent integrations "
            "chosen by ws_search_capabilities are recorded automatically."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "provider", "purpose"],
            "properties": {
                "draft_id": {"type": "string"},
                "provider": {"type": "string"},
                "purpose": {"type": "string", "minLength": 5},
                "required": {
                    "type": "boolean",
                    "description": (
                        "Whether the linked work needs it to run (default true). "
                        "Either way creation is not blocked."
                    ),
                },
                "linked_service_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Which workspace services wait for this connection.",
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
            "The server reuses the stored validated plan; do not transcribe ids. "
            "Repeating the call for an unchanged service, agent_name, and intent "
            "returns that stored plan without matching again."
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
            "stack into a list. A channel is a messaging surface with an "
            "existing account; pick the platform's own channel_type when "
            "one exists (e.g. twitter_x, slack, email). Use channel_type="
            "'other' only for a platform with no channel_type here, and then "
            "name it in `provider` and say in `purpose` what the workspace "
            "does on it. 'other' records intent only: there is no 'other' "
            "account to connect, agents reach the platform through a "
            "dedicated integration or the Chrome extension (either is "
            "enough; both is best), and it is optional unless required=true."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "role", "channel_type", "purpose"],
            "properties": {
                "draft_id": {"type": "string"},
                "role": {"enum": ["primary_external", "secondary_external", "internal"]},
                "channel_type": {"enum": CHANNEL_TYPES},
                "provider": {
                    "type": "string",
                    "description": (
                        "Platform key or name (e.g. 'twitter_x', 'xiaohongshu'). "
                        "Required when channel_type='other'."
                    ),
                },
                "purpose": {"type": "string", "minLength": 5},
                "required": {
                    "type": "boolean",
                    "description": (
                        "Whether the workspace cannot operate without this "
                        "channel connected. Defaults to true, except "
                        "channel_type='other' defaults to false."
                    ),
                },
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
            "guardrails only when those patterns express its full scope. "
            "System-generated defaults must use approval_required at most, "
            "never a hard block. "
            "Content, timing, or other conditions that action keys cannot "
            "represent must remain notice guidance, not a blanket action ban."
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
                    "description": (
                        "Runtime meaning when this can be enforced by action key. "
                        "approval_required: the agent pauses and acts only after a "
                        "person approves. draft_only: HARD BLOCK - the agent never "
                        "performs the action; it only prepares drafts and a person "
                        "publishes/sends by hand. deny: hard block, same enforcement "
                        "as draft_only. notice: guidance only, nothing enforced. "
                        "deny and draft_only are accepted only together with "
                        "enforcement_scope='all_matching_actions'; a missing "
                        "scope or conditional scope is rejected without adding a rule. "
                        "They also require "
                        "hard_block_requested_by_user=true; otherwise the rule is "
                        "recorded as approval_required. "
                        "Never put approval_required and draft_only/deny on the same "
                        "action patterns: the hard block wins and the approval is dead."
                    ),
                },
                "enforcement_scope": {
                    "type": "string",
                    "enum": ["all_matching_actions", "conditional"],
                    "description": (
                        "Declare whether the user's rule applies to every call "
                        "matching the action patterns, regardless of content, timing, "
                        "or other conditions. deny/draft_only require "
                        "all_matching_actions. Use conditional with notice when "
                        "action keys cannot represent the condition, and preserve "
                        "that condition in description. This declaration does not "
                        "prove the meaning of the user's words; do not invent a "
                        "broader prohibition."
                    ),
                },
                "hard_block_requested_by_user": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY when the user explicitly asked for a prohibition "
                        "in their own words (禁止 / 不允许 / never / do not / must not / "
                        "add-only). Required for rule_type deny or draft_only together "
                        "with user_request_quote; without both, those are downgraded to "
                        "approval_required. A wish to draft, review, or confirm first "
                        "is NOT a prohibition."
                    ),
                },
                "user_request_quote": {
                    "type": "string",
                    "description": (
                        "The user's own words, verbatim from this conversation, that "
                        "asked for the prohibition (e.g. '禁止直接发布'). Checked "
                        "against the chat: a quote the user never wrote, or one with "
                        "no prohibition wording, downgrades the rule to approval_required."
                    ),
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
            "Schedule a recurring clock automation. Only call this when the "
            "user described a schedule. Pass cron_expr (or every_seconds) and "
            "timezone for the exact cadence; the result echoes the schedule "
            "that will run. Event triggers (on new message, when X happens) "
            "are not schedules and are rejected."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "automation_key", "description", "trigger"],
            "properties": {
                "draft_id": {"type": "string"},
                "automation_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "description": {"type": "string", "minLength": 10},
                "trigger": {"type": "string", "description": "The cadence in the user's words, shown to them, e.g. '每周一上午9点', 'weekdays 08:30'. Used to derive the schedule only when cron_expr/every_seconds is absent."},
                "cron_expr": {"type": "string", "description": "5-field cron in `timezone`: minute hour day-of-month month day-of-week (0=Sunday). E.g. '0 9 * * 1' every Monday 09:00, '0 9 1 * *' the 1st of each month, '30 8 * * 1-5' weekdays."},
                "every_seconds": {"type": "number", "exclusiveMinimum": 0, "description": "Fixed interval instead of cron, e.g. 7200 for every 2 hours."},
                "timezone": {"type": "string", "description": "IANA timezone of the cadence, e.g. 'Asia/Shanghai' when the user says Beijing time. Omit to use the user's own timezone."},
                "service_key": {"type": "string"},
                "scheduled_job": {"type": "object", "description": "Optional native scheduled-job declaration. Include job_id, schedule and execution_target; workflow targets use workflow_slug from ws_propose_workflow."},
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
            "List every published Blueprint, most relevant to the user's need first. "
            "Call it before building from scratch. Each result has match.level "
            "(high/medium/low/none), match.score (share of the need it covers) and "
            "match.reasons (which catalog fields matched which terms). The level is a "
            "lexical hint: confirm the fit yourself from use_when and outcome_summary."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": (
                        "The user's need in keywords: domain, audience, outcomes, channels, "
                        "deliverables and cadence. Write it in English, the catalog's "
                        "language, and add the user's own key terms if they wrote in "
                        "another language. Draft basics and services are added automatically."
                    ),
                },
            },
        },
    },
}


SUGGEST_BLUEPRINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_suggest_blueprint",
        "description": (
            "Record the single published Blueprint you recommend, so the draft panel "
            "shows it. Use only when its use_when and outcome_summary cover the user's "
            "core outcome; matching terms alone are not a fit."
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


APPLY_BLUEPRINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_apply_blueprint",
        "description": (
            "Fill the draft from a published Blueprint: its services, Agents, Workflows, "
            "metrics, Goals, rules, automations, App and knowledge replace what the draft "
            "has. Call it only once the user chose to install the Blueprint or to "
            "customize from it. To customize, keep its parts and add or change only what "
            "the user's brief asks for and it lacks, then lint and mark ready."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "blueprint_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "blueprint_id": {"type": "string"},
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


READ_SOURCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_read_source",
        "description": (
            "Read the full text of a source the user attached to this draft "
            "(uploaded file, knowledge document, or pasted link listed in <sources>). "
            "Page with offset until next_offset is null. Read sources before "
            "designing services, skills, or knowledge from user-supplied material."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "source_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "source_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 40000},
                "refresh": {
                    "type": "boolean",
                    "description": "Re-fetch a link source that failed or changed.",
                },
            },
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


ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ws_ask_user",
        "description": (
            "Attach answer buttons to the question you are asking the user in this "
            "reply. Call it for every question: give up to 3 short options and the "
            "one you recommend with a one-sentence reason, in the user's language. "
            "The user can still answer in their own words."
        ),
        "parameters": {
            "type": "object",
            "required": ["draft_id", "question", "options", "recommended", "recommendation_reason"],
            "properties": {
                "draft_id": {"type": "string"},
                "question": {"type": "string", "minLength": 1},
                "options": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "recommended": {"type": "string", "minLength": 1},
                "recommendation_reason": {"type": "string", "minLength": 1, "maxLength": 280},
            },
        },
    },
}


ALL_TOOL_SCHEMAS = [
    ASK_USER_SCHEMA,
    COMMIT_BASICS_SCHEMA,
    PROPOSE_SERVICE_SCHEMA,
    DESIGN_APP_VIEW_SCHEMA,
    ACCEPT_APP_VIEW_SCHEMA,
    ACCEPT_PARTS_SCHEMA,
    DESIGN_WORKFLOWS_SCHEMA,
    COMMIT_BUILD_SPEC_SCHEMA,
    DESIGN_PARTS_SCHEMA,
    PROPOSE_WORKFLOW_SCHEMA,
    PROPOSE_RECORD_COLLECTION_SCHEMA,
    PROPOSE_SOURCE_WATCH_SCHEMA,
    PROPOSE_STAT_SCHEMA,
    PROPOSE_GOAL_SCHEMA,
    PROPOSE_AGENT_MAPPING_SCHEMA,
    REQUEST_CUSTOM_AGENT_SCHEMA,
    ASSIGN_STAFF_SCHEMA,
    ATTACH_KNOWLEDGE_SCHEMA,
    SEARCH_CAPABILITIES_SCHEMA,
    PROPOSE_CHANNEL_SCHEMA,
    FLAG_MISSING_INTEGRATION_SCHEMA,
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
    APPLY_BLUEPRINT_SCHEMA,
    GET_DRAFT_SCHEMA,
    READ_SOURCE_SCHEMA,
    LINT_DRAFT_SCHEMA,
    MARK_READY_SCHEMA,
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HARD_BLOCK_RULE_TYPES = frozenset({"deny", "draft_only", "never_allow", "block"})
_ULID_RE = re.compile(r"^[A-Z0-9]{26}$")
_CHANNEL_ACTION_PATTERNS = {
    "email": {"email.send", "email.delete"},
}
_CHANNEL_BINDING_ALIASES = {
    "email": {"email", "gmail", "outlook", "smtp", "sendgrid"},
}
# Rules and the governance policy compiled from them are the user's stated
# limits. Every draft rule type (approval_required, deny, draft_only, notice)
# only restricts, so dropping a pattern or rewording a rule because a channel
# went away (or was never connected) can only weaken what the user asked for.
_CHANNEL_SCRUB_PRESERVED_KEYS = {"rules", "governance_policy", "governance"}
_CHANNEL_TEXT_SCRUB_SKIP_KEYS = {
    *_CHANNEL_SCRUB_PRESERVED_KEYS,
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


def _tool_failed(exc: Exception) -> str:
    """A crash as the model should see it: our own checks say what to change, faults stay in the logs."""
    if isinstance(exc, ValueError):
        return _err(str(exc))
    return _err(
        "this step failed on our side",
        hint="check the draft, try the step once more, and if it still fails carry on with the rest",
    )


def _refused(detail: Dict[str, Any]) -> str:
    """A refusal already shaped as ``{"error": ..., **extra}``."""
    return json.dumps({"ok": False, **detail}, ensure_ascii=False)


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
    """Remove automation/binding references to a channel the user removed.

    ``ws_remove(kind="channel")`` is a semantic operation, not just a list
    edit. If a channel disappears, automation text and bindings that still
    reference it would make the draft preview lie to the user. Rules are
    left exactly as written: an approval or ban on an action that cannot run
    yet costs nothing, and it must still hold once the channel comes back.
    """
    key = _channel_key(key)
    if not key:
        return {"automations": 0}

    removed = list(fields.get("_removed_channels") or [])
    if key not in removed:
        removed.append(key)
    fields["_removed_channels"] = removed

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
        "automations": automation_changes,
        "text_fields": text_changes,
        "knowledge_attachments": knowledge_removed,
    }


def _reconcile_removed_channel_references(fields: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "automations": 0,
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
    from packages.core.services.workspace_setup_service import normalize_workspace_draft_governance

    fields = normalize_workspace_draft_governance(fields)
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
        (
            # Listing a requirement grants nothing, so the selection itself
            # has to cover it or the Agent fails the draft's readiness check.
            "Service required runtime capabilities (the selected ids must cover "
            "every one; the candidate capability:<id> covers <id>, and so does "
            "any one of the tools it lists, which grants less): "
            f"{service.get('required_capabilities') or []}"
        ),
    ]
    if context["rules"]:
        prompt_parts.append(f"Applicable rules: {context['rules']}")
        prompt_parts.append(
            "A rule's rule_type is its execution policy: approval_required means "
            "the Agent may perform the action after human approval, even when "
            "the original template description says never or do not. Do not "
            "remove that action's capabilities or recreate a hard block from "
            "default prose. Explicit user-confirmed deny rules remain bans."
        )
    prompt_parts.append(
        # Staging 2026-11-09, 3 of 3 candle shop drafts: every Agent got
        # scheduling, one the whole Flow-authoring set, one a store platform
        # nobody named, and the support Agent could publish and delete posts.
        "This Agent does this service's work inside a Workspace whose Flows and schedules are already "
        "set up: starting and following a Flow is workflow.run. Select a platform only when the text "
        "above names it or it is connected; when it only says 'my store' or 'orders', select no store "
        "platform. Of a platform's actions keep only those this service does: one that answers "
        "messages reads and replies, it does not publish or delete posts."
    )
    if context["intent"]:
        prompt_parts.append(f"Additional user requirements: {context['intent']}")
    return "\n".join(prompt_parts)


def _workspace_service_plan(catalog: Any, plan: Any, service: Dict[str, Any]) -> Any:
    """``plan`` without the Workspace-building capabilities its service did not ask for.

    Creating, scheduling or deleting Flows, scheduled jobs and Initiatives is
    how a Workspace is built, not the work a service's Agent does. A service
    that manages them lists the capability in ``required_capabilities``. A
    Skill brings the tools it declares, so one that declares them goes too
    (staging 2026-11-09: workspace-workflows gave a support Agent every
    Flow-authoring tool).
    """
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.services.agent_capability_catalog import AgentCapabilityKind

    required = {str(item) for item in service.get("required_capabilities") or []}
    unasked = {cap_id for cap_id, cap in CORE_CAPABILITIES.items()
               if cap.risk_level == "admin" and cap_id not in required}
    kept_tools = {name for cap_id, cap in CORE_CAPABILITIES.items() if cap_id not in unasked for name in cap.tool_names}
    building_tools = {name for cap_id in unasked for name in CORE_CAPABILITIES[cap_id].tool_names} - kept_tools
    by_id = {candidate.catalog_id: candidate for candidate in catalog.candidates}
    kept = [
        catalog_id for catalog_id in plan.selected_catalog_ids
        if (candidate := by_id.get(catalog_id)) is None
        or not (candidate.kind is AgentCapabilityKind.BUSINESS_CAPABILITY and candidate.ref in unasked)
        and not (candidate.kind is AgentCapabilityKind.TOOL and candidate.ref in building_tools)
        and not (candidate.kind is AgentCapabilityKind.SKILL
                 and building_tools & set(candidate.metadata.get("declared_tools") or ()))
    ]
    return plan if len(kept) == len(plan.selected_catalog_ids) else catalog.resolve(kept)


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


# ── ws_ask_user ─────────────────────────────────────────────────────────────

#: Draft field holding this turn's question until the transcript records it on
#: the architect's reply (``workspace_draft_service._record_visible_messages``).
PENDING_QUESTION_FIELD = "_pending_question"


async def _ask_user(db, *, entity_id: str, user_id: str = "", **kwargs):
    from packages.core.services.decision_recommendation import RecommendationError, reply_question

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    try:
        question = reply_question(
            kwargs.get("question"), kwargs.get("options"),
            kwargs.get("recommended"), kwargs.get("recommendation_reason"),
        )
    except RecommendationError as exc:
        return _err(str(exc))
    fields = dict(draft.fields or {})
    fields[PENDING_QUESTION_FIELD] = question
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"options": question["options"], "recommended": question["recommendation"]["option"]})


# ── ws_propose_service ──────────────────────────────────────────────────────

async def _propose_service(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    service_key = kwargs.get("service_key", "")
    if not _SLUG_RE.match(service_key):
        return _err("service_key must be snake_case", got=service_key)

    raw_required_capabilities = kwargs.get("required_capabilities", [])
    if not isinstance(raw_required_capabilities, list) or any(
        not isinstance(value, str) for value in raw_required_capabilities
    ):
        return _err("required_capabilities must be an array of canonical capability ids")
    from packages.core.ai.runtime.task_requirements import (
        normalize_task_runtime_capability_ids,
    )

    try:
        required_capabilities = normalize_task_runtime_capability_ids(
            raw_required_capabilities,
            allowed_ids=STRATEGIST_TASK_CAPABILITY_IDS,
            strict=True,
        )
    except ValueError as exc:
        return _err("invalid required_capabilities", detail=str(exc))

    new_service = {
        "service_key": service_key,
        "name": kwargs.get("name", ""),
        "description": kwargs.get("description", ""),
        "autonomy_level": kwargs.get("autonomy_level", "supervised"),
        "owner_role": kwargs.get("owner_role", ""),
        "required_capabilities": list(required_capabilities),
    }
    if kwargs.get("rationale"):
        new_service["rationale"] = kwargs["rationale"]
    handoff_to = kwargs.get("handoff_to")
    if handoff_to is not None:
        if not isinstance(handoff_to, list) or not all(
            isinstance(key, str) and _SLUG_RE.match(key) and key != service_key for key in handoff_to
        ):
            return _err("handoff_to must list other services' snake_case service_keys")
        if handoff_to:
            new_service["handoff_to"] = list(dict.fromkeys(handoff_to))
    if kwargs.get("human_review") is True:
        new_service["human_review"] = True

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

@dataclasses.dataclass(frozen=True)
class _SkillsToAcquire:
    """The lease a first ``draft_phase`` took to find a service's Skills."""
    token: str
    request_key: str
    specs: list
    catalog: Any
    mapping: Optional[dict]


def _agent_mapping(fields: Dict[str, Any], service_key: str) -> Optional[dict]:
    return next(
        (
            item for item in (fields.get("agent_mappings") or [])
            if (item or {}).get("service_key") == service_key
        ),
        None,
    )


async def _request_custom_agent(db, *, entity_id: str, user_id: str = "", draft_phase=None, **kwargs):
    """Save a custom Agent design for one service.

    ``draft_phase`` (from an Architect turn) opens a short locked transaction;
    finding missing Skills runs between the read and the save, holding none.
    """
    if draft_phase is None:
        return await _request_custom_agent_in_draft(db, entity_id=entity_id, user_id=user_id, **kwargs)
    async with draft_phase():
        outcome = await _request_custom_agent_in_draft(
            db, entity_id=entity_id, user_id=user_id, staged=True, **kwargs,
        )
    return outcome


async def _request_custom_agent_in_draft(
    db, *, entity_id: str, user_id: str = "", staged: bool = False,
    acquired: Optional[_SkillsToAcquire] = None, **kwargs,
):
    """The locked part of ``_request_custom_agent``.

    ``staged`` returns a ``_SkillsToAcquire`` lease instead of finding missing
    Skills in this transaction; ``acquired`` is that lease, now recorded.
    """
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    service_key = kwargs.get("service_key", "")
    if acquired is not None and _agent_mapping(draft.fields or {}, service_key) != acquired.mapping:
        return _err(
            "the Agent for this service changed while its Skills were found",
            hint="review the current Agent, then call ws_request_custom_agent again",
            service_key=service_key,
        )
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
    capability_catalog = None
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

    # Method skills cite the draft sources they were distilled from. Resolve the
    # ids to portable refs here so provisioning never has to read the draft.
    from packages.core.services.workspace_draft_sources import find_source, ready_source_ids

    readable_source_ids = ready_source_ids(draft.fields)
    resolved_missing_skill_specs: List[Dict[str, Any]] = []
    for raw_spec in kwargs.get("missing_skill_specs") or []:
        if not isinstance(raw_spec, dict):
            continue
        spec = dict(raw_spec)
        spec_source_ids = list(dict.fromkeys(
            str(s).strip() for s in (spec.get("source_ids") or []) if str(s or "").strip()
        ))
        unknown_source_ids = [s for s in spec_source_ids if s not in readable_source_ids]
        if unknown_source_ids:
            return _err(
                "missing_skill_specs.source_ids must reference readable draft sources",
                unknown=unknown_source_ids,
                readable=sorted(readable_source_ids),
                skill=spec.get("name"),
            )
        if spec_source_ids:
            spec["source_ids"] = spec_source_ids
            spec["source_refs"] = []
            for source_id in spec_source_ids:
                source = find_source(draft.fields, source_id) or {}
                spec["source_refs"].append({
                    key: value
                    for key, value in {
                        "source_id": source_id,
                        "kind": source.get("kind"),
                        "name": source.get("name"),
                        "document_id": source.get("document_id"),
                        "url": source.get("url"),
                    }.items()
                    if value
                })
        resolved_missing_skill_specs.append(spec)

    acquisition_receipts = []

    # A connection the user has not made yet is recorded as a pending
    # requirement (missing_integrations below), never a reason to drop the
    # capability: the binding works as soon as the account is connected.
    # Only a dependency no account can satisfy stops the Agent here.
    unavailable_dependencies = [
        dict(item) for item in (capability_plan.setup_required if capability_plan is not None else ())
        if item.get("setup_kind") == "skill_dependency_unavailable"
    ]
    if unavailable_dependencies:
        return _err(
            "Agent dependencies are not available",
            setup_required=unavailable_dependencies,
            hint="Select an available alternative, then search again.",
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
        "missing_skill_specs": resolved_missing_skill_specs,
        "skill_acquisition_receipts": acquisition_receipts,
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
    # Reject invented tool / skill / MCP names here, at the write, instead of
    # letting them reach finalize (2026-09-11: a made-up tool name failed the
    # whole workspace creation and the user had to paste the error back).
    # Plan-derived bindings come from the validated catalog and need no check;
    # only the free-text legacy fields can carry hallucinated names.
    if capability_plan is None:
        available_tool_refs, available_skill_refs, available_mcp_refs = await _binding_vocabulary(
            db,
            entity_id=entity_id,
            user_id=user_id,
            tool_refs=set(create_draft["tool_bindings"]),
            skill_refs=set(create_draft["skill_bindings"]),
            mcp_refs=set(create_draft["mcp_bindings"]),
        )
        unknown_tools = [ref for ref in create_draft["tool_bindings"] if ref not in available_tool_refs]
        unknown_skills = [ref for ref in create_draft["skill_bindings"] if ref not in available_skill_refs]
        unknown_mcp = [ref for ref in create_draft["mcp_bindings"] if ref not in available_mcp_refs]
        if unknown_tools or unknown_skills or unknown_mcp:
            return _err(
                "unknown bindings",
                tools=unknown_tools,
                skills=unknown_skills,
                mcp=unknown_mcp,
                hint=(
                    "Bindings must come from ws_search_capabilities for this service_key; "
                    "do not invent tool, skill, or MCP names."
                ),
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

    names = {name.strip() for name in (staff.name, staff.display_name) if name and name.strip()}
    said = [str((draft.fields or {}).get(STAFF_REQUEST_TEXT_FIELD) or "").casefold()]
    said.extend(
        str(message.get(STAFF_REQUEST_TEXT_FIELD) or "").casefold()
        for message in draft.messages or []
        if isinstance(message, dict) and message.get("role") == "user"
    )
    # Han text does not require spaces around a name, as in 让Amy审稿.
    word = r"[^\W㐀-鿿豈-﫿]"
    if not any(
        re.search(rf"(?<!{word}){re.escape(name.casefold())}(?!{word})", text)
        for name in names for text in said
    ):
        return _err(
            "the person did not name this staff member",
            staff_name=sorted(names)[0] if names else staff_id,
            hint=("Assign only people named in the user's own messages. Ask the user to name the person "
                  "if needed. Work the user approves themselves needs no assignment."),
        )

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

    from packages.core.services.workspace_draft_sources import ready_source_ids

    source_ids = list(dict.fromkeys(
        str(s).strip() for s in (kwargs.get("source_ids") or []) if str(s or "").strip()
    ))
    readable = ready_source_ids(draft.fields)
    unknown = [s for s in source_ids if s not in readable]
    if unknown:
        return _err(
            "unknown or unreadable source_ids",
            unknown=unknown,
            readable=sorted(readable),
        )

    attachment = {
        "name": name,
        "purpose": purpose,
        "mode": mode,
        "template_group_id": template_group_id,
        "source_ids": source_ids,
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
    linked_service_keys = _service_key_list(kwargs.get("linked_service_keys"))
    flagged = list(fields.get("flagged_integrations") or [])
    # One requirement per provider. An Agent design that already records it
    # keeps its own entry; this call only widens which services wait for it.
    existing = next(
        (f for f in flagged if isinstance(f, dict) and _as_nonempty_str(f.get("provider")) == provider),
        None,
    )
    if existing is None:
        existing = {"provider": provider, "source": "explicit", "linked_service_keys": []}
        flagged.append(existing)
    existing["purpose"] = existing.get("purpose") or kwargs.get("purpose", "")
    existing["required"] = bool(existing.get("required", False) or kwargs.get("required", True))
    existing["linked_service_keys"] = list(dict.fromkeys(
        [*_service_key_list(existing.get("linked_service_keys")), *linked_service_keys]
    ))
    existing["blocks_creation"] = False
    if resolved.covered_provider:
        existing.setdefault("covered_provider", resolved.covered_provider)
    fields["flagged_integrations"] = flagged
    draft.fields = fields
    await _persist(db, draft)
    return _ok({
        "provider": provider,
        "requested_provider": requested_provider,
        "covered_provider": resolved.covered_provider,
        "flagged_count": len(flagged),
    })


async def _search_capabilities(db, *, entity_id: str, user_id: str = "", draft_phase=None, **kwargs):
    """Match one service semantically, plus return Workspace-only resources.

    Direct callers that omit ``service_key`` retain the full legacy inventory.
    The Architect schema requires it, keeping the large actor catalog inside
    the bounded matcher instead of truncating it in the outer chat context.

    ``draft_phase`` (from an Architect turn) opens a short locked transaction;
    the semantic matcher runs between the read and the save, holding none.
    """
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup
    from packages.core.models.staff import Staff
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )

    draft_id = kwargs.get("draft_id", "")
    phase = draft_phase or contextlib.nullcontext
    async with phase():
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
        # Aggregators and catalog-only providers are not existing actor connections.

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

        stored_plan = _current_agent_capability_plan(
            fields,
            service_key=service_key,
            agent_name=agent_name,
            intent=extra_intent,
            context=context,
            agent_catalog=agent_catalog,
        )
        if stored_plan is not None:
            # Matching the same request against the same catalog again only
            # costs provider calls; the stored ids are what it selected.
            return _ok(_capability_search_result(
                payload,
                shared_payload=shared_payload,
                agent_catalog=agent_catalog,
                plan=agent_catalog.resolve(stored_plan.get("capability_ids") or []),
                service_key=service_key,
                agent_name=agent_name,
                reused=True,
            ))

        if draft_phase is None:
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
        plan = _workspace_service_plan(agent_catalog, plan, service)
    except Exception as exc:
        # Even a recoverable provider failure must re-establish the turn lock
        # before the model can retry or execute another Draft mutation.
        match_error = exc

    if draft_phase is not None and match_error is not None:
        raise match_error
    async with phase():
        if draft_phase is None:
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
        else:
            # Re-read: the person or the Architect may have edited the Draft
            # meanwhile. Only this service's plan is written, and only while
            # the service still matches what was matched.
            draft = await _load_draft(db, draft_id, entity_id, user_id)
            if draft is None:
                return _err("draft not found")
            fields = dict(draft.fields or {})
            service = _agent_capability_service(fields, service_key)
            if service is None or _stable_fingerprint(_agent_capability_context(
                fields, service=service, service_key=service_key, intent=extra_intent,
            )) != _stable_fingerprint(context):
                return _err(
                    "the service changed while its capabilities were matched",
                    hint="call ws_search_capabilities again for this service_key",
                    service_key=service_key,
                )
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
    # Without a turn, keep the re-acquired row lock through the rest of the
    # Architect turn; the caller owns the final commit.
    return _ok(_capability_search_result(
        payload,
        shared_payload=shared_payload,
        agent_catalog=agent_catalog,
        plan=plan,
        service_key=service_key,
        agent_name=agent_name,
    ))


def _current_agent_capability_plan(
    fields: Dict[str, Any],
    *,
    service_key: str,
    agent_name: str,
    intent: str,
    context: Dict[str, Any],
    agent_catalog: Any,
) -> Optional[Dict[str, Any]]:
    """The stored plan for this exact request, while service and catalog are unchanged."""
    stored_plan = next(
        (
            item for item in (fields.get(_AGENT_CAPABILITY_PLANS_FIELD) or [])
            if isinstance(item, dict) and item.get("service_key") == service_key
        ),
        None,
    )
    if (
        stored_plan is None
        or _as_nonempty_str(stored_plan.get("agent_name")) != agent_name
        or _as_nonempty_str(stored_plan.get("intent")) != intent
        or stored_plan.get("context_fingerprint") != _stable_fingerprint(context)
        or stored_plan.get("catalog_fingerprint")
        != _agent_capability_catalog_fingerprint(agent_catalog)
    ):
        return None
    return stored_plan


def _capability_search_result(
    payload: Dict[str, Any],
    *,
    shared_payload: Dict[str, Any],
    agent_catalog: Any,
    plan: Any,
    service_key: str,
    agent_name: str,
    reused: bool = False,
) -> Dict[str, Any]:
    integrations_out = shared_payload["integrations"]
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
        "plan_reused": reused,
    })
    return payload


# ── ws_propose_channel ──────────────────────────────────────────────────────

async def _propose_channel(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    role = kwargs.get("role", "")
    channel_type = _as_nonempty_str(kwargs.get("channel_type"))
    provider = _as_nonempty_str(kwargs.get("provider"))
    unbacked = channel_type in UNBACKED_CHANNEL_TYPES
    if unbacked and (not provider or _channel_key(provider) in UNBACKED_CHANNEL_TYPES):
        return _err(
            "channel_type 'other' needs the platform named in `provider` "
            "(e.g. provider='xiaohongshu') so the workspace can say what it is; "
            "if the platform has its own channel_type in the enum, use that instead.",
            channel_type=channel_type,
        )
    block = {
        "channel_type": channel_type,
        "purpose": kwargs.get("purpose", ""),
        "login_required": bool(kwargs.get("login_required", False)),
        "linked_service_key": kwargs.get("linked_service_key", ""),
        "notes": kwargs.get("notes", ""),
    }
    if provider:
        block["provider"] = provider
    # ``other`` is a statement about a platform, not an account to connect, so
    # it does not block the workspace unless the author says it must.
    required_default = not unbacked
    block["required"] = bool(kwargs.get("required", required_default))

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
    rule = {
        "rule_key": rule_key,
        "description": kwargs.get("description", ""),
        "scope": kwargs.get("scope", "all"),
        "severity": kwargs.get("severity", "warn"),
    }
    rule_type = str(kwargs.get("rule_type") or "").strip().lower()
    enforcement_scope = str(kwargs.get("enforcement_scope") or "").strip().lower()
    downgraded_from = ""
    downgrade_reason = ""
    if rule_type in _HARD_BLOCK_RULE_TYPES:
        if enforcement_scope != "all_matching_actions":
            return _err(
                "A hard-block rule requires enforcement_scope='all_matching_actions'. "
                "No rule was changed. Action patterns block every matching call; "
                "they cannot enforce content, timing, or other conditions in the "
                "description. Preserve a conditional policy as rule_type='notice' "
                "guidance, or clarify whether the user wants every matching action banned.",
                code="hard_block_scope_required",
                rule_key=rule_key,
            )
        # The platform never adds a ban on its own. The model's flag is only
        # its opinion; the user's quoted words are checked against the chat
        # and must actually ask for a prohibition.
        downgrade_reason = _hard_block_request_problem(
            draft,
            flagged=bool(kwargs.get("hard_block_requested_by_user")),
            quote=str(kwargs.get("user_request_quote") or ""),
        )
        if downgrade_reason:
            downgraded_from = rule_type
            rule_type = "approval_required"
    if rule_type:
        rule["rule_type"] = rule_type
    if enforcement_scope:
        rule["enforcement_scope"] = enforcement_scope
    action_patterns = [
        str(p).strip() for p in (kwargs.get("action_patterns") or [])
        if str(p or "").strip()
    ]
    if action_patterns:
        rule["action_patterns"] = list(dict.fromkeys(action_patterns))
    fields["rules"] = _replace_in_list(
        fields.get("rules") or [], "rule_key", rule_key, rule,
    )
    from packages.core.services.workspace_setup_service import (
        EXPLICIT_HARD_BLOCK_RULES_FIELD,
        workspace_rule_fingerprint,
    )

    confirmations = dict(fields.get(EXPLICIT_HARD_BLOCK_RULES_FIELD) or {})
    if rule_type in _HARD_BLOCK_RULE_TYPES:
        confirmations[rule_key] = workspace_rule_fingerprint(rule)
    else:
        confirmations.pop(rule_key, None)
    if confirmations:
        fields[EXPLICIT_HARD_BLOCK_RULES_FIELD] = confirmations
    else:
        fields.pop(EXPLICIT_HARD_BLOCK_RULES_FIELD, None)
    _clear_agent_capability_plans(fields)
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    from packages.core.services.workspace_setup_service import (
        describe_rule_enforcement_conflict,
        rule_enforcement_conflicts,
    )

    warnings = [
        describe_rule_enforcement_conflict(conflict)
        for conflict in rule_enforcement_conflicts(fields["rules"])
        if rule_key in conflict["blocking_rule_keys"] + conflict["approval_rule_keys"]
    ]
    payload: Dict[str, Any] = {"rule_key": rule_key, "rule_type": rule_type or None, "warnings": warnings}
    if downgraded_from:
        payload["downgraded_from"] = downgraded_from
        warnings.insert(0, (
            f"rule_type={downgraded_from!r} recorded as 'approval_required': "
            f"{downgrade_reason}. A hard block needs hard_block_requested_by_user=true "
            "AND user_request_quote = the user's own words (verbatim, from this chat) "
            "asking for the prohibition; otherwise keep the approval gate."
        ))
    return _ok(payload)


def _hard_block_request_problem(draft, *, flagged: bool, quote: str) -> str:
    """Why a hard block may NOT be installed; empty when the user asked for it."""

    from packages.core.services.workspace_setup_service import user_requested_hard_block

    if not flagged:
        return "the user did not ask for a prohibition (hard_block_requested_by_user not set)"
    quote_text = " ".join(str(quote or "").split())
    if not quote_text:
        return "no user_request_quote was given"
    wanted = quote_text.casefold()
    user_texts = [
        " ".join(str(m.get("content") or "").split()).casefold()
        for m in (draft.messages or [])
        if isinstance(m, dict) and str(m.get("role") or "").strip().lower() == "user"
    ]
    if not any(wanted in text for text in user_texts):
        return f"user_request_quote {quote_text!r} does not appear in the user's messages (原话核对失败)"
    if not user_requested_hard_block(quote_text):
        return (
            f"user_request_quote {quote_text!r} has no prohibition wording "
            "(禁止 / 不允许 / never / do not ...); it reads as a wish to review first"
        )
    return ""


# ── Part designers: ws_design_<part> / ws_accept_parts ───────────────────────

async def _design_part(db, *, entity_id: str, user_id: str = "", part: str, draft_phase=None, **kwargs):
    """Run ``part``'s designer and keep its candidate in ``_candidates``.

    ``draft_phase`` (from an Architect turn) opens a short locked transaction;
    the sub-agent runs between the read and the save, holding none.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.workspace_part_designers import (
        candidate_reply,
        keep_candidate,
        part_designers,
        pending_candidates,
    )

    from packages.core.services.workspace_build_spec import set_progress
    from packages.core.services.workspace_part_designers import DesignJob, design_wave, note_design_failure

    designer = part_designers()[part]
    phase = draft_phase or contextlib.nullcontext
    brief = str(kwargs.get("brief") or "").strip()
    async with phase():
        draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
        if draft is None:
            return _err("draft not found")
        if not brief:
            return _err(f"brief is required: {designer.brief_hint}")
        context = designer.context_from_draft(dict(draft.fields or {}))
        # Feedback is about the candidate still waiting; the designer revises it.
        sent_back = pending_candidates(dict(draft.fields or {})).get(part) if kwargs.get("feedback") else None
    blocked = designer.blockers(context)
    if blocked:
        return _refused(blocked)
    outcome = (await design_wave(
        [DesignJob(designer, context, brief, kwargs.get("feedback"), sent_back)], entity_id=entity_id,
        user_id=user_id or None, workspace_id=None, surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
        keepalive=_draft_keepalive(draft_phase),
    ))[0]
    if outcome.error is not None or outcome.result.candidate is None:
        async with phase():
            draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
            if draft is None:
                return _err("draft not found")
            fields = dict(draft.fields or {})
            note_design_failure(fields, part)
            draft.fields = fields
            await _persist(db, draft)
    if outcome.error is not None:
        # Technical: reported to operators; the lead builds the part, the person hears nothing of it.
        return _err(f"the {designer.title} design is not available right now",
                    **({"next": f"{designer.fallback} Do not mention this to the person."} if designer.fallback else {}))
    result = outcome.result
    if result.candidate is None:
        return _err(f"the {designer.title} designer did not produce a candidate that passes its checks",
                    design_notes=result.notes, attempts=result.attempts,
                    **({"next": designer.fallback} if designer.fallback else {}))
    async with phase():
        # Re-read: the person or the Architect may have edited the Draft meanwhile.
        draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
        if draft is None:
            return _err("draft not found")
        fields = dict(draft.fields or {})
        _, errors, _ = designer.check(designer.context_from_draft(fields), result.candidate)
        if errors:
            return _err(f"the Draft changed while the {designer.title} was designed; command a new design",
                        errors=errors)
        keep_candidate(fields, part, {
            "value": result.candidate, "brief": brief, **result.details,
            "warnings": result.warnings, "design_notes": result.notes,
        })
        set_progress(fields, part, "candidate")
        draft.fields = fields
        await _persist(db, draft)
    return _ok(candidate_reply(designer, result))


def _draft_keepalive(draft_phase):
    """Renews the turn's hold on the Draft while designers run; None outside a turn."""
    if draft_phase is None:
        return None

    async def keepalive() -> None:
        async with draft_phase():
            pass

    return keepalive


async def _accept_parts(db, *, entity_id: str, user_id: str = "", draft_phase=None, **kwargs):
    """Accept each named part's candidate in order; stop at the first that fails.

    Each part is checked again and written in its own short locked transaction;
    a part that writes through the lead's tools (``PartDesigner.apply``) runs
    them, and any long work they do holds no lock.
    """
    from packages.core.services.workspace_build_spec import set_progress
    from packages.core.services.workspace_part_designers import accept_candidate, part_designers, pending_candidates

    parts = list(dict.fromkeys(str(part) for part in kwargs.get("parts") or []))
    if not parts:
        return _err("parts is required: name the parts whose candidates to accept")
    phase = draft_phase or contextlib.nullcontext
    draft_id = kwargs.get("draft_id", "")
    accepted: dict[str, Any] = {}
    failed = None
    for part in parts:
        designer = part_designers().get(part)
        if designer is not None and designer.apply is not None:
            async with phase():
                draft = await _load_draft(db, draft_id, entity_id, user_id)
                if draft is None:
                    return _err("draft not found")
                record = pending_candidates(dict(draft.fields or {})).get(part)
            if record is None:
                failed = {"part": part, "error": f"no {designer.title} design is waiting; command one with "
                                                 "ws_design_parts"}
                break
            summary, error = await designer.apply(db, record, entity_id=entity_id, user_id=user_id,
                                                  draft_id=draft_id, draft_phase=phase)
            if error is not None:
                failed = {"part": part, **error}
                break
        else:
            async with phase():
                draft = await _load_draft(db, draft_id, entity_id, user_id)
                if draft is None:
                    return _err("draft not found")
                fields = dict(draft.fields or {})
                # Checked against the Draft as the parts before it left it.
                summary, error = accept_candidate(fields, part)
                if error is not None:
                    failed = {"part": part, **error}
                    break
                draft.fields = fields
                await _persist(db, draft)
        async with phase():
            draft = await _load_draft(db, draft_id, entity_id, user_id)
            if draft is not None:
                fields = dict(draft.fields or {})
                set_progress(fields, part, "accepted")
                draft.fields = fields
                await _persist(db, draft)
        accepted[part] = summary
    if failed is not None:
        return _err(f"{failed['part']} was not accepted: {failed['error']}", accepted=accepted, failed=failed,
                    not_tried=parts[len(accepted) + 1:])
    return _ok({"accepted": accepted})


async def _commit_build_spec(db, *, entity_id: str, user_id: str = "", **kwargs):
    """Check the lead's build spec and keep it; dirty parts are designed again."""
    from packages.core.services.workspace_build_spec import (
        MULTI_STAGE_STAGES,
        SOLO_MAX_WORK_UNITS,
        commit_spec,
        next_wave,
        validate_build_spec,
        work_units,
    )
    from packages.core.services.workspace_part_designers import designer_dependencies

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    fields = dict(draft.fields or {})
    proposed = {key: value for key, value in kwargs.items() if key != "draft_id" and not key.startswith("_")}
    spec, errors = validate_build_spec(proposed, fields)
    if errors:
        return _err("the build spec has problems; fix them and commit it again", errors=errors)
    stored = commit_spec(fields, spec, designer_dependencies())
    draft.fields = fields
    await _persist(db, draft)
    if stored["mode"] == "solo":
        return _ok({"mode": "solo", "work_units": work_units(stored), "next": (
            f"Small Workspace (at most {SOLO_MAX_WORK_UNITS} Agents, Workflows, Automations and Knowledge nets, "
            f"none with {MULTI_STAGE_STAGES}+ stages): build them yourself with the write tools, then command the "
            "App with ws_design_app_view.")})
    wave = next_wave(stored, designer_dependencies())
    return _ok({"mode": "team", "progress": stored["progress"], "redesign": stored["dirty"], "next_wave": wave,
                "next": f"Call ws_design_parts to design {', '.join(wave) or 'nothing'}." if wave
                else "Every part is designed; ws_lint_draft next."})


async def _design_parts(db, *, entity_id: str, user_id: str = "", draft_phase=None, **kwargs):
    """Run one wave of part designers at once and keep each candidate.

    ``draft_phase`` (from an Architect turn) opens a short locked transaction;
    the designers run between the read and the save, holding none.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.workspace_build_spec import (
        DONE,
        MAX_SENDBACKS,
        SPEC_PARTS,
        build_spec,
        count_sendback,
        next_wave,
        set_progress,
        waiting_on,
    )
    from packages.core.services.workspace_part_designers import (
        DesignJob,
        candidate_reply,
        design_wave,
        designer_dependencies,
        keep_candidate,
        note_design_failure,
        part_designers,
        pending_candidates,
    )

    phase = draft_phase or contextlib.nullcontext
    draft_id = kwargs.get("draft_id", "")
    asked = {str(row.get("part")): row for row in kwargs.get("parts") or [] if isinstance(row, dict) and row.get("part")}
    deps = designer_dependencies()
    results: dict[str, Any] = {}
    jobs: list = []
    async with phase():
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")
        fields = dict(draft.fields or {})
        spec = build_spec(fields)
        if spec is None:
            return _err("commit the build spec with ws_commit_build_spec first")
        if spec.get("mode") == "solo":
            return _refused({"error": "this Workspace is built in solo mode: build its parts yourself with the "
                                      "write tools, and the App with ws_design_app_view"})
        unknown = [part for part in asked if part not in spec.get("progress", {})]
        if unknown:
            return _refused({"error": f"the build spec plans no {unknown}; plan them in ws_commit_build_spec",
                             "planned": sorted(spec.get("progress", {}))})
        progress = spec.get("progress") or {}
        # A part whose candidate waits for review is designed again only when named.
        wave = list(asked) or [part for part in next_wave(spec, deps) if progress.get(part) == "pending"]
        waiting = [part for part, state in progress.items() if state == "candidate"]
        if not wave and waiting:
            return _ok({"parts": {}, "progress": progress, "next": (
                f"Review the waiting {waiting}: accept with ws_accept_parts, or name a part with feedback to "
                "send it back.")})
        for part in wave:
            blocked_by = waiting_on(spec, part, deps)
            if blocked_by:
                results[part] = {"status": "waiting", "next": f"accept {blocked_by} first"}
                continue
            designer = part_designers()[part]
            row = asked.get(part, {})
            feedback = str(row.get("feedback") or "").strip() or None
            if feedback and int((spec.get("sendbacks") or {}).get(part) or 0) >= MAX_SENDBACKS:
                set_progress(fields, part, "lead")
                results[part] = {"status": "lead", "next": (
                    f"It was sent back {MAX_SENDBACKS} times. If the person must decide, ask with ws_ask_user; "
                    f"otherwise {designer.fallback or 'build it yourself'} Never drop what the person asked for.")}
                continue
            context = designer.context_from_draft(fields)
            blocked = designer.blockers(context)
            if blocked:
                results[part] = {"status": "blocked", **blocked}
                continue
            if not designer.needed(context):
                set_progress(fields, part, "skipped")
                results[part] = {"status": "skipped", "next": "nothing to design for it"}
                continue
            brief = str(row.get("brief") or (spec.get("briefs") or {}).get(part) or "").strip()
            if not brief:
                results[part] = {"status": "blocked", "error": f"give it a brief: {designer.brief_hint}"}
                continue
            if feedback:
                count_sendback(fields, part)
            sent_back = pending_candidates(fields).get(part) if feedback else None
            jobs.append(DesignJob(designer, context, brief, feedback, sent_back))
        draft.fields = fields
        await _persist(db, draft)
    outcomes = await design_wave(jobs, entity_id=entity_id, user_id=user_id or None, workspace_id=None,
                                 surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT, keepalive=_draft_keepalive(draft_phase))
    async with phase():
        # Re-read: the person or the Architect may have edited the Draft meanwhile.
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")
        fields = dict(draft.fields or {})
        for outcome in outcomes:
            designer = outcome.job.designer
            part = designer.part
            if outcome.error is not None:
                # Technical: reported to operators; the person hears nothing of it.
                note_design_failure(fields, part)
                set_progress(fields, part, "lead")
                results[part] = {"status": "lead", "next": (
                    f"{designer.fallback or 'Build it yourself.'} Do not mention this to the person.")}
                continue
            result = outcome.result
            if result.candidate is None:
                note_design_failure(fields, part)
                results[part] = {"status": "no_design", "design_notes": result.notes, "attempts": result.attempts,
                                 "next": f"Send it back with feedback, or: {designer.fallback or 'build it yourself'}"}
                continue
            _, errors, _ = designer.check(designer.context_from_draft(fields), result.candidate)
            if errors:
                results[part] = {"status": "stale", "errors": errors,
                                 "next": "The Draft changed while it was designed; command it again."}
                continue
            keep_candidate(fields, part, {"value": result.candidate, "brief": outcome.job.brief, **result.details,
                                          "warnings": result.warnings, "design_notes": result.notes})
            set_progress(fields, part, "candidate")
            results[part] = {"status": "candidate", **candidate_reply(designer, result)}
        draft.fields = fields
        await _persist(db, draft)
        spec = build_spec(fields) or {}
    progress = spec.get("progress") or {}
    ready = [part for part, row in results.items() if row.get("status") == "candidate"]
    later = [part for part in progress if progress[part] not in DONE and part not in results]
    return _ok({
        "parts": {part: results[part] for part in SPEC_PARTS if part in results},
        "progress": progress,
        "next": (f"Review {ready}: accept with ws_accept_parts or send back with feedback. " if ready else "")
                + (f"Then ws_design_parts for {later}." if later else "Then ws_lint_draft."),
    })


async def _design_app_view(db, *, entity_id: str, user_id: str = "", **kwargs):
    kwargs.pop("part", None)
    return await _design_part(db, entity_id=entity_id, user_id=user_id, part="app", **kwargs)


async def _design_workflows(db, *, entity_id: str, user_id: str = "", **kwargs):
    kwargs.pop("part", None)
    return await _design_part(db, entity_id=entity_id, user_id=user_id, part="workflows", **kwargs)


async def _accept_app_view(db, *, entity_id: str, user_id: str = "", **kwargs):
    from packages.core.services.workspace_part_designers import accept_candidate

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    from packages.core.services.workspace_build_spec import set_progress

    fields = dict(draft.fields or {})
    summary, failed = accept_candidate(fields, "app")
    if failed is not None:
        return _refused(failed)
    set_progress(fields, "app", "accepted")
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"accepted": True, **summary})


class DraftWorkflowError(ValueError):
    """A proposed Workflow the Draft cannot keep, with what is wrong."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


def _review_asked_as_confirmation(steps: list[dict[str, Any]]) -> str | None:
    """Why a Confirm-only wait between an Agent's draft and its outside action is a review, if one is.

    Staging 2026-11-09, 5 of 6 drafts: "confirm this reply before it is sent"
    offered one Confirm, so the person could not send the reply back.
    """
    by_id = {str(step.get("id")): step for step in steps if step.get("id")}

    def config(step: dict[str, Any]) -> dict[str, Any]:
        return step.get("config") if isinstance(step.get("config"), dict) else {}

    def walk(start: str, links: dict[str, list[str]]) -> list[dict[str, Any]]:
        """The steps reached from ``start`` along ``links`` before another wait."""
        seen, queue, reached = {start}, list(links.get(start, [])), []
        while queue:
            step_id = queue.pop(0)
            step = by_id.get(step_id)
            if step_id in seen or step is None:
                continue
            seen.add(step_id)
            reached.append(step)
            if step.get("type") != "wait":
                queue.extend(links.get(step_id, []))
        return reached

    after = {step_id: [str(item) for item in step.get("next") or []] for step_id, step in by_id.items()}
    before: dict[str, list[str]] = {}
    for step_id, targets in after.items():
        for target in targets:
            before.setdefault(target, []).append(step_id)
    for step_id, step in by_id.items():
        if step.get("type") != "wait" or not workflow_wait_is_confirmation(config(step)):
            continue
        makers = [item for item in walk(step_id, before) if item.get("type") == "agent"]
        actors = [item for item in walk(step_id, after) if item.get("type") == "agent"
                  and "effects" in (config(item).get("expects") or [])]
        if makers and actors:
            maker = makers[0]["id"]
            return (f"wait {step_id} asks the person to confirm what {maker} made before {actors[0]['id']} acts on "
                    f"it: that is an approval, so they can send it back -- a message that asks to approve it, options "
                    f"['approve','revise'], approval_values ['approve'], revise_from '{maker}'")
    return None


#: Words in a Flow's slug or name that say it runs on a cadence.
_ROUTINE_WORDS = frozenset({"hourly", "daily", "nightly", "weekly", "biweekly", "monthly", "quarterly"})
_ROUTINE_NAME = r"每天|每日|每周|每月|每季度|日报|周报|月报"


#: Result fields by which a step says whether a person must approve its work.
_APPROVAL_FLAG = re.compile(r"\b(?:needs|requires|require)_(?:approval|review|confirmation)\b|\bapproval_required\b")


def _approval_that_resends(steps: list[dict[str, Any]]) -> str | None:
    """Why an approval could send or publish twice, if it could.

    Staging 2026-11-09, a candle shop reply Flow: the drafting step sent routine replies itself and
    returned needs_approval, yet every message waited for approval, and revise re-ran that step.
    """
    def config(step: dict[str, Any]) -> dict[str, Any]:
        return step.get("config") if isinstance(step.get("config"), dict) else {}

    by_id = {str(step.get("id")): step for step in steps if step.get("id")}
    waits = [step for step in steps if step.get("type") == "wait"
             and str(config(step).get("wait_type") or "approval") == "approval"]
    for wait in waits:
        redo = by_id.get(str(config(wait).get("revise_from") or ""))
        if redo is not None and "effects" in (config(redo).get("expects") or []):
            return (f"revise at {wait['id']} re-runs {redo['id']}, which acts outside (sends or publishes), so a "
                    f"revision could do it twice: revise from the step that drafts, and act only after the approval")
    if not waits:
        return None
    branched = " ".join(str(config(step).get("expression") or "") + json.dumps(step.get("cases") or config(step).get("cases") or [])
                        for step in steps if step.get("type") in {"condition", "switch"})
    for step in steps:
        if step.get("type") != "agent":
            continue
        flags = set(_APPROVAL_FLAG.findall(json.dumps(config(step), ensure_ascii=False)))
        if flags and not any(flag in branched for flag in flags):
            flag = sorted(flags)[0]
            return (f"{step['id']} decides whether a person must approve ({flag}), yet every run waits at "
                    f"{waits[0]['id']}: add a condition step on {{{{{step['id']}.{flag}}}}} before the wait, so the "
                    f"ones that need no approval skip it")
    return None


def draft_workflow(proposed: dict[str, Any], *, workflows: list[dict[str, Any]],
                   service_keys: set[str]) -> dict[str, Any]:
    """One portable Workflow as a Draft keeps it, checked as creation checks it.

    ``workflows`` are the Draft's others, which its sub-Flow steps may run.
    Raises ``DraftWorkflowError`` with what to fix.
    """
    slug = str(proposed.get("slug") or "").strip()
    if not _SLUG_RE.match(slug):
        raise DraftWorkflowError("workflow slug must be snake_case", got=slug)
    steps = copy.deepcopy(proposed.get("steps"))
    if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
        raise DraftWorkflowError("workflow steps must be a non-empty array of objects")
    if str(proposed.get("trigger_type") or "") == "schedule":
        # Creation binds no Workflow of this trigger, so it would never run.
        raise DraftWorkflowError("a Workflow runs on a clock through an Automation, not its trigger: omit "
                                 "trigger_type (or use manual) and schedule it with an Automation", got="schedule")
    review = _review_asked_as_confirmation(steps) or _approval_that_resends(steps)
    if review:
        raise DraftWorkflowError(review)
    for step in steps:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step.get("type") == "tool" and str(config.get("tool") or "") == "manor":
            raise DraftWorkflowError("use the native workflow_project node for project creation; tool/manor wrappers cannot declare App-readable project state")
        if step.get("type") == "workflow_project" and str(config.get("operation") or "") in {"create", "claim"}:
            schema = config.get("state_schema")
            if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                raise DraftWorkflowError("workflow_project create requires state_schema.properties for every App-readable state field")
        if step.get("type") == "wait" and workflow_wait_is_confirmation(config):
            # The person reports what they did themselves: one Confirm, nothing to send back.
            step["config"] = {
                **{key: value for key, value in config.items() if key not in {"revise_from", "approval_values"}},
                "options": workflow_wait_options(config),
            }
    workflow = {key: copy.deepcopy(proposed[key]) for key in (
        "slug", "name", "description", "run_inputs", "internal", "trigger_type", "binding_config",
        "variables", "output_schema", "enabled",
    ) if key in proposed}
    workflow.update(slug=slug, steps=steps)
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values
    from packages.core.blueprints.payload import _validate_workflows
    from packages.core.services.workflow_service import workflow_service_keys

    try:
        runtime_steps = _blueprint_workflow_definition_values(workflow)["steps"]
        # A sub-Flow step runs another Workflow of this Draft, by its slug.
        _validate_workflows({"workflows": [*(item for item in workflows if item.get("slug") != slug), workflow]})
    except Exception as exc:
        raise DraftWorkflowError(f"workflow is invalid: {exc}") from exc
    unknown = sorted(workflow_service_keys(runtime_steps) - service_keys)
    if unknown:
        raise DraftWorkflowError(
            "workflow agent steps must run services already in this draft; add them with ws_propose_service first",
            unknown_service_keys=unknown,
        )
    return workflow


async def _propose_workflow(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    fields = dict(draft.fields or {})
    others = [item for item in fields.get("workflows") or [] if isinstance(item, dict)]
    service_keys = {str((service or {}).get("service_key") or "") for service in fields.get("services") or []}
    try:
        workflow = draft_workflow(kwargs, workflows=others, service_keys=service_keys)
    except DraftWorkflowError as exc:
        return _err(str(exc), **exc.details)
    from packages.core.services.workflow_run_input_guidance import run_input_issues

    inputs = run_input_issues([*(item for item in others if item.get("slug") != workflow["slug"]), workflow],
                              checked={workflow["slug"]})
    if inputs:
        return _err("run inputs ask the person to type what they could upload or pick", issues=inputs)
    slug = workflow["slug"]
    fields["workflows"] = _replace_in_list(
        [item for item in fields.get("workflows") or [] if isinstance(item, dict)],
        "slug", slug, workflow,
    )
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"slug": slug, "internal": bool(workflow.get("internal"))})


async def _propose_record_collection(db, *, entity_id: str, user_id: str = "", **kwargs):
    """A kept-by-hand collection's add, change and take-off Flows, built by the record-collection pattern."""
    from packages.core.blueprints.workflow_patterns import record_collection_flows_from_args
    from packages.core.services.workflow_run_input_guidance import run_input_issues

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    if not _SLUG_RE.match(str(kwargs.get("slug") or "")):
        return _err("slug must be snake_case", got=kwargs.get("slug"))
    try:
        built = record_collection_flows_from_args(kwargs)["workflows"]
    except (KeyError, TypeError, ValueError) as exc:
        return _err(f"the collection could not be built: {exc}")
    fields = dict(draft.fields or {})
    service_keys = {str((service or {}).get("service_key") or "") for service in fields.get("services") or []}
    workflows = [item for item in fields.get("workflows") or [] if isinstance(item, dict)]
    for proposed in built:
        others = [item for item in workflows if item.get("slug") != proposed["slug"]]
        try:
            workflows = _replace_in_list(workflows, "slug", proposed["slug"],
                                         draft_workflow(proposed, workflows=others, service_keys=service_keys))
        except DraftWorkflowError as exc:
            return _err(f"{proposed['slug']}: {exc}", **exc.details)
    slugs = [proposed["slug"] for proposed in built]
    inputs = run_input_issues(workflows, checked=set(slugs))
    if inputs:
        return _err("run inputs ask the person to type what they could upload or pick", issues=inputs)
    fields["workflows"] = workflows
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"slugs": slugs, "item_project_type": str(kwargs.get("item_project_type") or "")})


async def _propose_source_watch(db, *, entity_id: str, user_id: str = "", **kwargs):
    """A watch that starts a Flow for each new thing in an account, built by the source-watch pattern."""
    from packages.core.blueprints.workflow_patterns import source_watch_flows_from_args

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    if not _SLUG_RE.match(str(kwargs.get("slug") or "")):
        return _err("slug must be snake_case", got=kwargs.get("slug"))
    try:
        built = source_watch_flows_from_args(kwargs)
    except (KeyError, TypeError, ValueError) as exc:
        return _err(f"the watch could not be built: {exc}")
    schedules = []
    for row in built["automations"]:
        request = {
            "automation_key": row["workflow"],
            "description": f"Run {row['workflow']}: {row['why']}.",
            "trigger": row["cron_expr"],
            "cron_expr": row["cron_expr"],
            "timezone": row["timezone"],
            "service_key": str(kwargs.get("service_key") or ""),
            "scheduled_job": {"job_id": row["workflow"],
                              "execution_target": {"type": "workflow", "workflow_slug": row["workflow"]}},
        }
        schedule, error = await _resolve_automation_schedule(db, user_id=user_id, kwargs=request)
        if error is not None:
            return error
        schedules.append((request, schedule))
    fields = dict(draft.fields or {})
    service_keys = {str((service or {}).get("service_key") or "") for service in fields.get("services") or []}
    workflows = [item for item in fields.get("workflows") or [] if isinstance(item, dict)]
    # The internal one-item Flow first: the watch runs it.
    for proposed in reversed(built["workflows"]):
        others = [item for item in workflows if item.get("slug") != proposed["slug"]]
        try:
            workflows = _replace_in_list(workflows, "slug", proposed["slug"],
                                         draft_workflow(proposed, workflows=others, service_keys=service_keys))
        except DraftWorkflowError as exc:
            return _err(f"{proposed['slug']}: {exc}", **exc.details)
    fields["workflows"] = workflows
    scheduled = []
    for request, schedule in schedules:
        error = _stage_automation(fields, schedule=schedule, kwargs=request)
        if error is not None:
            return error
        scheduled.append({"automation_key": request["automation_key"], "schedule": schedule})
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"slugs": [row["slug"] for row in built["workflows"]], "automations": scheduled})


async def _propose_stat(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    key = str(kwargs.get("key") or "").strip()
    if not key:
        return _err("stat key is required")
    stat = {field: copy.deepcopy(kwargs[field]) for field in (
        "key", "name", "library_key", "description", "value_type", "unit", "window", "collector_type", "collector_config", "collection_cadence", "goal_eligible"
    ) if field in kwargs}
    stat["key"] = key
    try:
        # Reuse the native Stat definition validator, rather than accepting a
        # dashboard-only shape that fails only after Workspace creation.
        resolve_draft_goal_measurements({"stats": [stat], "goals": []})
    except ValueError as exc:
        return _err(str(exc))
    fields = dict(draft.fields or {})
    fields["stats"] = _replace_in_list(
        [item for item in fields.get("stats") or [] if isinstance(item, dict)],
        "key", key, stat,
    )
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"key": key})


def _validate_draft_resources(fields: dict[str, Any]) -> None:
    """Apply portable Blueprint validation before Draft readiness/finalize."""
    from packages.core.blueprints.installer import validate_portable_recipe_resources
    recipe = {"workflows": fields.get("workflows") or [], "scheduled_jobs": fields.get("scheduled_jobs") or []}
    try:
        validate_portable_recipe_resources(recipe)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


# ── ws_propose_automation ───────────────────────────────────────────────────

async def _propose_automation(db, *, entity_id: str, user_id: str = "", **kwargs):
    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    automation_key = kwargs.get("automation_key", "")
    if not _SLUG_RE.match(automation_key):
        return _err("automation_key must be snake_case", got=automation_key)
    schedule, schedule_error = await _resolve_automation_schedule(
        db, user_id=user_id, kwargs=kwargs,
    )
    if schedule_error is not None:
        return schedule_error
    fields = dict(draft.fields or {})
    error = _stage_automation(fields, schedule=schedule, kwargs=kwargs)
    if error is not None:
        return error
    _reconcile_removed_channel_references(fields)
    draft.fields = fields
    await _persist(db, draft)
    return _ok({"automation_key": automation_key, "schedule": schedule})


def _stage_automation(fields: dict, *, schedule: dict, kwargs: dict) -> str | None:
    """Stage a validated schedule in a local copy of the draft fields."""
    automation_key = kwargs["automation_key"]
    automation = {
        "automation_key": automation_key,
        "description": kwargs.get("description", ""),
        "trigger": kwargs.get("trigger", ""),
        "service_key": kwargs.get("service_key", ""),
    }
    automation.update(schedule)
    for removed_channel in fields.get("_removed_channels") or []:
        automation["description"] = _scrub_removed_channel_text(automation.get("description"), _channel_key(removed_channel))
        automation["trigger"] = _scrub_removed_channel_text(automation.get("trigger"), _channel_key(removed_channel))
    existing = [
        item for item in (fields.get("automations") or [])
        if _automation_signature(item) != _automation_signature(automation)
    ]
    fields["automations"] = _replace_in_list(existing, "automation_key", automation_key, automation)
    scheduled_job = kwargs.get("scheduled_job")
    if scheduled_job is not None:
        if not isinstance(scheduled_job, dict):
            return _err("scheduled_job must be an object")
        job_id = str(scheduled_job.get("job_id") or automation_key).strip()
        if not _SLUG_RE.match(job_id):
            return _err("scheduled_job.job_id must be snake_case")
        from packages.core.blueprints.installer import validate_portable_recipe_resources

        native_job = _native_scheduled_job(scheduled_job, job_id, schedule, automation["service_key"])
        try:
            validate_portable_recipe_resources({
                "workflows": [item for item in fields.get("workflows") or [] if isinstance(item, dict)],
                "scheduled_jobs": [native_job],
            })
        except Exception as exc:
            return _err(
                f"scheduled_job is invalid: {exc}. A workflow target must name a workflow already "
                "proposed with ws_propose_workflow; otherwise omit scheduled_job and the automation "
                "runs its service's agent.",
                code="invalid_scheduled_job",
            )
        fields["scheduled_jobs"] = _replace_in_list(
            [item for item in fields.get("scheduled_jobs") or [] if isinstance(item, dict)],
            "job_id", job_id, native_job,
        )
        # The job may be named differently; finalize reads this to create only the job.
        fields["automations"] = _replace_in_list(
            fields["automations"], "automation_key", automation_key, {**automation, "job_id": job_id},
        )
    return None


def _native_scheduled_job(declared: dict, job_id: str, schedule: dict, service_key: str) -> dict:
    """The declared native Job, completed with the automation's resolved clock and target."""
    job = copy.deepcopy(declared)
    job["job_id"] = job_id
    if not str(job.get("name") or "").strip():
        job["name"] = job_id.replace("_", " ").strip().capitalize()
    job.pop("schedule", None)
    if not job.get("schedule_kind") and job.get("job_type") != "manual":
        for key in ("schedule_kind", "cron_expr", "every_seconds"):
            if key in schedule:
                job[key] = schedule[key]
        job.setdefault("job_type", "cron" if schedule["schedule_kind"] == "cron" else "interval")
    job.setdefault("timezone", schedule["timezone"])
    target = dict(job.get("execution_target") or {})
    if not job.get("execution_type"):
        job["execution_type"] = "workflow" if target.get("workflow_slug") else "agent"
    if job["execution_type"] in {"agent", "agent_message"} and service_key:
        target.setdefault("service_key", service_key)
    job["execution_target"] = target
    return job


async def _resolve_automation_schedule(db, *, user_id: str, kwargs: dict) -> tuple[dict, str | None]:
    """Exact scheduler fields for a proposed automation, or a tool error.

    An unreadable cadence is an error back to the architect, never a daily
    guess, and an event trigger is refused so the user hears it cannot run
    on a clock instead of getting a schedule they did not ask for.
    """
    from packages.core.schedule_cadence import (
        automation_cadence,
        automation_timezone,
        looks_like_event_trigger,
    )

    request = {
        key: kwargs.get(key)
        for key in ("trigger", "cron_expr", "every_seconds", "timezone")
        if kwargs.get(key) not in (None, "")
    }
    try:
        cadence = automation_cadence(request)
    except (TypeError, ValueError) as exc:
        return {}, _err(f"invalid schedule: {exc}", code="invalid_automation_schedule")
    if cadence is None:
        trigger = str(kwargs.get("trigger") or "")
        if looks_like_event_trigger(trigger):
            return {}, _err(
                "Automations run on a clock; this trigger is an event. Messages arriving on a "
                "connected channel are already answered by that service's agent, so no automation "
                "is needed for them. For any other event, tell the user it cannot start work "
                "automatically yet and offer a schedule instead.",
                code="event_trigger_unsupported",
                trigger=trigger,
            )
        return {}, _err(
            "Could not read a schedule from trigger. Pass cron_expr (5 fields, 0=Sunday) or "
            "every_seconds. Cadences such as every two weeks or the last day of the month have "
            "no cron form: confirm a supported cadence with the user.",
            code="invalid_automation_schedule",
            trigger=str(kwargs.get("trigger") or ""),
        )
    default_timezone = "UTC"
    if user_id:
        from sqlalchemy import select

        from packages.core.models.user import User

        default_timezone = str(await db.scalar(select(User.timezone).where(User.id == user_id)) or "UTC")
    try:
        timezone_name = automation_timezone(request, default=default_timezone)
    except ValueError as exc:
        return {}, _err(str(exc), code="invalid_automation_timezone")
    schedule: dict[str, Any] = {"schedule_kind": cadence.schedule_kind, "timezone": timezone_name}
    if cadence.cron_expr:
        schedule["cron_expr"] = cadence.cron_expr
    else:
        schedule["every_seconds"] = cadence.every_seconds
    return schedule, None


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
        from packages.core.services.workspace_setup_service import EXPLICIT_HARD_BLOCK_RULES_FIELD

        confirmations = dict(fields.get(EXPLICIT_HARD_BLOCK_RULES_FIELD) or {})
        confirmations.pop(key, None)
        if confirmations:
            fields[EXPLICIT_HARD_BLOCK_RULES_FIELD] = confirmations
        else:
            fields.pop(EXPLICIT_HARD_BLOCK_RULES_FIELD, None)
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

# Catalog fields a need is matched against, with the weight of a hit in each.
# The Blueprint's identity (title, tags, when to use it, what it delivers)
# outweighs its long-form description.
_BLUEPRINT_MATCH_FIELDS = (
    ("title", 3.0),
    ("tags", 3.0),
    ("use_when", 2.0),
    ("outcome_summary", 2.0),
    ("summary", 2.0),
    ("description", 1.0),
)
_BLUEPRINT_IDENTITY_FIELDS = frozenset({"title", "tags", "use_when", "outcome_summary", "summary"})
_BLUEPRINT_MATCH_STOPWORDS = frozenset("""
    about across after all also and any are before being between both but can
    could did does done each every for from get give gives had has have help
    helps her here his how into its just keep keeps let lets like made make
    makes manor many may might more most much must need needs not one only
    other our out over own per run runs set sets she should some start starts
    such than that the their them then there these they this those through
    under use used user users using very via want wants was were what when
    where which while who why will with within without would you your
    workspace workspaces
""".split())
_CJK_RUN_RE = re.compile(r"[㐀-鿿豈-﫿]+")
_LATIN_WORD_RE = re.compile(r"[a-z0-9]+")
# idf-weighted share of the need that a Blueprint covers.
_BLUEPRINT_MATCH_HIGH = 0.35
_BLUEPRINT_MATCH_MEDIUM = 0.2
_BLUEPRINT_MATCH_LEVELS = ("high", "medium", "low", "none")


def _blueprint_match_stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _blueprint_match_terms(text: str) -> Dict[str, str]:
    """Map each comparable term in ``text`` to the first word it came from.

    Latin words are lowercased and lightly stemmed; CJK text, which has no
    word boundaries, is compared by character bigrams.
    """
    lowered = str(text or "").lower()
    out: Dict[str, str] = {}
    for word in _LATIN_WORD_RE.findall(lowered):
        if len(word) < 3 or word.isdigit() or word in _BLUEPRINT_MATCH_STOPWORDS:
            continue
        out.setdefault(_blueprint_match_stem(word), word)
    for run in _CJK_RUN_RE.findall(lowered):
        for index in range(len(run) - 1):
            out.setdefault(run[index:index + 2], run[index:index + 2])
    return out


def _blueprint_catalog_text(bp, manifest: Dict[str, Any], field: str) -> str:
    """One catalog field's text, including its translated overlays."""
    if field == "tags":
        values: List[Any] = [str(tag).replace("-", " ") for tag in bp.tags or manifest.get("tags") or []]
    else:
        values = [getattr(bp, field, None) or manifest.get(field)]
    for overlay in (manifest.get("i18n") or {}).values():
        if isinstance(overlay, dict):
            value = overlay.get(field)
            values.extend(value if isinstance(value, list) else [value])
    return " ".join(str(value) for value in values if value)


def _draft_need_text(draft) -> str:
    fields = dict(getattr(draft, "fields", None) or {})
    parts = [fields.get(key) for key in ("name", "kind", "operating_context", "primary_work")]
    for service in fields.get("services") or []:
        if isinstance(service, dict):
            parts.extend([service.get("name"), service.get("description")])
    return " ".join(str(part) for part in parts if isinstance(part, str) and part.strip())


def _rank_blueprints(need_text: str, blueprints: List[Any]) -> List[Dict[str, Any]]:
    """Order published Blueprints by how much of the need each one covers.

    The score is the idf-weighted share of the need's terms found in a
    Blueprint's catalog text, so words most Blueprints share count little.
    The level is a hint for the architect, who still judges the fit.
    """
    import math

    need = _blueprint_match_terms(need_text)
    catalog = []
    document_frequency: Dict[str, int] = {}
    for bp in blueprints:
        manifest = dict((bp.payload or {}).get("manifest") or {}) if isinstance(bp.payload, dict) else {}
        field_terms = {
            field: set(_blueprint_match_terms(_blueprint_catalog_text(bp, manifest, field)))
            for field, _ in _BLUEPRINT_MATCH_FIELDS
        }
        for term in set().union(*field_terms.values()):
            document_frequency[term] = document_frequency.get(term, 0) + 1
        catalog.append((bp, manifest, field_terms))

    total = max(len(catalog), 1)

    def idf(term: str) -> float:
        # A term no Blueprint mentions still counts toward the need it leaves uncovered.
        return math.log(1 + total / document_frequency.get(term, 0.5))

    need_weight = sum(idf(term) for term in need) or 1.0
    ranked = []
    for bp, manifest, field_terms in catalog:
        matched: Dict[str, List[str]] = {}
        covered = 0.0
        identity_hits = 0
        for term, word in need.items():
            field = next((name for name, _ in _BLUEPRINT_MATCH_FIELDS if term in field_terms[name]), None)
            if field is None:
                continue
            matched.setdefault(field, []).append(word)
            covered += idf(term)
            identity_hits += field in _BLUEPRINT_IDENTITY_FIELDS
        score = covered / need_weight
        if score >= _BLUEPRINT_MATCH_HIGH and identity_hits >= 4:
            level = "high"
        elif score >= _BLUEPRINT_MATCH_MEDIUM:
            level = "medium"
        else:
            level = "low" if matched else "none"
        reasons = [
            f"{field.replace('_', ' ')} matches: {', '.join(sorted(matched[field]))}"
            for field, _ in _BLUEPRINT_MATCH_FIELDS
            if field in matched
        ]
        row = {
            "id": bp.id,
            "title": bp.title,
            "summary": bp.summary,
            "tags": list(bp.tags or []),
            "use_when": str(manifest.get("use_when") or "")[:400] or None,
            "outcome_summary": str(manifest.get("outcome_summary") or "")[:400] or None,
            "install_count": int(bp.install_count or 0),
            "match": {"level": level, "score": round(score, 2), "reasons": reasons},
        }
        if bp.price_cents:
            row["price"] = {"cents": int(bp.price_cents), "currency": bp.currency}
        ranked.append(row)
    ranked.sort(key=lambda row: (
        _BLUEPRINT_MATCH_LEVELS.index(row["match"]["level"]),
        -row["match"]["score"],
        -row["install_count"],
    ))
    return ranked


async def _search_blueprints(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import select
    from packages.core.models.blueprint import WorkspaceBlueprint

    draft = None
    draft_id = kwargs.get("draft_id", "")
    if draft_id:
        draft = await _load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return _err("draft not found")

    query = str(kwargs.get("query") or "").strip()
    need_text = " ".join(part for part in (query, _draft_need_text(draft) if draft else "") if part)
    if not need_text:
        return _err(
            "describe the need: pass query with the user's domain, outcomes, channels and deliverables",
        )

    stmt = (
        select(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED)
        .order_by(WorkspaceBlueprint.install_count.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    out = _rank_blueprints(need_text, list(rows))
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


async def _apply_blueprint(db, *, entity_id: str, user_id: str = "", **kwargs):
    from packages.core.services.workspace_draft_service import apply_blueprint

    draft_id = str(kwargs.get("draft_id") or "").strip()
    blueprint_id = str(kwargs.get("blueprint_id") or "").strip()
    try:
        draft = await apply_blueprint(
            db,
            draft_id=draft_id,
            entity_id=entity_id,
            user_id=user_id,
            blueprint_id=blueprint_id,
        )
    except PermissionError as exc:
        return _err(str(exc), blueprint_id=blueprint_id, purchase_required=True)
    except ValueError as exc:
        return _err(str(exc), blueprint_id=blueprint_id)
    fields = dict(draft.fields or {})
    return _ok({
        "blueprint_id": draft.applied_blueprint_id,
        "name": fields.get("name"),
        "services": [
            str(service.get("name") or service.get("service_key") or "")
            for service in fields.get("services") or []
            if isinstance(service, dict)
        ],
        "workflows": [
            str(workflow.get("name") or workflow.get("slug") or "")
            for workflow in fields.get("workflows") or []
            if isinstance(workflow, dict)
        ],
        "missing": list(draft.missing or []),
    })


# ── ws_get_draft ────────────────────────────────────────────────────────────

async def _read_source(db, *, entity_id: str, user_id: str = "", **kwargs):
    """Page through one draft source's extracted text (read-only)."""
    from packages.core.services import workspace_draft_sources as sources

    draft = await _load_draft(db, kwargs.get("draft_id", ""), entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    source_id = str(kwargs.get("source_id") or "").strip()
    source = sources.find_source(draft.fields, source_id)
    if source is None:
        return _err(
            "unknown source_id",
            source_id=source_id,
            known=[str(s.get("id")) for s in sources.draft_sources(draft.fields)],
        )
    if kwargs.get("refresh") and source.get("kind") == "link":
        refreshed = await sources.refresh_link_source(draft, source_id)
        if refreshed is not None:
            source = refreshed
            await _persist(db, draft)
    if source.get("status") != "ready":
        return _err(
            "source is not readable",
            source_id=source_id,
            status=source.get("status"),
            reason=source.get("error"),
        )
    text = str(source.get("text") or "")
    offset = max(0, int(kwargs.get("offset") or 0))
    max_chars = min(40_000, max(1_000, int(kwargs.get("max_chars") or 12_000)))
    content = text[offset:offset + max_chars]
    end = offset + len(content)
    return _ok({
        "source_id": source_id,
        "name": source.get("name"),
        "kind": source.get("kind"),
        "status": "ready",
        "total_chars": len(text),
        "offset": offset,
        "content": content,
        "next_offset": end if end < len(text) else None,
        "truncated": bool(source.get("truncated")),
    })


async def _get_draft(db, *, entity_id: str, user_id: str = "", **kwargs):
    from packages.core.services.workspace_draft_sources import strip_source_text

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")
    fields = dict(draft.fields or {})
    workflows = [item for item in fields.get("workflows") or [] if isinstance(item, dict)]
    stats = [item for item in fields.get("stats") or [] if isinstance(item, dict)]
    producers = []
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            config = step.get("config") if isinstance(step, dict) else None
            if not isinstance(config, dict) or str(step.get("type") or step.get("kind") or "") != "workflow_project":
                continue
            schema = config.get("state_schema") if isinstance(config.get("state_schema"), dict) else {}
            producers.append({
                "workflow_slug": workflow.get("slug"), "project_type": config.get("project_type"),
                "schema_version": config.get("schema_version"),
                "step_id": step.get("id"),
                "state_fields": sorted((schema.get("properties") or {}).keys()),
            })
    return _ok({
        "draft": {
            "id": draft.id,
            "status": draft.status,
            "ready": bool(draft.ready),
            "missing": list(draft.missing or []),
            "suggested_blueprint_id": draft.suggested_blueprint_id,
            "applied_blueprint_id": draft.applied_blueprint_id,
            # Source text is paged through ws_read_source; never dump it here.
            "fields": strip_source_text(fields),
        },
        "resource_catalog": {
            "workflow_definitions": [str(item.get("slug")) for item in workflows if item.get("slug")],
            "workflow_bindings": [str(item.get("slug")) for item in workflows if item.get("slug") and not item.get("internal")],
            "stats": [str(item.get("key") or item.get("library_key")) for item in stats if item.get("key") or item.get("library_key")],
            "scheduled_jobs": [str(item.get("job_id")) for item in fields.get("scheduled_jobs") or [] if isinstance(item, dict) and item.get("job_id")],
            "project_producers": producers,
        },
    })


# ── ws_lint_draft ───────────────────────────────────────────────────────────

async def _binding_vocabulary(
    db,
    *,
    entity_id: str,
    user_id: str,
    tool_refs: set[str],
    skill_refs: set[str],
    mcp_refs: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Resolve which referenced tool / skill / mcp refs actually exist for this actor.

    Shared by ws_lint_draft (report) and ws_request_custom_agent (reject at write
    time) so the two can never disagree about what a valid binding is.
    """
    from sqlalchemy import and_, or_, select

    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
    from packages.core.models.mcp import MCPServer
    from packages.core.models.skill import Skill
    from packages.core.models.workspace import ToolDefinition
    from packages.core.services.integration_resolution import bindable_mcp_provider_keys
    from packages.core.services.provider_keys import canonical_provider_key

    tool_rows = set((await db.execute(
        select(ToolDefinition.name).where(
            ToolDefinition.name.in_(tool_refs),
            ToolDefinition.status == "active",
        )
    )).scalars().all()) if tool_refs else set()
    available_tool_refs = tool_rows | set(
        runtime_registered_tool_names(include_undiscoverable=True)
    )

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
    available_skill_refs = {
        ref
        for skill in skill_rows
        if skill.entity_id is None or skill.id in readable_skill_ids
        for ref in (skill.id, skill.slug)
        if ref
    }

    mcp_rows = list((await db.execute(
        select(MCPServer).where(
            MCPServer.status == "active",
            (MCPServer.id.in_(mcp_refs)) | (MCPServer.server_key.in_(mcp_refs)),
        )
    )).scalars().all()) if mcp_refs else []
    # Validation asks whether the binding can run, not whether it is an
    # integration the user connects: internal bridges are bindable too.
    bindable_provider_keys = await bindable_mcp_provider_keys(db)
    available_mcp_refs = {
        ref
        for server in mcp_rows
        if canonical_provider_key(server.server_key) in bindable_provider_keys
        for ref in (server.id, server.server_key)
        if ref
    }
    return available_tool_refs, available_skill_refs, available_mcp_refs


def _custom_agent_runtime_capability_ids(
    custom: Dict[str, Any],
    *,
    mcp_rows: List[Any],
) -> frozenset[str]:
    """Project a draft Agent's configured runtime surface without granting it.

    Keep this aligned with Planner capability attribution: native tool names,
    configured Skill invocation, and exact MCP actions are inputs to the same
    canonical catalog used after provisioning. A server wildcard with no
    discoverable action catalog cannot prove that a requirement is covered.
    """
    from packages.core.ai.runtime.planning import (
        runtime_planner_action_specs_from_tools_cached,
        runtime_planner_capability_catalog,
    )
    from packages.core.services.agent_permission_service import parse_mcp_tool_name
    from packages.core.services.agent_provisioning_service import (
        spec_from_create_agent_draft,
    )
    from packages.core.services.provider_keys import canonical_provider_key

    spec = spec_from_create_agent_draft(custom)
    platform_tools: list[str] = []
    provider_actions: dict[str, list[str]] = {}
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] = {}

    def add_action(provider: object, action: object) -> None:
        provider_key = canonical_provider_key(str(provider or "").strip())
        action_key = str(action or "").strip()
        if not provider_key or not action_key:
            return
        actions = provider_actions.setdefault(provider_key, [])
        if action_key not in actions:
            actions.append(action_key)

    for tool_name in spec.tool_bindings:
        parsed = parse_mcp_tool_name(tool_name)
        if parsed is None:
            platform_tools.append(tool_name)
        else:
            add_action(*parsed)

    requested_servers = {
        str(ref).strip() for ref in spec.mcp_bindings if str(ref or "").strip()
    }
    for server in mcp_rows:
        if not requested_servers.intersection({
            str(getattr(server, "id", "") or "").strip(),
            str(getattr(server, "server_key", "") or "").strip(),
        }):
            continue
        server_key = str(getattr(server, "server_key", "") or "").strip()
        provider = canonical_provider_key(server_key)
        specs = runtime_planner_action_specs_from_tools_cached(
            getattr(server, "tools_cached", None) or []
        )
        provider_action_specs.setdefault(provider, {}).update(specs)
        configured = (
            spec.mcp_allowed_tools[server_key]
            if server_key in spec.mcp_allowed_tools
            else getattr(server, "default_allowed_tools", None)
        )
        selected_actions = list(specs) if configured is None else list(configured)
        for raw_action in selected_actions:
            parsed = parse_mcp_tool_name(str(raw_action or ""))
            if parsed is None:
                add_action(provider, raw_action)
                continue
            action_provider, action = parsed
            if canonical_provider_key(action_provider) == provider:
                add_action(provider, action)

    has_skills = bool(
        spec.skill_bindings
        or spec.skill_binding_refs
        or spec.missing_skill_specs
    )
    entries = runtime_planner_capability_catalog(
        provider_actions=provider_actions,
        provider_action_specs=provider_action_specs,
        platform_tools=platform_tools,
        skills=({"name": "configured Skill"},) if has_skills else (),
    )
    return frozenset(entry.capability_id for entry in entries)


def workflow_process_issues(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Hand-offs and human reviews the draft declares must be run by a Workflow.

    A service's ``handoff_to`` is only real when one Workflow runs both roles'
    agent steps with the hand-off in between, and ``human_review`` only when
    an approval wait follows that service's step. Graph checks only: each
    Workflow's own validity is reported by the shared validator.
    """
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    services = [service for service in fields.get("services") or [] if isinstance(service, dict)]
    service_keys = {str(service.get("service_key") or "") for service in services}
    graphs: list[tuple[str, list[dict]]] = []
    for workflow in fields.get("workflows") or []:
        if not isinstance(workflow, dict):
            continue
        try:
            graphs.append((str(workflow.get("slug") or ""), _blueprint_workflow_definition_values(workflow)["steps"]))
        except Exception:
            continue

    def downstream(steps: list[dict], start: str) -> set[str]:
        edges = {
            str(step.get("id")): [
                *(step.get("next") or []), *(step.get("true_next") or []), *(step.get("false_next") or []),
            ]
            for step in steps
        }
        seen: set[str] = set()
        pending = list(edges.get(start, []))
        while pending:
            current = pending.pop()
            if isinstance(current, str) and current and current not in seen:
                seen.add(current)
                pending.extend(edges.get(current, []))
        return seen

    def service_steps(steps: list[dict], service_key: str) -> list[str]:
        return [
            str(step.get("id")) for step in steps
            if step.get("type") == "agent"
            and str((step.get("config") or {}).get("service_key") or "") == service_key
        ]

    issues: List[Dict[str, Any]] = []
    for slug, steps in graphs:
        for step in steps:
            key = str((step.get("config") or {}).get("service_key") or "")
            if step.get("type") == "agent" and key and key not in service_keys:
                issues.append({
                    "severity": "P0",
                    "where": f"workflows.{slug}.{step.get('id')}",
                    "message": f"Workflow step runs service '{key}', which this draft does not define.",
                })
    for service in services:
        key = str(service.get("service_key") or "")
        for target in service.get("handoff_to") or []:
            if target not in service_keys:
                issues.append({
                    "severity": "P0",
                    "where": f"services.{key}.handoff_to",
                    "message": f"service '{key}' hands off to '{target}', which this draft does not define.",
                })
                continue
            if not any(
                any(
                    set(service_steps(steps, target)) & downstream(steps, source)
                    for source in service_steps(steps, key)
                )
                for _slug, steps in graphs
            ):
                issues.append({
                    "severity": "P0",
                    "where": f"services.{key}.handoff_to",
                    "message": (
                        f"service '{key}' hands its result to '{target}', but no Workflow runs that hand-off. "
                        "Call ws_propose_workflow with one agent step per service (config.service_key), "
                        f"routing '{key}' to '{target}' with next."
                    ),
                })
        if service.get("human_review") is True and not any(
            any(
                any(
                    step.get("type") == "wait"
                    and str((step.get("config") or {}).get("wait_type") or "approval") == "approval"
                    for step in steps if str(step.get("id")) in downstream(steps, source)
                )
                for source in service_steps(steps, key)
            )
            # The approval a publishing grant is made from reviews what that service then publishes.
            or any(
                set(service_steps(steps, key)) & downstream(steps, str((step.get("config") or {}).get("approval_step_id")))
                for step in steps if step.get("type") == "workflow_action_grant"
            )
            for _slug, steps in graphs
        ):
            issues.append({
                "severity": "P0",
                "where": f"services.{key}.human_review",
                "message": (
                    f"the user reviews '{key}' results, but no Workflow waits for that approval. Add a wait step "
                    "with config.wait_type='approval' after this service's agent step."
                ),
            })
    return issues


def _unscheduled_workflow_issues(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Workflows that do nothing unless an Automation runs them.

    One that records assessed Stats, or one that leases due projects (a
    schedule worker), runs on a schedule.
    """
    scheduled = {str((job.get("execution_target") or {}).get("workflow_slug") or "")
                 for job in fields.get("scheduled_jobs") or [] if isinstance(job, dict)}
    reviewed = {str((stat.get("collector_config") or {}).get("workflow_slug") or "")
                for stat in fields.get("stats") or []
                if isinstance(stat, dict) and stat.get("collector_type") == "agent_review"}
    issues = []
    for workflow in fields.get("workflows") or []:
        slug = str((workflow or {}).get("slug") or "") if isinstance(workflow, dict) else ""
        leases = any(step.get("type") == "workflow_project" and (step.get("config") or {}).get("operation") == "lease_due"
                     for step in workflow.get("steps") or [] if isinstance(step, dict)) if slug else False
        if slug and slug not in scheduled and (slug in reviewed or leases):
            issues.append({"severity": "P1", "where": f"workflows.{slug}", "message": (
                f"'{slug}' {'records Stats' if slug in reviewed else 'starts due work'} only when it runs on a "
                "schedule: add an automation whose scheduled_job targets this Workflow.")})
    return issues


# A run input only something from outside can fill: the id of a message or comment, an event's payload.
_OUTSIDE_EVENT_INPUT = re.compile(
    r"(?:^|_)(?:message|email|mail|comment|thread|dm|conversation|notification)_(?:id|event|payload)$"
    r"|(?:^|_)(?:event|webhook|payload)(?:_(?:id|data|payload))?$")
# Triggers that start nothing by themselves: an ``event`` binding fires only on an API call nothing here makes.
_UNFIRED_TRIGGERS = frozenset({"", "manual", "internal", "event"})


def unstartable_workflow_issues(fields: Dict[str, Any], *, planned: Any = ()) -> List[Dict[str, Any]]:
    """Workflows nothing starts: internal with no Workflow running them, or waiting for an outside event.

    A channel hands a new message to its service's Agent, never to a Workflow,
    so one that takes an email's or comment's id runs only if something gives
    it that id. ``planned`` are Workflows the design already asks an Automation for.
    """
    from packages.core.services.workspace_automations_designer import NEEDED_FIELD

    workflows = [row for row in fields.get("workflows") or [] if isinstance(row, dict) and row.get("slug")]
    started = {str((job.get("execution_target") or {}).get("workflow_slug") or "")
               for job in fields.get("scheduled_jobs") or [] if isinstance(job, dict)}
    for name in (NEEDED_FIELD, "automations"):
        started |= {str(row.get("workflow") or "") for row in fields.get(name) or [] if isinstance(row, dict)}
    started |= {str(slug) for slug in planned}
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            if isinstance(step, dict) and step.get("type") in {"subworkflow", "foreach_subworkflow"}:
                config = step.get("config") or {}
                started |= {str(config.get("workflow_id") or ""), str(config.get("source_workflow_key") or "")}
    issues: List[Dict[str, Any]] = []
    for workflow in workflows:
        slug = str(workflow["slug"])
        if slug in started or str(workflow.get("trigger_type") or "") not in _UNFIRED_TRIGGERS:
            continue
        if workflow.get("internal"):
            issues.append({"severity": "P1", "where": f"workflows.{slug}", "message": (
                f"'{slug}' is internal and no Workflow runs it, so nothing can start it.")})
            continue
        if (workflow.get("binding_config") or {}).get("started_by_person"):
            continue
        rows = [*(workflow.get("run_inputs") or []),
                *((step.get("config") or {}).get("run_inputs") or [] for step in workflow.get("steps") or []
                  if isinstance(step, dict) and step.get("type") == "trigger")]
        rows = [cell for row in rows for cell in (row if isinstance(row, list) else [row]) if isinstance(cell, dict)]
        waiting_for = sorted({str(row.get("key")) for row in rows if _OUTSIDE_EVENT_INPUT.search(str(row.get("key") or ""))})
        if waiting_for:
            issues.append({"severity": "P1", "where": f"workflows.{slug}", "message": (
                f"'{slug}' waits for {', '.join(waiting_for)}, which only something from outside can give, and nothing "
                "starts it: no channel hands a new message to a Workflow. Have an Automation start it "
                "(wf_pattern_source_watch), or give it trigger_type:'webhook' if the sender can call a URL. If the "
                "person starts it by hand, keep what they said in binding_config:{started_by_person:'<their words>'}. "
                "Tell the person what still has to be connected, or ask them.")})
    return issues


async def _lint_draft(db, *, entity_id: str, user_id: str = "", **kwargs):
    from sqlalchemy import and_, or_, select

    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.runtime.task_requirements import (
        normalize_task_runtime_capability_ids,
        task_runtime_capability_validation_errors,
    )
    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import Agent, AgentToolBinding, ToolDefinition
    from packages.core.services.agent_provisioning_service import spec_from_create_agent_draft
    from packages.core.services.integration_resolution import integration_provider_readiness
    from packages.core.services.provider_keys import canonical_provider_key
    from packages.core.services.workspace_integration_scope import integration_provider_for_tool

    draft_id = kwargs.get("draft_id", "")
    draft = await _load_draft(db, draft_id, entity_id, user_id)
    if draft is None:
        return _err("draft not found")

    fields = dict(draft.fields or {})
    issues: List[Dict[str, Any]] = []
    pending_connections: set[str] = set()

    def _note_pending_connection(where: str, provider: Any) -> None:
        """A requested integration that is not connected yet never blocks
        creation: the Workspace keeps the requirement, asks the user to
        connect it in Workspace chat, and the work starts once connected."""
        from packages.core.services.integration_resolution import (
            is_user_facing_integration_provider,
        )
        from packages.core.services.provider_keys import canonical_provider_key

        key = canonical_provider_key(provider)
        # Internal bridges have nothing for the user to connect.
        if not is_user_facing_integration_provider(key) or key in pending_connections:
            return
        pending_connections.add(key)
        issues.append({
            "severity": "P2",
            "where": where,
            "message": (
                f"{key!r} is not connected yet. Keep it: the Workspace is created with it "
                "waiting for connection and asks the user to connect it in Workspace chat. "
                "Tell the user, and do not remove the channel, binding or rules that need it."
            ),
        })

    # A data component is executable configuration, not decorative prose.  It
    # may only refer to native resources actually present in this Draft.  The
    # same validator runs again during Blueprint export/install with resolved
    # IDs, but reject invalid AI output before a Draft can be marked ready.
    if isinstance(fields.get("app_view"), dict):
        try:
            from packages.core.services.workspace_app_view import validate_app_view_data_contract
            workflows = [item for item in fields.get("workflows") or [] if isinstance(item, dict)]
            definitions = {str(item.get("slug") or "") for item in workflows if item.get("slug")}
            bindings = {str(item.get("slug") or "") for item in workflows if item.get("slug") and not item.get("internal")}
            stats = {str(item.get("key") or item.get("library_key") or "") for item in fields.get("stats") or [] if isinstance(item, dict)}
            validate_app_view_data_contract(
                fields["app_view"].get("data_sources", {}), fields["app_view"].get("components", []),
                catalog={"workflow_definitions": definitions, "workflow_bindings": bindings,
                         "stat_keys": stats, "workflows": workflows},
            )
        except ValueError as exc:
            issues.append({"severity": "P0", "where": "app_view", "message": str(exc)})
    from packages.core.services.workspace_app_designer import project_structure_issues
    issues.extend(project_structure_issues(
        [item for item in fields.get("workflows") or [] if isinstance(item, dict) and item.get("slug")]))
    from packages.core.services.workspace_part_designers import missing_app_issue, pending_candidate_issues
    issues.extend(pending_candidate_issues(fields))
    # A Blueprint's page is its author's design, including none.
    app_issue = None if getattr(draft, "applied_blueprint_id", None) else missing_app_issue(fields)
    if app_issue is not None:
        issues.append(app_issue)
    issues.extend(_unscheduled_workflow_issues(fields))
    from packages.core.services.workspace_build_spec import build_spec
    if build_spec(fields) is not None:
        issues.extend(unstartable_workflow_issues(fields))
    try:
        _validate_draft_resources(fields)
    except ValueError as exc:
        issues.append({"severity": "P0", "where": "resources", "message": str(exc)})
    issues.extend(workflow_process_issues(fields))

    # Top-level basics
    for required in ("name", "kind", "operating_context", "primary_work"):
        if not (fields.get(required) or "").strip():
            issues.append({"severity": "P0", "where": required, "message": f"{required} is empty -- call ws_commit_basics."})

    services = fields.get("services") or []
    if not services:
        issues.append({"severity": "P0", "where": "services", "message": "No services defined -- call ws_propose_service at least once."})

    service_keys = {(s or {}).get("service_key") for s in services}
    service_required_capabilities: dict[str, tuple[str, ...]] = {}

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
        if "required_capabilities" not in (svc or {}):
            # Existing drafts remain compatible. New ws_propose_service calls
            # are schema-required to make this execution contract explicit.
            continue
        raw_requirements = (svc or {}).get("required_capabilities")
        requirement_path = f"services.{sk}.required_capabilities"
        if not isinstance(raw_requirements, list) or any(
            not isinstance(value, str) for value in raw_requirements
        ):
            issues.append({
                "severity": "P0",
                "where": requirement_path,
                "message": "required_capabilities must be an array of canonical capability ids.",
            })
            continue
        requirement_errors = task_runtime_capability_validation_errors(
            raw_requirements,
            allowed_ids=STRATEGIST_TASK_CAPABILITY_IDS,
            path=requirement_path,
        )
        for error in requirement_errors:
            issues.append({
                "severity": "P0",
                "where": error["path"],
                "message": error["message"],
            })
        if not requirement_errors and sk:
            service_required_capabilities[str(sk)] = normalize_task_runtime_capability_ids(
                raw_requirements,
                allowed_ids=STRATEGIST_TASK_CAPABILITY_IDS,
            )

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
    mapped_agent_ids = available_agent_ids | available_marketplace_agent_ids
    mapped_integration_rows = list((await db.execute(
        select(AgentMCPBinding.agent_id, MCPServer.server_key)
        .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
        .where(
            AgentMCPBinding.agent_id.in_(mapped_agent_ids),
            AgentMCPBinding.status == "active",
        )
    )).all()) if mapped_agent_ids and not getattr(draft, "applied_blueprint_id", None) else []
    if mapped_agent_ids and not getattr(draft, "applied_blueprint_id", None):
        tool_rows = (await db.execute(
            select(AgentToolBinding.agent_id, ToolDefinition.name)
            .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
            .where(
                AgentToolBinding.agent_id.in_(mapped_agent_ids),
                ToolDefinition.status == "active",
            )
        )).all()
        mapped_integration_rows.extend(
            (agent_id, provider)
            for agent_id, tool_name in tool_rows
            if (provider := integration_provider_for_tool(tool_name))
        )
        skill_tool_rows = (await db.execute(
            select(AgentSkillBinding.agent_id, Skill.tools)
            .join(Skill, Skill.id == AgentSkillBinding.skill_id)
            .where(
                AgentSkillBinding.agent_id.in_(mapped_agent_ids),
                AgentSkillBinding.status == "active",
                Skill.status == "active",
            )
        )).all()
        mapped_integration_rows.extend(
            (agent_id, provider)
            for agent_id, tools in skill_tool_rows
            for tool_name in (tools or [])
            if (provider := integration_provider_for_tool(tool_name))
        )
    mapped_integration_rows = list(dict.fromkeys(mapped_integration_rows))
    mapped_mcp_readiness = await integration_provider_readiness(
        db,
        entity_id=entity_id,
        user_id=user_id or None,
        provider_keys=[server_key for _, server_key in mapped_integration_rows],
    ) if mapped_integration_rows else {}

    custom_drafts = [
        dict((mapping or {}).get("create_agent_draft") or {})
        for mapping in mappings
        if (mapping or {}).get("strategy") == "create_custom"
    ]
    tool_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in spec_from_create_agent_draft(custom).tool_bindings
        if str(ref or "").strip()
    }
    skill_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in (custom.get("skill_bindings") or [])
        if str(ref or "").strip()
    }
    mcp_refs = {
        str(ref).strip()
        for custom in custom_drafts
        for ref in (custom.get("mcp_bindings") or [])
        if str(ref or "").strip()
    }
    available_tool_refs, available_skill_refs, available_mcp_refs = await _binding_vocabulary(
        db,
        entity_id=entity_id,
        user_id=user_id,
        tool_refs=tool_refs,
        skill_refs=skill_refs,
        mcp_refs=mcp_refs,
    )
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
    # Same rule the install binds with: a Blueprint export names a built-in
    # as ``builtin:<slug>``, never by its deployment-specific row id.
    from packages.core.services.marketplace_skill_service import (
        resolve_platform_marketplace_skill,
    )

    available_platform_skill_ids: set[str] = set()
    for platform_skill_id in platform_skill_ids:
        try:
            await resolve_platform_marketplace_skill(
                db, entity_id=entity_id, skill_id=platform_skill_id,
            )
        except ValueError:
            continue
        available_platform_skill_ids.add(platform_skill_id)
    manor_skill_ids = {
        str(ref.get("marketplace_id") or "").strip()
        for ref in exact_skill_binding_refs
        if str(ref.get("marketplace_source") or "platform").strip() == "manor"
        and str(ref.get("marketplace_id") or "").strip()
    }
    available_manor_skill_ids: set[str] = set()
    if manor_skill_ids and is_cloud():
        pass

    mcp_rows = list((await db.execute(
        select(MCPServer).where(
            MCPServer.status == "active",
            (MCPServer.id.in_(mcp_refs)) | (MCPServer.server_key.in_(mcp_refs)),
        )
    )).scalars().all()) if mcp_refs else []
    mcp_readiness = await integration_provider_readiness(
        db,
        entity_id=entity_id,
        user_id=user_id or None,
        provider_keys=[server.server_key for server in mcp_rows],
    ) if mcp_rows else {}

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
            required_capabilities = set(service_required_capabilities.get(str(sk), ()))
            if required_capabilities:
                covered_capabilities = _custom_agent_runtime_capability_ids(
                    custom,
                    mcp_rows=mcp_rows,
                )
                missing_capabilities = sorted(
                    required_capabilities - covered_capabilities
                )
                for missing_capability in missing_capabilities:
                    issues.append({
                        "severity": "P0",
                        # The bounded pre-finalize repair already owns this
                        # path and can only revise Agent binding fields.
                        "where": f"agent_mappings.{sk}.tool_bindings",
                        "binding_ref": missing_capability,
                        "missing_capabilities": [missing_capability],
                        "message": (
                            "custom Agent bindings do not cover required runtime "
                            f"capability {missing_capability!r}"
                            + (
                                f" ({CORE_CAPABILITIES[missing_capability].description.rstrip('.')}). "
                                if missing_capability in CORE_CAPABILITIES else ". "
                            )
                            + "Requirements diagnose the gap; they do not grant tools. If the service does "
                            "not need this, remove it from the service's required_capabilities "
                            "(reading Knowledge is workspace.search)."
                        ),
                    })
            # Provisioning adds capability, baseline, and Skill invocation
            # tools; readiness must validate the same effective bindings.
            for ref in spec_from_create_agent_draft(custom).tool_bindings:
                if ref not in available_tool_refs:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.tool_bindings",
                        "binding_ref": ref,
                        "message": f"tool binding {ref!r} is not available.",
                    })
            for ref in custom.get("business_capabilities") or []:
                if ref not in CORE_CAPABILITIES:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.business_capabilities",
                        "binding_ref": ref,
                        "message": f"business capability {ref!r} is not available.",
                    })
            for ref in custom.get("skill_bindings") or []:
                if ref not in available_skill_refs and ref not in generated_skill_refs:
                    issues.append({
                        "severity": "P0",
                        "where": f"agent_mappings.{sk}.skill_bindings",
                        "binding_ref": ref,
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
                        "binding_ref": ref,
                        "message": f"MCP binding {ref!r} is not in the supported integration inventory.",
                    })
                elif not getattr(draft, "applied_blueprint_id", None):
                    server = next((row for row in mcp_rows if ref in (row.id, row.server_key)), None)
                    provider = canonical_provider_key(server.server_key) if server else ""
                    if not mcp_readiness.get(provider) or not mcp_readiness[provider].ready:
                        _note_pending_connection(f"agent_mappings.{sk}.mcp_bindings", provider or ref)
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
            elif not getattr(draft, "applied_blueprint_id", None):
                for bound_agent_id, server_key in mapped_integration_rows:
                    provider = canonical_provider_key(server_key)
                    if bound_agent_id == expected_agent_id and (
                        not mapped_mcp_readiness.get(provider)
                        or not mapped_mcp_readiness[provider].ready
                    ):
                        _note_pending_connection(f"agent_mappings.{sk}.mcp_bindings", provider)

    if not getattr(draft, "applied_blueprint_id", None):
        for mapping in mappings:
            service_key = str((mapping or {}).get("service_key") or "")
            for item in _mapping_missing_integrations(mapping):
                _note_pending_connection(
                    f"agent_mappings.{service_key}.missing_integrations", item.get("provider"),
                )
        for item in fields.get("flagged_integrations") or []:
            if isinstance(item, dict):
                _note_pending_connection("flagged_integrations", item.get("provider"))

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
    if not getattr(draft, "applied_blueprint_id", None):
        from packages.core.services.integration_service import get_integration_inventory
        from packages.core.services.workspace_readiness import BUILT_IN_CHANNEL_TYPES

        channels = [
            ("primary_external_channel", cc.get("primary_external_channel")),
            *[(f"secondary_external_channels.{index}", item)
              for index, item in enumerate(cc.get("secondary_external_channels") or [])],
        ]
        external = [
            (path, item) for path, item in channels
            if isinstance(item, dict)
            and item.get("channel_type") not in BUILT_IN_CHANNEL_TYPES
            and item.get("channel_type") not in UNBACKED_CHANNEL_TYPES
        ]
        if external:
            inventory = await get_integration_inventory(db, entity_id, user_id=user_id)
            connected = {
                canonical_provider_key(item.get("provider"))
                for item in inventory.get("integrations") or []
                if item.get("ready")
            }
            for path, item in external:
                provider = canonical_provider_key(item.get("provider") or item.get("channel_type"))
                if provider not in connected:
                    _note_pending_connection(f"channel_config.{path}", provider)

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

    # A hard-block rule and an approval rule on the same action: the block
    # wins and the approval never fires. Seen in production as a workspace
    # whose owner could not tell why posts were never published.
    from packages.core.services.workspace_setup_service import (
        describe_rule_enforcement_conflict,
        rule_enforcement_conflicts,
    )

    # An approval or ban that names no action is guidance only; nothing would
    # stop the action it describes.
    for rule in fields.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get("rule_type") or "").strip().lower()
        if (rule_type == "approval_required" or rule_type in _HARD_BLOCK_RULE_TYPES) and not rule.get("action_patterns"):
            issues.append({
                "severity": "P1",
                "where": f"rules.{rule.get('rule_key') or '?'}",
                "message": (
                    f"{rule_type} rule has no action_patterns, so it will not be enforced. "
                    "Re-propose it with the action keys it covers (e.g. email.send); keep them "
                    "even when that integration is not connected yet."
                ),
            })

    for conflict in rule_enforcement_conflicts(fields.get("rules") or []):
        issues.append({
            "severity": "P1",
            "where": f"rules.{conflict['pattern']}",
            "message": describe_rule_enforcement_conflict(conflict),
        })

    # An automation is created only with a readable clock schedule; catch the
    # rest here so the user decides, rather than finalize skipping it.
    from packages.core.schedule_cadence import automation_cadence, looks_like_event_trigger

    native_job_keys = {
        str(job.get("job_id") or "").strip()
        for job in fields.get("scheduled_jobs") or [] if isinstance(job, dict)
    }
    workflow_slugs = [str(row.get("slug")) for row in fields.get("workflows") or []
                      if isinstance(row, dict) and row.get("slug")]
    for a in fields.get("automations") or []:
        if not isinstance(a, dict) or str(a.get("automation_key") or "").strip() in native_job_keys:
            continue
        # Staging 2026-11-09, 2 of 3 candle shop drafts: "start create_product_post
        # three times" left how many posts and what each covers to the Agent.
        started = [slug for slug in workflow_slugs if re.search(
            rf"\b(start|run|initiate|trigger|launch)\w*\b[^.]*\b{re.escape(slug)}\b",
            str(a.get("description") or ""), re.IGNORECASE)]
        if started:
            issues.append({
                "severity": "P0",
                "where": f"automations.{a.get('automation_key', '?')}",
                "message": (
                    f"automation asks its Agent to start {', '.join(started)}: schedule the Flow itself with "
                    "scheduled_job.execution_target.workflow_slug; when it runs once per planned item, write a Flow "
                    f"that plans the items and runs {started[0]} for each (foreach_subworkflow over the plan) and "
                    "schedule that one."
                ),
            })
        try:
            readable = automation_cadence(a) is not None
        except (TypeError, ValueError):
            readable = False
        if readable:
            continue
        event = looks_like_event_trigger(a.get("trigger"))
        issues.append({
            "severity": "P0",
            "where": f"automations.{a.get('automation_key', '?')}",
            "message": (
                "automation is triggered by an event, which a scheduled automation cannot do; "
                "remove it and tell the user, or replace it with a schedule they confirm."
                if event else
                "automation has no readable schedule; re-propose it with cron_expr or every_seconds."
            ),
        })

    # Staging 2026-11-09: a restaurant draft had weekly_content and weekly_review_replies and no
    # schedule at all, so every weekly routine waited for a person to start it.
    scheduled = {str((job.get("execution_target") or {}).get("workflow_slug") or "")
                 for job in fields.get("scheduled_jobs") or [] if isinstance(job, dict)}
    for row in fields.get("workflows") or []:
        if not isinstance(row, dict) or row.get("internal") or str(row.get("slug") or "") in scheduled:
            continue
        slug = str(row.get("slug") or "")
        cadence = sorted(set(slug.split("_")) & _ROUTINE_WORDS) or re.findall(_ROUTINE_NAME, str(row.get("name") or ""))
        if cadence:
            issues.append({
                "severity": "P0",
                "where": f"workflows.{slug}",
                "message": (
                    f"{slug} reads as a {cadence[0]} routine but no schedule runs it: add an Automation whose "
                    f"scheduled_job.execution_target.workflow_slug is {slug!r}. If a person starts it by hand "
                    "each time, name it for what it makes, not for its cadence."
                ),
            })

    from packages.core.services.workspace_workflow_designer import chat_post_step_issues

    for message in chat_post_step_issues([row for row in fields.get("workflows") or [] if isinstance(row, dict)]):
        issues.append({"severity": "P0", "where": "workflows", "message": message})

    # Automations referencing nonexistent services
    for a in fields.get("automations") or []:
        sk = (a or {}).get("service_key")
        if sk and sk not in service_keys:
            issues.append({
                "severity": "P1",
                "where": f"automations.{(a or {}).get('automation_key','?')}",
                "message": f"automation references unknown service '{sk}'.",
            })

    # Sources the user attached but nothing consumes: a soft warning so the
    # architect either attaches them (knowledge / skill) or says they were left out.
    from packages.core.services.workspace_draft_sources import draft_sources

    referenced_source_ids: set[str] = set()
    for ka in fields.get("knowledge_attachments") or []:
        referenced_source_ids.update(str(s) for s in ((ka or {}).get("source_ids") or []))
    for mapping in mappings:
        custom = (mapping or {}).get("create_agent_draft") or {}
        for spec in custom.get("missing_skill_specs") or []:
            referenced_source_ids.update(str(s) for s in ((spec or {}).get("source_ids") or []))
    for source in draft_sources(fields):
        if source.get("status") != "ready" or str(source.get("id")) in referenced_source_ids:
            continue
        issues.append({
            "severity": "P2",
            "where": f"sources.{source.get('id')}",
            "message": (
                f"source '{source.get('name')}' is not used by any knowledge net or skill -- "
                "attach it with ws_attach_knowledge(source_ids=[...]), encode it in "
                "missing_skill_specs[].source_ids, or tell the user it was left out."
            ),
        })

    from packages.core.services.workspace_agents_designer import spec_platform_issues
    from packages.core.services.workspace_schedule_cost import schedule_cost_summary

    issues.extend(await spec_platform_issues(db, fields, entity_id=entity_id, user_id=user_id))
    schedule_cost = await schedule_cost_summary(db, fields, entity_id=entity_id, user_id=user_id)

    p0 = sum(1 for i in issues if i["severity"] == "P0")
    return _ok({
        "ok_to_finalize": p0 == 0,
        "p0": p0,
        "p1": sum(1 for i in issues if i["severity"] == "P1"),
        "issues": issues,
        **_issues_for_designers(fields, issues),
        **({"schedule_cost": schedule_cost} if schedule_cost else {}),
    })


def _issues_for_designers(fields: Dict[str, Any], issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """With a team build spec, which part designer each P0/P1 issue goes back to."""
    from packages.core.services.workspace_build_spec import build_spec, issues_by_part
    from packages.core.services.workspace_part_designers import CANDIDATES_FIELD, designer_lint_where

    spec = build_spec(fields)
    if spec is None or spec.get("mode") != "team":
        return {}
    owned = {part: [issue for issue in rows if not str(issue.get("where") or "").startswith(CANDIDATES_FIELD)]
             for part, rows in issues_by_part(issues, designer_lint_where()).items()}
    owned = {part: rows for part, rows in owned.items() if rows and part in (spec.get("progress") or {})}
    if not owned:
        return {}
    return {"by_part": {part: [issue["where"] for issue in rows] for part, rows in owned.items()},
            "next": ("Send each part its issues as feedback: ws_design_parts(parts=[{part, feedback}]) quoting "
                     "them; fix the others yourself.")}


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
    "ws_ask_user": _ask_user,
    "ws_commit_basics": _commit_basics,
    "ws_propose_service": _propose_service,
    "ws_design_app_view": _design_app_view,
    "ws_accept_app_view": _accept_app_view,
    "ws_accept_parts": _accept_parts,
    "ws_design_workflows": _design_workflows,
    "ws_commit_build_spec": _commit_build_spec,
    "ws_design_parts": _design_parts,
    "ws_propose_workflow": _propose_workflow,
    "ws_propose_record_collection": _propose_record_collection,
    "ws_propose_source_watch": _propose_source_watch,
    "ws_propose_stat": _propose_stat,
    "ws_propose_goal": _propose_goal,
    "ws_propose_agent_mapping": _propose_agent_mapping,
    "ws_request_custom_agent": _request_custom_agent,
    "ws_assign_staff": _assign_staff,
    "ws_attach_knowledge": _attach_knowledge,
    "ws_search_capabilities": _search_capabilities,
    "ws_propose_channel": _propose_channel,
    "ws_flag_missing_integration": _flag_missing_integration,
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
    "ws_apply_blueprint": _apply_blueprint,
    "ws_get_draft": _get_draft,
    "ws_read_source": _read_source,
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
                    logger.exception("tool %s failed on the database", name)
                    return _tool_failed(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s crashed", name)
            return _tool_failed(exc)
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
