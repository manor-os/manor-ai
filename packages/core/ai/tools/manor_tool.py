"""
Manor Platform composite tool — single gateway to all platform actions.

Same pattern as manor-multi-agent: action + params routing with keyword search.
Dispatches to service layer functions instead of tool pool.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.skills import (
    runtime_delete_skill,
    runtime_generate_skill,
    runtime_get_skill,
    runtime_invoke_skill,
    runtime_list_skills,
    runtime_skill_descriptor_detail_payload,
    runtime_skill_descriptor_list_payload,
    runtime_skill_descriptors_from_tool_kwargs,
    runtime_update_skill,
)
from packages.core.ai.runtime.manor_actions import (
    runtime_manor_bind_channel,
    runtime_manor_add_task_comment,
    runtime_manor_assign_task,
    runtime_manor_create_client,
    runtime_manor_create_document_folder,
    runtime_manor_create_order,
    runtime_manor_create_task,
    runtime_manor_delete_client,
    runtime_manor_delete_document,
    runtime_manor_delete_document_folder,
    runtime_manor_get_document,
    runtime_manor_get_client,
    runtime_manor_get_order,
    runtime_manor_get_entity_info,
    runtime_manor_get_staff,
    runtime_manor_get_task_details,
    runtime_manor_list_channel_bindings,
    runtime_manor_list_conversations,
    runtime_manor_list_clients,
    runtime_manor_list_integrations,
    runtime_manor_list_notifications,
    runtime_manor_get_dashboard_summary,
    runtime_manor_get_operating_model,
    runtime_manor_get_workspace,
    runtime_manor_get_workspace_activity,
    runtime_manor_get_workspace_agents,
    runtime_manor_get_workspace_daily_summary,
    runtime_manor_get_workspace_dashboard,
    runtime_manor_apply_workspace_operation_patch,
    runtime_manor_cancel_scheduled_job,
    runtime_manor_create_scheduled_job,
    runtime_manor_list_agents,
    runtime_manor_list_document_folders,
    runtime_manor_list_document_groups,
    runtime_manor_list_documents,
    runtime_manor_list_scheduled_jobs,
    runtime_manor_list_orders,
    runtime_manor_list_roles,
    runtime_manor_list_staff,
    runtime_manor_list_task_categories,
    runtime_manor_list_tasks,
    runtime_manor_list_workspaces,
    runtime_manor_list_workspace_artifacts,
    runtime_manor_move_document_folder,
    runtime_manor_move_documents_to_folder,
    runtime_manor_rename_document_folder,
    runtime_manor_run_scheduled_job_now,
    runtime_manor_search_documents,
    runtime_manor_send_email,
    runtime_manor_start_workspace_draft,
    runtime_manor_toggle_scheduled_job,
    runtime_manor_unbind_channel,
    runtime_manor_update_client,
    runtime_manor_update_order,
    runtime_manor_update_task,
    runtime_manor_update_workspace,
    runtime_manor_upload_document,
)
from packages.core.ai.runtime.goal_actions import (
    runtime_get_goal_status_action,
    runtime_update_goal_value_action,
)
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_injected_tool_context_args,
    runtime_allowed_tool_names_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.constants.task import TASK_STATUSES

logger = logging.getLogger(__name__)
_TASK_STATUS_VALUES = ", ".join(TASK_STATUSES.keys())
_TASK_STATUS_PARAM_GUIDANCE = (
    f"For task params.status, pass exactly one of: {_TASK_STATUS_VALUES}. "
    "Use status='pending' for todo/待办/open tasks; do not pass status='todo'. "
    "Use status='in_progress' for doing/进行中 tasks and status='completed' for done/已完成 tasks."
)

# ── Action catalog ──────────────────────────────────────────────────────────

_ACTIONS: dict[str, list[tuple[str, str]]] = {
    "Tasks": [
        ("list_tasks", f"List tasks with status/priority/assignee filters. {_TASK_STATUS_PARAM_GUIDANCE}"),
        ("get_task_details", "Get full details of a task by ID"),
        ("create_task", "Create a new task; optional assignee_id/staff_id/assignee_name/assignee_email/agent_id assigns it immediately"),
        ("update_task", f"General task update: title, description, status, priority, category, deadline, details, assignment, and other supported task fields. {_TASK_STATUS_PARAM_GUIDANCE}"),
        ("assign_task", "Assign an existing task; requires task_id plus assignee_id/staff_id/assignee_name/assignee_email/agent_id"),
        ("add_task_comment", "Post a comment / log entry on a task"),
        ("delete_task", "Delete a task by ID"),
        ("list_task_categories", "List available task categories"),
        ("get_task_health", "SLA report: overdue, blocked, stalled tasks"),
    ],
    "Documents": [
        ("list_documents", "List user-visible Knowledge documents; excludes raw filesystem/system files"),
        ("get_document", "Get document details by ID"),
        ("upload_document", "Upload a document to knowledge base"),
        ("delete_document", "Delete a document"),
        ("search_documents", "Search user-visible Knowledge documents by keyword"),
        ("list_workspace_artifacts", "List AI-generated or saved files associated with a workspace"),
        ("list_document_folders", "List user-visible Knowledge folders"),
        ("create_document_folder", "Create a user-visible Knowledge folder"),
        ("rename_document_folder", "Rename a Knowledge folder"),
        ("move_document_folder", "Move a Knowledge folder under another folder or root"),
        ("delete_document_folder", "Delete a Knowledge folder and all nested contents"),
        ("move_document_to_folder", "Move one Knowledge document into a folder or root"),
        ("move_documents_to_folder", "Move multiple Knowledge documents into a folder or root"),
        ("list_document_groups", "List document groups/collections used for workspace RAG context"),
    ],
    "Agents": [
        ("list_agents", "List all agents (system + custom)"),
        ("get_agent", "Get agent details by ID"),
        ("list_agent_tools", "List tools bound to an agent"),
        ("bind_agent_tool", "Bind a tool to an agent"),
        ("unbind_agent_tool", "Remove tool binding from an agent"),
    ],
    "Communications": [
        ("send_email", "Send email via configured channel"),
        ("list_conversations", "List conversations by agent/user"),
        ("list_notifications", "List notifications with read/unread filter"),
        ("mark_notification_read", "Mark notification as read"),
        ("list_channel_bindings", "List every channel (Telegram/Slack/WhatsApp/…) and which agent it routes to"),
        ("bind_channel", "Route a channel's inbound messages to a specific agent (params: channel_config_id, agent_id)"),
        ("unbind_channel", "Remove an agent binding from a channel — inbound still logs but no agent runs (params: channel_id)"),
    ],
    "Dashboard": [
        ("get_dashboard_summary", "Task completion, satisfaction, time range stats"),
        ("get_system_health", "Task counts by status, staff overview, health score"),
        ("list_token_usage", "Token usage log"),
    ],
    "Workspace": [
        ("list_workspaces", "List all workspaces in the entity"),
        ("get_workspace", "Get workspace details including operating model"),
        ("get_workspace_daily_summary", "Deterministic workspace daily summary data: previous-day outcomes, current health, human handoff items, and today's focus"),
        ("start_workspace_draft", "Use this for user-facing chat requests to create a new workspace. Starts the guided draft flow and returns a /workspaces/new?draft=<id> link."),
        ("update_workspace", "Update workspace name, description, or category"),
        ("get_operating_model", "Get workspace operating model (services, goals, rules)"),
        ("update_operating_model", "Update workspace operating model"),
        ("add_workspace_service", "Add a service to workspace operating model"),
        ("remove_workspace_service", "Remove a service from workspace"),
        ("map_agent_to_service", "Assign an agent to a workspace service"),
        ("unmap_agent_from_service", "Remove agent from a workspace service"),
        ("get_workspace_agents", "Get all agent-service mappings for a workspace"),
        ("get_goal_status", "Get active workspace goal status, current values, and measurement metadata"),
        ("update_goal_value", "Record a verified manual goal measurement; appends goal_measurements and updates current_value"),
        ("update_workspace_goals", "Update workspace goals"),
        ("update_workspace_rules", "Update workspace rules"),
        ("get_workspace_activity", "Get recent workspace activity log"),
        ("get_workspace_dashboard", "Get workspace dashboard stats"),
    ],
    "Entity": [
        ("get_entity_info", "Entity details: name, usage, plan"),
        ("list_integrations", "List configured integrations plus current agent readiness"),
        ("list_ready_integrations", "List integrations/MCP servers the current user can use now"),
        ("list_users", "List users in entity"),
    ],
    "Team": [
        ("list_staff", "List team members with kind (employee/contractor/vendor/external) and role filters"),
        ("get_staff", "Get staff member details by ID"),
        ("list_roles", "List entity roles with permission sets and head-counts"),
    ],
    "Clients": [
        ("list_clients", "List clients with name/email/status filters"),
        ("get_client", "Get client details by ID"),
        ("create_client", "Create a new client"),
        ("update_client", "Update client fields"),
        ("delete_client", "Soft-delete a client by ID"),
    ],
    "Orders": [
        ("list_orders", "List business orders with status filters"),
        ("get_order", "Get order details by ID"),
        ("create_order", "Create a new order"),
        ("update_order", "Update order fields or status"),
    ],
    "Skills": [
        ("list_skills", "List available skills"),
        ("get_skill", "Get skill details"),
        ("create_skill", "Generate a new skill from a description via AI"),
        ("update_skill", "Patch an existing skill by describing what to change"),
        ("delete_skill", "Delete a custom skill"),
        ("invoke_skill", "Execute a skill by name"),
    ],
    "Automations": [
        ("create_scheduled_job", "Create a recurring automation (cron/interval/one-time) with auto-generated skill"),
        ("list_scheduled_jobs", "List all scheduled automations"),
        ("cancel_scheduled_job", "Delete a scheduled job"),
        ("toggle_scheduled_job", "Enable or disable a scheduled job"),
        ("run_scheduled_job_now", "Force-run a scheduled job immediately"),
    ],
}

# Flatten for lookup
_ALL_ACTIONS: dict[str, str] = {}
_ACTION_DEPT: dict[str, str] = {}
for _dept, _actions in _ACTIONS.items():
    for _name, _desc in _actions:
        _ALL_ACTIONS[_name] = _desc
        _ACTION_DEPT[_name] = _dept


def get_manor_action_names() -> set[str]:
    return set(_ALL_ACTIONS.keys())


def _doc_summary(doc: Any, *, details: bool = False) -> dict[str, Any]:
    data = {
        "id": doc.id,
        "name": doc.name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
    }
    if details:
        data.update({
            "mime_type": getattr(doc, "mime_type", None),
            "source": getattr(doc, "source", None),
            "vector_status": getattr(doc, "vector_status", None),
            "folder_id": getattr(doc, "folder_id", None),
            "fs_path": getattr(doc, "fs_path", None),
        })
    return data


def _search_actions(query: str, max_results: int = 8) -> list[dict]:
    query_lower = query.lower()
    scored: list[tuple[int, str, str, str]] = []
    for name, desc in _ALL_ACTIONS.items():
        dept = _ACTION_DEPT[name]
        score = 0
        for word in query_lower.split():
            if word in name.lower():
                score += 3
            elif word in desc.lower():
                score += 1
            elif word in dept.lower():
                score += 1
        if score > 0:
            scored.append((score, name, desc, dept))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"action": name, "description": desc, "department": dept}
        for _, name, desc, dept in scored[:max_results]
    ]


# ── Action handlers (delegate to service layer) ────────────────────────────

async def _dispatch_action(
    action: str,
    params: dict,
    entity_id: str,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    approval_token: str | None = None,
    runtime_envelope: Any | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    allowed_tool_names: set[str] | None = None,
) -> str:
    """Route an action to the appropriate service function."""
    from packages.core.database import async_session

    params = dict(params or {})
    runtime_tool_kwargs = runtime_injected_tool_context_args(
        user_id=user_id,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        runtime_envelope=runtime_envelope,
        allowed_tool_names=allowed_tool_names,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
    )

    try:
        async with async_session() as db:
            # Tasks
            if action == "list_tasks":
                return await runtime_manor_list_tasks(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_task_details":
                return await runtime_manor_get_task_details(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "list_task_categories":
                return await runtime_manor_list_task_categories(db, entity_id=entity_id)

            if action == "create_task":
                return await runtime_manor_create_task(
                    db,
                    entity_id=entity_id,
                    params=params,
                    workspace_id=workspace_id or "",
                )

            if action == "update_task":
                return await runtime_manor_update_task(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    task_id=task_id,
                )

            if action == "assign_task":
                return await runtime_manor_assign_task(
                    db,
                    entity_id=entity_id,
                    params=params,
                    workspace_id=workspace_id or "",
                    task_id=task_id,
                )

            if action == "add_task_comment":
                return await runtime_manor_add_task_comment(
                    db,
                    entity_id=entity_id,
                    params=params,
                    actor_agent_id=getattr(runtime_envelope, "agent_id", None),
                )

            # Documents
            if action == "list_documents":
                return await runtime_manor_list_documents(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "upload_document":
                return await runtime_manor_upload_document(
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                    workspace_id=workspace_id or "",
                    conversation_id=conversation_id,
                    task_id=task_id or params.get("task_id"),
                    approval_token=approval_token,
                )

            if action == "search_documents":
                return await runtime_manor_search_documents(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "list_workspace_artifacts":
                return await runtime_manor_list_workspace_artifacts(
                    db,
                    entity_id=entity_id,
                    params=params,
                    workspace_id=workspace_id or "",
                    task_id=task_id or "",
                )

            if action == "get_document":
                return await runtime_manor_get_document(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "delete_document":
                return await runtime_manor_delete_document(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "list_document_folders":
                return await runtime_manor_list_document_folders(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "create_document_folder":
                return await runtime_manor_create_document_folder(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "rename_document_folder":
                return await runtime_manor_rename_document_folder(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "move_document_folder":
                return await runtime_manor_move_document_folder(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "delete_document_folder":
                return await runtime_manor_delete_document_folder(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action in {"move_document_to_folder", "move_documents_to_folder"}:
                return await runtime_manor_move_documents_to_folder(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "list_document_groups":
                return await runtime_manor_list_document_groups(db, entity_id=entity_id)

            # Agents
            if action == "list_agents":
                return await runtime_manor_list_agents(db, entity_id=entity_id)

            # Dashboard
            if action == "get_dashboard_summary":
                return await runtime_manor_get_dashboard_summary(db, entity_id=entity_id)

            # Workspace
            if action == "list_workspaces":
                return await runtime_manor_list_workspaces(db, entity_id=entity_id)

            if action == "get_workspace":
                return await runtime_manor_get_workspace(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_workspace_daily_summary":
                return await runtime_manor_get_workspace_daily_summary(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id or "",
                    params=params,
                )

            if action == "start_workspace_draft":
                return await runtime_manor_start_workspace_draft(
                    entity_id=entity_id,
                    user_id=user_id or "",
                    params=params,
                )

            if action == "update_workspace":
                return await runtime_manor_update_workspace(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_operating_model":
                return await runtime_manor_get_operating_model(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "update_operating_model":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "operating_model.replace",
                        "payload": {"operating_model": params.get("operating_model", {})},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "add_workspace_service":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "service_role.upsert",
                        "payload": {"service": params.get("service", {})},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "remove_workspace_service":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "service_role.remove",
                        "payload": {"key": params.get("service_key", "")},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "map_agent_to_service":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "agent_mapping.upsert",
                        "payload": {
                            "mapping": {
                                "service_key": params.get("service_key", ""),
                                "agent_id": params.get("agent_id", ""),
                                "custom_prompt": params.get("custom_prompt"),
                            },
                        },
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "unmap_agent_from_service":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "agent_mapping.remove",
                        "payload": {"service_key": params.get("service_key", "")},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "get_workspace_agents":
                return await runtime_manor_get_workspace_agents(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_goal_status":
                return await runtime_get_goal_status_action(
                    entity_id=entity_id,
                    params=params,
                )

            if action == "update_goal_value":
                return await runtime_update_goal_value_action(
                    entity_id=entity_id,
                    params=params,
                )

            if action == "update_workspace_goals":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "goals.replace",
                        "payload": {"goals": params.get("goals", [])},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "update_workspace_rules":
                result = await runtime_manor_apply_workspace_operation_patch(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    params=params,
                    source_action=action,
                    patch={
                        "op": "rules.replace",
                        "payload": {"rules": params.get("rules", [])},
                    },
                )
                await db.commit()
                return json.dumps(result, default=str)

            if action == "get_workspace_activity":
                return await runtime_manor_get_workspace_activity(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_workspace_dashboard":
                return await runtime_manor_get_workspace_dashboard(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            # Entity
            if action == "get_entity_info":
                return await runtime_manor_get_entity_info(db, entity_id=entity_id)

            if action in {"list_integrations", "list_ready_integrations"}:
                return await runtime_manor_list_integrations(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id or "",
                    ready_action=action == "list_ready_integrations",
                )

            # Skills
            if action == "list_skills":
                if runtime_envelope is not None:
                    descriptors = await runtime_skill_descriptors_from_tool_kwargs(
                        db,
                        runtime_tool_kwargs,
                        limit=50,
                    )
                    return json.dumps(
                        runtime_skill_descriptor_list_payload(
                            descriptors or (),
                            category=params.get("category"),
                        ),
                        default=str,
                    )
                skills = await runtime_list_skills(
                    db,
                    entity_id,
                    category=params.get("category"),
                )
                return json.dumps([{"id": s.id, "name": s.name, "slug": s.slug, "description": s.description, "category": s.category} for s in skills], default=str)

            if action == "get_skill":
                if runtime_envelope is not None:
                    skill_key = str(
                        params.get("skill_id")
                        or params.get("id")
                        or params.get("slug")
                        or params.get("name")
                        or ""
                    )
                    descriptors = await runtime_skill_descriptors_from_tool_kwargs(
                        db,
                        runtime_tool_kwargs,
                        limit=50,
                    )
                    return json.dumps(
                        runtime_skill_descriptor_detail_payload(
                            descriptors or (),
                            skill_key=skill_key,
                        ),
                        default=str,
                    )
                skill = await runtime_get_skill(db, params.get("skill_id", ""))
                if not skill:
                    return json.dumps({"error": "Skill not found"})
                return json.dumps({"id": skill.id, "name": skill.name, "description": skill.description, "system_prompt": skill.system_prompt, "tools": skill.tools, "category": skill.category}, default=str)

            if action == "create_skill":
                skill = await runtime_generate_skill(
                    db,
                    prompt=f"{params.get('name', '')}: {params.get('description', '')}",
                    entity_id=entity_id,
                    category=params.get("category"),
                    tags=params.get("tags", []),
                )
                await db.commit()
                return json.dumps({"id": skill.id, "name": skill.name, "created": True}, default=str)

            if action == "update_skill":
                skill = await runtime_update_skill(
                    db,
                    skill_id=params.get("skill_id", ""),
                    change_description=params.get("change_description", ""),
                    entity_id=entity_id,
                )
                await db.commit()
                return json.dumps({"id": skill.id, "name": skill.name, "version": skill.version, "updated": True}, default=str)

            if action == "delete_skill":
                deleted = await runtime_delete_skill(
                    db,
                    skill_id=params.get("skill_id", ""),
                    entity_id=entity_id,
                )
                return json.dumps({"deleted": deleted})

            if action == "invoke_skill":
                result = await runtime_invoke_skill(
                    db,
                    params.get("skill", ""),
                    entity_id,
                    params.get("input", ""),
                    user_id=user_id or None,
                    workspace_id=workspace_id or None,
                    conversation_id=conversation_id or None,
                    task_id=task_id or params.get("task_id") or None,
                )
                return json.dumps(result, default=str)

            # Automations / Scheduled Jobs
            if action == "create_scheduled_job":
                return await runtime_manor_create_scheduled_job(
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                )

            if action == "list_scheduled_jobs":
                return await runtime_manor_list_scheduled_jobs(entity_id=entity_id)

            if action == "cancel_scheduled_job":
                return await runtime_manor_cancel_scheduled_job(
                    entity_id=entity_id,
                    params=params,
                )

            if action == "toggle_scheduled_job":
                return await runtime_manor_toggle_scheduled_job(
                    entity_id=entity_id,
                    params=params,
                )

            if action == "run_scheduled_job_now":
                return await runtime_manor_run_scheduled_job_now(
                    entity_id=entity_id,
                    params=params,
                )

            # Notifications
            if action == "list_notifications":
                return await runtime_manor_list_notifications(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                )

            # Conversations
            if action == "list_conversations":
                return await runtime_manor_list_conversations(db, entity_id=entity_id)

            if action == "send_email":
                return await runtime_manor_send_email(
                    db,
                    entity_id=entity_id,
                    params=params,
                    workspace_id=workspace_id or "",
                    conversation_id=conversation_id,
                )

            # Channel bindings — route a channel's inbound to a specific agent
            if action == "list_channel_bindings":
                return await runtime_manor_list_channel_bindings(db, entity_id=entity_id)

            if action == "bind_channel":
                return await runtime_manor_bind_channel(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "unbind_channel":
                return await runtime_manor_unbind_channel(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            # Team — staff + roles
            if action == "list_staff":
                return await runtime_manor_list_staff(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_staff":
                return await runtime_manor_get_staff(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "list_roles":
                return await runtime_manor_list_roles(db, entity_id=entity_id)

            # Clients
            if action == "list_clients":
                return await runtime_manor_list_clients(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_client":
                return await runtime_manor_get_client(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "create_client":
                return await runtime_manor_create_client(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "update_client":
                return await runtime_manor_update_client(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "delete_client":
                return await runtime_manor_delete_client(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            # Orders
            if action == "list_orders":
                return await runtime_manor_list_orders(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "get_order":
                return await runtime_manor_get_order(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            if action == "create_order":
                return await runtime_manor_create_order(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                )

            if action == "update_order":
                return await runtime_manor_update_order(
                    db,
                    entity_id=entity_id,
                    params=params,
                )

            # Fallback
            return json.dumps({"error": f"Action '{action}' handler not yet implemented. Use search to find alternatives."})

    except ImportError as ie:
        return json.dumps({"error": f"Service not available: {ie}"})
    except Exception as e:
        logger.exception("manor action=%s failed: %s", action, e)
        return json.dumps({"error": f"Action '{action}' failed: {e}"})


# ── Tool schema ──────────────────────────────────────────────────────────────

_HANDLER_CONTEXT_KEYS = {
    "action",
    "query",
    "params",
    "entity_id",
    "user_id",
    "approval_token",
} | RUNTIME_TOOL_CONTEXT_KEYS


def _merge_action_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Accept both params={...} and direct action args like task_id=... ."""
    params = kwargs.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    direct_params = {
        key: value
        for key, value in kwargs.items()
        if key not in _HANDLER_CONTEXT_KEYS
    }
    return {**direct_params, **params}


