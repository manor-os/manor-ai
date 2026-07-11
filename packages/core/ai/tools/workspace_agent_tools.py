"""Workspace Agent tools for Manor AI in workspace chat.

These tools are the durable operation layer behind natural-language workspace
chat. They let the master agent turn user messages into workspace-scoped tasks,
task runtime requirements, persistent guardrails, and strategist reviews without
going through the broad ``manor`` composite gateway.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import (
    runtime_get_goal_status_action,
    runtime_update_goal_value_action,
    runtime_workspace_add_knowledge_documents_action,
    runtime_workspace_add_rule_action,
    runtime_workspace_create_knowledge_folder_action,
    runtime_workspace_create_task_action,
    runtime_workspace_delegate_service_action,
    runtime_workspace_list_knowledge_action,
    runtime_workspace_operation_action,
    runtime_workspace_remove_knowledge_document_action,
    runtime_workspace_request_strategist_review_action,
    runtime_workspace_update_task_runtime_action,
    runtime_workspace_update_knowledge_policy_action,
)


WORKSPACE_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_agent",
        "description": "Workspace control: search, task, knowledge, rule, strategist.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search",
                        "create_task",
                        "update_task_runtime",
                        "list_knowledge",
                        "create_knowledge_folder",
                        "add_knowledge_documents",
                        "remove_knowledge_document",
                        "update_knowledge_policy",
                        "add_rule",
                        "delegate_service",
                        "get_goal_status",
                        "update_goal_value",
                        "operation",
                        "request_strategist_review",
                    ],
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action-specific parameters. For delegate_service include "
                        "service_key or agent_subscription_id, prompt, and optional max_rounds. "
                        "For get_goal_status pass optional goal_id; the current workspace_id "
                        "is applied automatically. For update_goal_value pass goal_id, value, "
                        "and optional note to append a verified manual goal_measurements row."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


WORKSPACE_LIST_KNOWLEDGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_list_knowledge",
        "description": (
            "List workspace Knowledge Nets and attached documents. "
            "Use before changing workspace knowledge when the user refers to a "
            "net or document by name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_documents": {
                    "type": "boolean",
                    "description": "Include document details for each group. Default true.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum documents per group, default 20.",
                },
            },
            "required": [],
        },
    },
}


WORKSPACE_CREATE_KNOWLEDGE_FOLDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_create_knowledge_folder",
        "description": (
            "Create a user-manageable workspace Knowledge Net. Nets are "
            "optional document networks inside the workspace's knowledge; documents added here "
            "remain normal Knowledge documents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Knowledge Net name."},
                "purpose": {
                    "type": "string",
                    "description": "How agents should use this Knowledge Net.",
                },
                "use_by_default": {
                    "type": "boolean",
                    "description": "Prefer this Knowledge Net in workspace retrieval policy.",
                },
            },
            "required": ["name"],
        },
    },
}


WORKSPACE_ADD_KNOWLEDGE_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_add_knowledge_documents",
        "description": (
            "Attach existing Knowledge documents to workspace knowledge. Use the "
            "default Workspace Knowledge collection when no group is specified. "
            "This does not duplicate or move the documents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Existing document ids to attach.",
                },
                "document_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Document names to resolve when ids are not known.",
                },
                "group_id": {"type": "string", "description": "Optional target Knowledge Net/group id."},
                "group_name": {
                    "type": "string",
                    "description": "Optional target Knowledge Net name. If omitted, uses Workspace Knowledge.",
                },
                "create_group_if_missing": {
                    "type": "boolean",
                    "description": "Create group_name as a Knowledge Net if it does not exist.",
                },
                "use_by_default": {
                    "type": "boolean",
                    "description": "Prefer the target group in workspace retrieval policy.",
                },
            },
            "required": [],
        },
    },
}


WORKSPACE_REMOVE_KNOWLEDGE_DOCUMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_remove_knowledge_document",
        "description": (
            "Detach a document from workspace knowledge without deleting the "
            "document from the user's Knowledge base."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Document id to detach."},
                "document_name": {"type": "string", "description": "Document name to resolve if id is unknown."},
                "group_id": {"type": "string", "description": "Optional group id to detach from."},
                "group_name": {"type": "string", "description": "Optional group name to detach from."},
                "all_groups": {
                    "type": "boolean",
                    "description": "When true or no group is given, detach from all workspace knowledge groups.",
                },
            },
            "required": [],
        },
    },
}


WORKSPACE_UPDATE_KNOWLEDGE_POLICY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_update_knowledge_policy",
        "description": (
            "Update workspace knowledge retrieval policy or Knowledge Net metadata, such "
            "as auto-search, cite sources, strict mode, net purpose, and "
            "whether a net is preferred by default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "auto_search": {"type": "boolean", "description": "Let agents search workspace knowledge when relevant."},
                "citation_required": {"type": "boolean", "description": "Prefer citations/source names when knowledge affects an answer."},
                "strict_mode": {"type": "boolean", "description": "For knowledge tasks, stay inside workspace knowledge unless user adds more sources."},
                "retrieval_mode": {
                    "type": "string",
                    "enum": ["auto", "manual", "strict"],
                    "description": "Workspace retrieval mode.",
                },
                "group_id": {"type": "string", "description": "Optional Knowledge Net/group id to update."},
                "group_name": {"type": "string", "description": "Optional Knowledge Net/group name to update."},
                "name": {"type": "string", "description": "Optional new Knowledge Net/group name."},
                "purpose": {"type": "string", "description": "Optional Knowledge Net/group purpose."},
                "use_by_default": {"type": "boolean", "description": "Add or remove the Knowledge Net from default/preferred groups."},
            },
            "required": [],
        },
    },
}


WORKSPACE_CREATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_create_task",
        "description": (
            "Create a new task or customer support ticket inside the current "
            "workspace and persist any task-specific runtime instructions, "
            "required knowledge references, and guardrail rules in "
            "task.details.runtime_context. Use this in workspace chat or "
            "public/customer chat when the user asks Manor AI to prepare, do, "
            "follow up, coordinate a concrete piece of work, or record an "
            "issue/request for staff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title."},
                "description": {"type": "string", "description": "Task details and expected outcome."},
                "priority": {
                    "type": "integer",
                    "description": "Priority: 5=critical, 4=high, 3=medium, 2=low, 1=minimal.",
                },
                "task_type": {"type": "string", "description": "Task type slug, default general."},
                "assignee_id": {"type": "string", "description": "Optional user/staff assignee id."},
                "agent_id": {"type": "string", "description": "Optional agent id to associate with the task."},
                "agent_type": {"type": "string", "description": "Optional agent type/service key hint."},
                "deadline": {"type": "string", "description": "Optional ISO-8601 deadline."},
                "runtime_instructions": {
                    "type": "string",
                    "description": "Task-only instructions from the user's latest message.",
                },
                "required_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Document ids, file refs, or knowledge group refs required for this task.",
                },
                "rules": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Task-only guardrails. Each rule may include description, "
                        "rule_type, action_patterns, severity, and rule_key."
                    ),
                },
                "knowledge_query": {
                    "type": "string",
                    "description": "Natural-language description of knowledge the worker should consult.",
                },
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional Manor Runtime BusinessCapability ids needed by this task, "
                        "such as workspace.search, web.safe_search, file.write, "
                        "sandbox.execute, or automation.manage. Use capability ids, "
                        "not tool names."
                    ),
                },
                "owner_service_key": {
                    "type": "string",
                    "description": "Optional workspace service key that should own/plan this task.",
                },
                "delegate_service_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional workspace service keys the planner may delegate to.",
                },
                "depends_on_task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional predecessor task ids whose outputs are required before "
                        "this task can start. Use when the user's request depends on "
                        "previous or running workspace task outputs."
                    ),
                },
                "goal_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional active workspace goal ids or metric keys this task is "
                        "intended to move. Use when the user's request is part of a "
                        "specific goal."
                    ),
                },
                "start": {
                    "type": "boolean",
                    "description": "Set true when the user wants the work to begin now, not just be tracked.",
                },
            },
            "required": ["title"],
        },
    },
}


WORKSPACE_UPDATE_TASK_RUNTIME_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_update_task_runtime",
        "description": (
            "Add or replace runtime instructions, required references, and "
            "task-only guardrail rules for an existing workspace task. Use when "
            "the user gives extra requirements for a specific task during "
            "workspace chat. Default behavior is append-only; set replace=true "
            "only when the user explicitly wants to overwrite prior runtime context. "
            "For added roles or review stages on an active task, use this tool instead "
            "of creating workspace-wide rules or new tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Existing workspace task id."},
                "runtime_instructions": {"type": "string", "description": "Additional task-only instructions."},
                "required_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional document/file/knowledge refs required for the task.",
                },
                "rules": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Additional task-only guardrails.",
                },
                "knowledge_query": {
                    "type": "string",
                    "description": "Knowledge the task should consult.",
                },
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Additional Manor Runtime BusinessCapability ids needed by this task. "
                        "Use capability ids, not tool names."
                    ),
                },
                "replace": {
                    "type": "boolean",
                    "description": "When true, replace runtime_context instead of appending.",
                },
            },
            "required": ["task_id"],
        },
    },
}


WORKSPACE_ADD_RULE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_add_rule",
        "description": (
            "Add a persistent workspace-level operating rule/guardrail from "
            "natural language and enrich it with action_patterns. By default "
            "this creates an operation draft and asks the user to approve before "
            "syncing runtime governance. Use only when the user clearly wants "
            "the rule to apply to the workspace beyond a single task. Do not "
            "use for temporary task roles, review stages, or downstream workflow "
            "appended to an existing task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "The rule in the user's words.",
                },
                "rule_key": {
                    "type": "string",
                    "description": "Optional stable key. Generated from description when omitted.",
                },
                "rule_type": {
                    "type": "string",
                    "enum": [
                        "approval_required",
                        "hitl_required",
                        "require_approval",
                        "deny",
                        "never_allow",
                        "block",
                        "draft_only",
                        "auto_approve",
                        "allow",
                        "exception",
                    ],
                    "description": "Optional enforcement type. Natural language is inferred when omitted.",
                },
                "action_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit governance action-key patterns.",
                },
                "severity": {"type": "string", "description": "Optional severity label."},
                "notes": {"type": "string", "description": "Optional operator notes."},
                "user_confirmation": {
                    "type": "boolean",
                    "description": (
                        "Set true only when the user has explicitly approved applying this "
                        "exact rule change. Default false creates a review draft first."
                    ),
                },
            },
            "required": ["description"],
        },
    },
}


WORKSPACE_REQUEST_STRATEGIST_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_request_strategist_review",
        "description": (
            "Request a Strategist review for the current workspace. Use when "
            "the user asks Manor AI to plan, reprioritize, propose next tasks, "
            "or reassess workspace goals/rules/knowledge after new context. Do not "
            "use merely because the user appended task-local roles or review stages; "
            "persist those with workspace_update_task_runtime unless the user explicitly "
            "asks to replan or create follow-up tasks now."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why the review is needed."},
                "countdown_seconds": {
                    "type": "integer",
                    "description": "Delay before dispatch, 0-300 seconds. Default 1.",
                },
            },
            "required": [],
        },
    },
}


WORKSPACE_OPERATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_operation",
        "description": (
            "Draft, validate, preview, apply, or discard workspace runtime "
            "changes. Use for workspace-wide service/agent/tool/skill/channel/"
            "integration/rule/budget/heartbeat/strategist changes. Applying "
            "requires explicit user confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get_current",
                        "create_draft",
                        "patch_draft",
                        "validate_draft",
                        "preview_diff",
                        "apply_draft",
                        "discard_draft",
                    ],
                },
                "draft_id": {"type": "string", "description": "Operation draft id when updating an existing draft."},
                "source_event_id": {"type": "string", "description": "Optional source label for audit."},
                "patches": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Operation patches, for example budget_policy.update, "
                        "heartbeat_policy.update, rule.add, service_role.upsert, "
                        "capability_binding.upsert, skill_binding.upsert, "
                        "integration_scope.update, channel.upsert, "
                        "agent_binding.upsert, agent_binding.remove, "
                        "agent_mappings.replace, strategist.update, "
                        "evaluation.update, operating_model.update, "
                        "goal.measurement_sources.update, task.prepare. "
                        "Use goal.measurement_sources.update to attach "
                        "measurement_source, measurement_cadence, baseline_value, "
                        "or current_value to existing goals by goal_key, "
                        "metric_key, key, id, or title. For manual dashboard "
                        "entry sources, set provider='manual' with "
                        "params.preserve_workspace_manual=true so workspace "
                        "goals do not default back to internal task-impact "
                        "measurement. "
                        "Prefer capability_binding.upsert with "
                        "capability_type='capability' and capability_id such as "
                        "'workspace.task', 'workspace.knowledge', or "
                        "'automation.manage' when the request maps to a Manor "
                        "runtime BusinessCapability. Use direct tool_name only "
                        "for narrow custom tools. Do not use action labels such "
                        "as 'publish' as capability_type; use 'action' for "
                        "plain action keys. "
                        "For external platforms/MCP integrations use "
                        "capability_binding.upsert with payload.binding fields "
                        "owner_scope (workspace_agent/service/agent/task), "
                        "capability_type='mcp', integration_key='<server_key>'. "
                        "Only include allowed_tools when the user wants to "
                        "narrow that server's actions. Add "
                        "rule.add patches for approval/review requirements."
                    ),
                },
                "user_confirmation": {
                    "type": "boolean",
                    "description": "Must be true to apply a draft after the user confirms.",
                },
            },
            "required": ["action"],
        },
    },
}


WORKSPACE_RESOLVE_HITL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_resolve_hitl",
        "description": (
            "Resolve an open workspace chat HITL request when the user's latest "
            "message is semantically answering it. Use the Open Workspace HITL "
            "Requests context to choose the target; do not call this for unrelated "
            "chat or unclear intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The pending_action message_id from Open Workspace HITL Requests.",
                },
                "hitl_id": {
                    "type": "string",
                    "description": "The HITL id/draft_id/review_id when message_id is not available.",
                },
                "action": {
                    "type": "string",
                    "description": "The user's intended decision, e.g. approve, reject, confirm, cancel.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short note explaining the user's decision.",
                },
                "payload": {
                    "type": "object",
                    "description": "Optional structured payload for future HITL kinds.",
                },
            },
            "required": ["action"],
        },
    },
}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _pending_hitl_id(pending_action: dict[str, Any]) -> str:
    operation = pending_action.get("operation") if isinstance(pending_action.get("operation"), dict) else {}
    return str(
        pending_action.get("draft_id")
        or pending_action.get("approval_token")
        or pending_action.get("review_id")
        or pending_action.get("step_id")
        or operation.get("draft_id")
        or ""
    ).strip()


def _normalise_hitl_action(action: Any) -> str:
    raw = str(action or "").strip().lower()
    approvals = {"approve", "approved", "yes", "accept", "confirm", "confirmed", "ok"}
    rejections = {"reject", "rejected", "no", "deny", "decline", "cancel", "cancelled", "canceled", "stop"}
    if raw in approvals:
        return "approve"
    if raw in rejections:
        return "reject"
    return raw


async def _workspace_agent_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    action = str(kwargs.get("action") or "").strip()
    raw_params = kwargs.get("params") or {}
    params = raw_params if isinstance(raw_params, dict) else {}
    if not action:
        return _dumps({"error": "action is required"})

    if action == "search":
        from packages.core.ai.runtime import runtime_workspace_search

        return await runtime_workspace_search(
            entity_id=entity_id,
            workspace_id=workspace_id,
            **params,
        )

    handlers = {
        "create_task": _workspace_create_task_handler,
        "update_task_runtime": _workspace_update_task_runtime_handler,
        "list_knowledge": _workspace_list_knowledge_handler,
        "create_knowledge_folder": _workspace_create_knowledge_folder_handler,
        "add_knowledge_documents": _workspace_add_knowledge_documents_handler,
        "remove_knowledge_document": _workspace_remove_knowledge_document_handler,
        "update_knowledge_policy": _workspace_update_knowledge_policy_handler,
        "add_rule": _workspace_add_rule_handler,
        "get_goal_status": _workspace_get_goal_status_handler,
        "update_goal_value": _workspace_update_goal_value_handler,
        "operation": _workspace_operation_handler,
        "request_strategist_review": _workspace_request_strategist_review_handler,
        "delegate_service": _workspace_delegate_service_handler,
        "get_goal_status": _workspace_get_goal_status_handler,
        "update_goal_value": _workspace_update_goal_value_handler,
    }
    handler = handlers.get(action)
    if not handler:
        return _dumps({"error": f"unsupported workspace_agent action: {action}"})

    # Stamp the active agent persona so task logs/comments it writes are
    # attributed to that agent in the activity UI (not a generic
    # "workspace-agent"). The id is injected by the runtime tool harness.
    actor_kwargs: dict[str, Any] = {}
    if action in ("create_task", "update_task_runtime"):
        actor_agent_id = str(kwargs.get("_agent_id_from_context") or "").strip()
        if actor_agent_id:
            actor_kwargs["actor_agent_id"] = actor_agent_id

    return await handler(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        **actor_kwargs,
        **params,
    )


async def _workspace_create_task_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    actor_agent_id: str | None = None,
    **kwargs: Any,
) -> str:
    actor_agent_id = actor_agent_id or str(kwargs.get("_agent_id_from_context") or "").strip() or None
    actor_kwargs = {"actor_agent_id": actor_agent_id} if actor_agent_id else {}
    return await runtime_workspace_create_task_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
        **actor_kwargs,
        params=kwargs,
    )


async def _workspace_update_task_runtime_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    actor_agent_id: str | None = None,
    **kwargs: Any,
) -> str:
    actor_agent_id = actor_agent_id or str(kwargs.get("_agent_id_from_context") or "").strip() or None
    actor_kwargs = {"actor_agent_id": actor_agent_id} if actor_agent_id else {}
    return await runtime_workspace_update_task_runtime_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
        **actor_kwargs,
        params=kwargs,
    )


async def _workspace_list_knowledge_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_list_knowledge_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        params=kwargs,
    )


async def _workspace_create_knowledge_folder_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_create_knowledge_folder_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_add_knowledge_documents_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_add_knowledge_documents_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_remove_knowledge_document_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_remove_knowledge_document_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_update_knowledge_policy_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_update_knowledge_policy_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_operation_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_operation_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_resolve_hitl_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    message_id = str(kwargs.get("message_id") or "").strip()
    hitl_id = str(kwargs.get("hitl_id") or "").strip()
    action = _normalise_hitl_action(kwargs.get("action"))
    note = str(kwargs.get("note") or "").strip()
    if not conversation_id or not workspace_id or not entity_id:
        return _dumps({"error": "workspace_resolve_hitl requires workspace conversation context"})
    if not user_id:
        return _dumps({"error": "workspace_resolve_hitl requires a user_id"})
    if action not in {"approve", "reject"}:
        return _dumps({
            "error": "unsupported_hitl_action",
            "message": "This tool currently supports approve/reject workspace operation reviews.",
            "action": action,
        })

    from sqlalchemy import select
    from packages.core.database import async_session
    from packages.core.models.task import Message
    from packages.core.services.workspace_operation_service import resolve_workspace_operation_review
    from packages.core.workspace_chat import service as workspace_chat_service

    async with async_session() as db:
        rows = list((await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.pending_action.isnot(None),
                Message.resolved_at.is_(None),
            )
            .order_by(Message.created_at.desc())
            .limit(25)
        )).scalars().all())
        candidates = [
            row for row in rows
            if isinstance(row.pending_action, dict)
            and row.pending_action.get("kind") == "workspace_operation_review"
        ]
        if not candidates:
            return _dumps({"resolved": False, "reason": "no_open_workspace_operation_review"})

        primary = next((row for row in candidates if row.id == message_id), None)
        if primary is None and hitl_id:
            primary = next(
                (
                    row for row in candidates
                    if _pending_hitl_id(row.pending_action or {}) == hitl_id
                ),
                None,
            )
        if primary is None:
            return _dumps({
                "resolved": False,
                "reason": "target_not_found",
                "available": [
                    {
                        "message_id": row.id,
                        "hitl_id": _pending_hitl_id(row.pending_action or {}),
                    }
                    for row in candidates[:10]
                ],
            })

        primary_hitl_id = _pending_hitl_id(primary.pending_action or {})
        targets = [
            row for row in candidates
            if primary_hitl_id and _pending_hitl_id(row.pending_action or {}) == primary_hitl_id
        ] or [primary]

        result = await resolve_workspace_operation_review(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            hitl_id=primary_hitl_id,
            action=action,
        )
        if result is None:
            await db.rollback()
            return _dumps({"resolved": False, "reason": "workspace_operation_review_rejected_by_service"})

        resolution = {"choice": str(result.get("action") or action)}
        if note:
            resolution["note"] = note
        resolved_message_ids: list[str] = []
        for target in targets:
            resolved = await workspace_chat_service.resolve_pending_action(
                db,
                message_id=target.id,
                user_id=user_id,
                resolution=resolution,
            )
            if resolved is not None:
                resolved_message_ids.append(target.id)

        db.add(Message(
            conversation_id=conversation_id,
            role="system",
            content=str(result.get("message") or "Workspace operation review resolved."),
            author_kind="system",
            message_kind="system",
            refs=[
                {"type": "message", "id": primary.id},
                {"type": "workspace_operation_draft", "id": result.get("draft_id")},
            ],
        ))
        await db.commit()
        return _dumps({
            "resolved": True,
            "kind": "workspace_operation_review",
            "action": resolution["choice"],
            "draft_id": result.get("draft_id") or primary_hitl_id,
            "message_ids": resolved_message_ids,
            "message": result.get("llm_message") or result.get("message"),
        })


async def _workspace_add_rule_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_add_rule_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
        params=kwargs,
    )


async def _workspace_delegate_service_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_delegate_service_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
        params=kwargs,
    )


async def _workspace_request_strategist_review_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    return await runtime_workspace_request_strategist_review_action(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
        params=kwargs,
    )


async def _workspace_get_goal_status_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    params = dict(kwargs)
    if workspace_id:
        params.setdefault("workspace_id", workspace_id)
    return await runtime_get_goal_status_action(
        entity_id=entity_id,
        params=params,
    )


async def _workspace_update_goal_value_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    params = dict(kwargs)
    if workspace_id:
        params.setdefault("workspace_id", workspace_id)
    return await runtime_update_goal_value_action(
        entity_id=entity_id,
        params=params,
    )


def get_tools():
    return [
        (WORKSPACE_AGENT_SCHEMA, _workspace_agent_handler),
        (WORKSPACE_CREATE_TASK_SCHEMA, _workspace_create_task_handler),
        (WORKSPACE_UPDATE_TASK_RUNTIME_SCHEMA, _workspace_update_task_runtime_handler),
        (WORKSPACE_LIST_KNOWLEDGE_SCHEMA, _workspace_list_knowledge_handler),
        (WORKSPACE_CREATE_KNOWLEDGE_FOLDER_SCHEMA, _workspace_create_knowledge_folder_handler),
        (WORKSPACE_ADD_KNOWLEDGE_DOCUMENTS_SCHEMA, _workspace_add_knowledge_documents_handler),
        (WORKSPACE_REMOVE_KNOWLEDGE_DOCUMENT_SCHEMA, _workspace_remove_knowledge_document_handler),
        (WORKSPACE_UPDATE_KNOWLEDGE_POLICY_SCHEMA, _workspace_update_knowledge_policy_handler),
        (WORKSPACE_OPERATION_SCHEMA, _workspace_operation_handler),
        (WORKSPACE_RESOLVE_HITL_SCHEMA, _workspace_resolve_hitl_handler),
        (WORKSPACE_ADD_RULE_SCHEMA, _workspace_add_rule_handler),
        (WORKSPACE_REQUEST_STRATEGIST_REVIEW_SCHEMA, _workspace_request_strategist_review_handler),
    ]