MANOR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "manor",
        "description": (
            "Execute Manor platform actions. Use action='search' with a short "
            "query first when unsure, then call the returned action with params. "
            "Common direct actions: list_tasks, create_task, assign_task, "
            "list_staff, list_documents, search_documents, list_document_folders, "
            "create_document_folder, move_documents_to_folder, list_ready_integrations, list_agents, "
            "list_workspaces, get_workspace_daily_summary, get_goal_status, update_goal_value, "
            "get_dashboard_summary, create_scheduled_job. "
            "For image/video/audio/document generation, use generate_file rather than Manor. "
            "Use list_documents/search_documents for user-visible Knowledge; "
            "raw filesystem tools are internal and should not be presented as "
            "the user's file list. For delayed or recurring work, always use "
            "create_scheduled_job rather than claiming completion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to execute. Use 'search' to find actions.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query when action='search'.",
                },
                "params": {
                    "type": "object",
                    "description": "Action-specific parameters.",
                    "additionalProperties": True,
                },
                "approval_token": {
                    "type": "string",
                    "description": "One-time token returned after the user approves a user-visible Manor document mutation.",
                },
            },
            "required": ["action"],
        },
    },
}


async def _manor_handler(entity_id: str = "", **kwargs: Any) -> str:
    """Manor composite tool handler."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    action = (kwargs.get("action") or "").strip()
    legacy_create_workspace = action == "create_workspace"
    if legacy_create_workspace:
        # Backward-compatible alias only. Do not expose low-level workspace row
        # creation to chat agents; route older calls into the guided draft flow.
        action = "start_workspace_draft"

    if not action:
        return json.dumps({"error": "action is required"})

    # Search mode — no DB session needed
    if action == "search":
        query = (kwargs.get("query") or "").strip()
        if not query:
            summary = {dept: [a[0] for a in actions] for dept, actions in _ACTIONS.items()}
            return json.dumps({"departments": summary, "total_actions": len(_ALL_ACTIONS)})
        results = _search_actions(query)
        if not results:
            return json.dumps({"matches": [], "query": query, "hint": "Try broader keywords."})
        return json.dumps({"matches": results, "query": query}, ensure_ascii=False)

    # Validate action exists before opening DB
    if action not in _ALL_ACTIONS:
        suggestions = _search_actions(action, max_results=5)
        return json.dumps({
            "error": f"Unknown action: '{action}'",
            "suggestions": [s["action"] for s in suggestions],
        })

    params = _merge_action_params(kwargs)
    if legacy_create_workspace and not params.get("initial_brief"):
        brief_parts = []
        for key, label in (
            ("name", "workspace name"),
            ("description", "description"),
            ("category", "category"),
        ):
            value = str(params.get(key) or "").strip()
            if value:
                brief_parts.append(f"{label}: {value}")
        params["initial_brief"] = "; ".join(brief_parts) or "Create a new workspace from the user's request."
    workspace_id = runtime_context.workspace_id
    conversation_id = runtime_context.conversation_id
    if workspace_id and action != "update_task":
        params.setdefault("workspace_id", workspace_id)
    return await _dispatch_action(
        action,
        params,
        entity_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=runtime_context.task_id or params.get("task_id"),
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        runtime_envelope=runtime_context.runtime_envelope,
        active_user_message=runtime_context.active_user_message,
        manual_skill_selected=runtime_context.manual_skill_selected,
        allowed_tool_names=runtime_allowed_tool_names_from_context(kwargs),
    )


def get_tools():
    return [(MANOR_SCHEMA, _manor_handler)]
