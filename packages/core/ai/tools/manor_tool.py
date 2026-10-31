"""
Manor Platform composite tool — single gateway to all platform actions.

Same pattern as manor-multi-agent: action + params routing with keyword search.
Dispatches to service layer functions instead of tool pool.
"""
from __future__ import annotations

import inspect
import json
import logging
import re

from packages.core.ai.runtime.crm_actions import CRM_ACTIONS, runtime_crm_action
from typing import Any

from packages.core.ai.runtime.skills import (
    runtime_delete_skill,
    runtime_generate_skill,
    runtime_get_skill,
    runtime_invoke_skill_action,
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
    runtime_manor_create_document_folder,
    runtime_manor_create_order,
    runtime_manor_create_task,
    runtime_manor_delete_document,
    runtime_manor_delete_document_folder,
    runtime_manor_get_document,
    runtime_manor_get_order,
    runtime_manor_get_entity_info,
    runtime_manor_get_staff,
    runtime_manor_get_task_details,
    runtime_manor_list_channel_bindings,
    runtime_manor_list_conversations,
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
    runtime_manor_continue_workspace_draft,
    runtime_manor_start_workspace_draft,
    runtime_manor_toggle_scheduled_job,
    runtime_manor_unbind_channel,
    runtime_manor_update_order,
    runtime_manor_update_task,
    runtime_manor_update_workspace,
    runtime_manor_upload_document,
)
from packages.core.ai.runtime.goal_actions import (
    runtime_get_goal_status_action,
    runtime_update_goal_value_action,
)
from packages.core.ai.runtime.stat_actions import ADD_ASSESSED_METRICS_GUIDANCE, STAT_ASSESSMENT_GUIDANCE, runtime_stat_action
from packages.core.ai.runtime.notification_actions import (
    runtime_notify_members_action,
)
from packages.core.ai.runtime.workspace_composite_actions import (
    runtime_workspace_composite_action,
)
from packages.core.ai.runtime.workspace_operation_actions import (
    WORKSPACE_OPERATION_CONFIGURATION_GUIDANCE,
    WORKSPACE_OPERATION_EAGER_CONFIGURATION_GUIDANCE,
)
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_agent_subscription_id_from_context,
    runtime_injected_tool_context_args,
    runtime_trusted_workspace_id_from_context,
    runtime_allowed_tool_names_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.constants.agents import MANOR_AGENT_ID
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
        (
            "list_tasks",
            "List Tasks with all filters in one params object. Plural values such as "
            "statuses/priorities/assignee_ids are ORed; different filters and inclusive "
            "*_after/*_before or priority_min/priority_max ranges are ANDed. Do not call "
            f"once per status or priority. {_TASK_STATUS_PARAM_GUIDANCE}",
        ),
        ("get_task_details", "Get full details of a task by ID"),
        ("create_task", "Create a new task; optional assignee_id/staff_id/assignee_name/assignee_email/agent_id assigns it immediately"),
        ("update_task", f"General task update: title, description, status, priority, category, deadline, details, assignment, and other supported task fields. {_TASK_STATUS_PARAM_GUIDANCE}"),
        ("assign_task", "Assign an existing task; requires task_id plus assignee_id/staff_id/assignee_name/assignee_email/agent_id"),
        ("add_task_comment", "Post a comment / log entry on a task"),
        ("list_task_categories", "List available task categories"),
    ],
    "Documents": [
        (
            "list_documents",
            "List user-visible Knowledge documents with plural metadata filters, inclusive "
            "time/size ranges, exact total, and pagination in one params object; excludes "
            "raw filesystem/system files",
        ),
        ("get_document", "Get document details (params: document_id)"),
        ("upload_document", "Upload a document to knowledge base"),
        ("delete_document", "Delete a document (params: document_id)"),
        (
            "search_documents",
            "Search user-visible documents by name and text (params: query plus "
            "plural filters and inclusive time/size ranges); returns name matches "
            "and content_matches with snippets",
        ),
        ("list_workspace_artifacts", "List AI-generated or saved files associated with a workspace"),
        ("list_document_folders", "List user-visible Knowledge folders"),
        ("create_document_folder", "Create a user-visible Knowledge folder"),
        ("rename_document_folder", "Rename a Knowledge folder"),
        ("move_document_folder", "Move a Knowledge folder under another folder or root"),
        ("delete_document_folder", "Delete a Knowledge folder and all nested contents"),
        (
            "move_document_to_folder",
            "Move one Knowledge document (params: document_id, folder_id — a "
            "folder id from list_document_folders, or 'root' for top level)",
        ),
        (
            "move_documents_to_folder",
            "Move multiple Knowledge documents (params: document_ids, folder_id "
            "— a folder id from list_document_folders, or 'root' for top level)",
        ),
        ("list_document_groups", "List document groups/collections used for workspace RAG context"),
    ],
    "Agents": [
        (
            "list_agents",
            "Query Agents with all filters in one params object. Plural values are "
            "ORed; different filters and inclusive time/deployment-count ranges are "
            "ANDed. Returns exact Agent ids; call this before setting an Automation's "
            "agent_id, then copy one returned active id. Do not call once per status, "
            "category, or Workspace.",
        ),
    ],
    "Communications": [
        (
            "send_email",
            "Send email via a configured account. Pass params.integration_account_id "
            "from list_ready_integrations to choose the sender; omit it for the default.",
        ),
        ("list_conversations", "List conversations by agent/user"),
        ("list_notifications", "List notifications with read/unread filter"),
        (
            "notify_members",
            "Notify team members: reaches each member on their preferred "
            "channels (in-app bell always, plus email/Telegram/etc. per their "
            "notification preferences). Params: title (required), body?, "
            "link?, member_ids? (user ids from list_staff/find_team_members; "
            "omit to notify ALL active members of the entity).",
        ),
        ("list_channel_bindings", "List every channel (Telegram/Slack/WhatsApp/…) and which agent it routes to"),
        ("bind_channel", "Route a channel's inbound messages to a specific agent (params: channel_config_id, agent_id)"),
        ("unbind_channel", "Remove an agent binding from a channel — inbound still logs but no agent runs (params: channel_id)"),
    ],
    "Dashboard": [
        ("get_dashboard_summary", "Task completion, satisfaction, time range stats"),
    ],
    "Workspace": [
        (
            "workspace",
            "Workspace runtime gateway. Pass params.action plus optional "
            "params.params for search, tasks, knowledge, rules, operations, "
            "HITL/blocker resolution, service delegation, goals, and Ledger views. "
            "For persistent workspace changes use params.action='operation' with "
            "params.params={action: get_current|create_draft|patch_draft|validate_draft|"
            "preview_diff|apply_draft|discard_draft, patches: [{op, payload}]}; "
            "an unknown patch op returns the full op vocabulary. Guardrails are "
            "derived from rules: first use get_current (read-only) to identify the "
            "blocking rule_key, rule_type and action/capability patterns, then lift "
            "or soften that rule with rule.remove / rule.update. Never probe rules "
            "by creating drafts or updating an invented rule key. create_draft "
            "requires nonempty patches in the same call; do not create an empty draft first. "
            + WORKSPACE_OPERATION_CONFIGURATION_GUIDANCE,
        ),
        ("list_workspaces", "List all workspaces in the entity"),
        ("get_workspace", "Get workspace details including operating model"),
        ("get_workspace_daily_summary", "Deterministic workspace daily summary data: previous-day outcomes, current health, human handoff items, and today's focus"),
        ("start_workspace_draft", "Use this for user-facing Chat requests to create a new Workspace. Starts the guided draft flow in the current Chat and returns a draft_id."),
        ("continue_workspace_draft", "Update the active Workspace draft in the current Chat (params: draft_id, message). Use this whenever the user adds, removes, or changes its configuration."),
        ("update_workspace", "Update workspace name, description, or category"),
        ("get_operating_model", "Get workspace operating model (services, goals, rules)"),
        ("update_operating_model", "Update workspace operating model"),
        ("add_workspace_service", "Add a service to workspace operating model"),
        ("remove_workspace_service", "Remove a service from workspace"),
        ("map_agent_to_service", "Assign an agent to a workspace service"),
        ("unmap_agent_from_service", "Remove agent from a workspace service"),
        ("get_workspace_agents", "Get all agent-service mappings for a workspace"),
        ("get_goal_status", "Get active workspace goal status, current values, and measurement metadata"),
        ("get_stat_status", "Read workspace metric definitions, latest assessment receipts and source document versions. " + STAT_ASSESSMENT_GUIDANCE),
        ("record_stat_assessment", "Persist an evidence-backed metric assessment and project linked Goals. " + STAT_ASSESSMENT_GUIDANCE),
        ("add_assessed_metrics", "Track audience metrics (video views, comments, likes, followers, active users) that an agent assesses from evidence on a schedule. " + ADD_ASSESSED_METRICS_GUIDANCE),
        ("update_goal_value", "Record a verified manual goal measurement; params must include goal_id (ULID or goal_key) AND numeric value in the same call; appends goal_measurements and updates current_value"),
        ("update_workspace_goals", "Update workspace goals"),
        ("update_workspace_rules", "Update workspace rules"),
        ("get_workspace_activity", "Get recent workspace activity log"),
        ("get_workspace_dashboard", "Get workspace dashboard stats"),
    ],
    "Entity": [
        ("get_entity_info", "Entity details: name, usage, plan"),
        ("list_integrations", "List configured integrations, readiness, and every linked account available to the current user"),
        ("list_ready_integrations", "List callable integrations/MCP servers with all account IDs; defaults are priority only"),
    ],
    "Team": [
        ("list_staff", "List team members with kind (employee/contractor/vendor/external) and role filters"),
        ("get_staff", "Get staff member details by ID"),
        ("list_roles", "List entity roles with permission sets and head-counts"),
    ],
    "Clients": list(CRM_ACTIONS.items()),
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
        (
            "create_scheduled_job",
            "Create a background automation (cron/interval/one-time). Omit params.agent_id to use the calling Agent; to select another Agent, first call list_agents and copy an exact active returned id. Missing, stale, inactive, or unavailable selections fall back to the calling Agent, and a custom Agent cannot select Manor. When enabled, queue an execution Skill; use Agent Initiative instead for proactive Chat delivery",
        ),
        (
            "list_scheduled_jobs",
            "Query scheduled automations with all filters in one params object. Plural "
            "values are ORed; different filters and inclusive time/error ranges are "
            "ANDed. Do not call once per status, Workspace, or Agent.",
        ),
        ("cancel_scheduled_job", "Delete a scheduled job"),
        ("toggle_scheduled_job", "Enable or disable a scheduled job"),
        ("run_scheduled_job_now", "Force-run a scheduled job immediately"),
    ],
}

# Flatten for lookup
_ALL_ACTIONS: dict[str, str] = {}
for _dept, _actions in _ACTIONS.items():
    for _name, _desc in _actions:
        _ALL_ACTIONS[_name] = _desc


def get_manor_action_names() -> set[str]:
    return set(_ALL_ACTIONS.keys())


def manor_action_manifests() -> list[dict[str, Any]]:
    """Catalog projection only: Runtime owns matching, scope and availability."""
    return [
        {"catalog_id": f"manor_action:{name}", "kind": "tool", "name": "manor",
         "description": description,
         "invoke_with": {"tool": "manor", "arguments": {"action": name}}}
        for name, description in _ALL_ACTIONS.items()
        if name in _IMPLEMENTED_ACTIONS
    ]


def _doc_summary(doc: Any, *, details: bool = False) -> dict[str, Any]:
    data = {
        "document_id": doc.id,
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


# ── Action handlers (delegate to service layer) ────────────────────────────

def _unsupported_action_response(action: str) -> str:
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    return json.dumps(tool_error_payload(
        tool_name="manor", code="unsupported_action",
        message="This Manor action has no executable handler. Discover a current operation with search_tools.",
        details={"field": "action", "action": action,
                 "capability_availability": "not_established",
                 "discovery_tool": "search_tools", "action_executed": False},
    ), ensure_ascii=False)


async def _collaborator_refused(
    action: str, params: dict, *, entity_id: str, user_id: str | None, workspace_id: str | None,
) -> str | None:
    """Backstop for the company-wide Manor gateway.

    The runtime authorization gate is the primary boundary. Here an external
    collaborator reaches only the actions whose subject that gate admits for
    them (Workspace work, Tasks, goals, automations), judged the same way.

    A collaborator acts only inside a Workspace: the gate refuses every
    collaborator call without one before the handler runs. A call with no
    Workspace is therefore not a collaborator's and needs no role lookup.
    """
    if not user_id or not workspace_id:
        return None
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services.runtime_authorization.collaborator import collaborator_subject_allowed
    from packages.core.services.runtime_authorization.domain import AuthorizationSubject

    try:
        authorization = classify_runtime_tool("manor", {"action": action, "params": params}).authorization
    except Exception:
        authorization = None
    if authorization is not None and collaborator_subject_allowed(
        AuthorizationSubject.create(authorization.action_key, authorization.capability_id), tool_name="manor",
    ):
        return None
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload
    from packages.core.services.runtime_authorization.collaborator import is_runtime_collaborator

    # The gate cached this call's answer; a direct call reads it once.
    if not await is_runtime_collaborator(user_id, entity_id):
        return None
    return json.dumps(tool_error_payload(
        tool_name="manor", code="collaborator_action_unavailable",
        message="This Manor action reaches the whole company and is not available to external collaborators.",
        details={"field": "action", "action": action, "action_executed": False},
    ), ensure_ascii=False)


async def _dispatch_action(
    action: str,
    params: dict,
    entity_id: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    approval_token: str | None = None,
    runtime_envelope: Any | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    allowed_tool_names: set[str] | None = None,
    runtime_context: Any | None = None,
) -> str:
    """Route an action to the appropriate service function."""
    from packages.core.database import async_session

    params = dict(params or {})
    authorization_workspace_id = (
        runtime_trusted_workspace_id_from_context(runtime_context)
        if runtime_context is not None
        else workspace_id if user_id else None
    )
    runtime_tool_kwargs = runtime_injected_tool_context_args(
        agent_id=agent_id,
        user_id=user_id,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        runtime_envelope=runtime_envelope,
        allowed_tool_names=allowed_tool_names,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
    )
    if workflow_run_id:
        runtime_tool_kwargs["_workflow_run_id_from_context"] = workflow_run_id

    if action == "workspace":
        workspace_action = str(params.get("action") or "").strip()
        raw_workspace_params = params.get("params")
        if isinstance(raw_workspace_params, dict):
            workspace_params = dict(raw_workspace_params)
        else:
            workspace_params = {
                key: value
                for key, value in params.items()
                if key not in {"action", "params", "workspace_id"}
            }
        return await runtime_workspace_composite_action(
            entity_id=entity_id,
            user_id=user_id or "",
            workspace_id=workspace_id or "",
            conversation_id=conversation_id or "",
            action=workspace_action,
            params=workspace_params,
            runtime_tool_kwargs=runtime_tool_kwargs,
        )

    try:
        async with async_session() as db:
            # Tasks
            if action == "list_tasks":
                return await runtime_manor_list_tasks(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                )

            if action == "get_task_details":
                return await runtime_manor_get_task_details(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
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
                    user_id=user_id,
                    actor_agent_id=agent_id,
                    runtime_tool_kwargs=runtime_tool_kwargs,
                )

            if action == "update_task":
                return await runtime_manor_update_task(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                    workspace_id=workspace_id or "",
                    task_id=task_id,
                    runtime_tool_kwargs=runtime_tool_kwargs,
                )

            if action == "assign_task":
                return await runtime_manor_assign_task(
                    db,
                    entity_id=entity_id,
                    params=params,
                    workspace_id=workspace_id or "",
                    task_id=task_id,
                    user_id=user_id,
                    runtime_tool_kwargs=runtime_tool_kwargs,
                )

            if action == "add_task_comment":
                return await runtime_manor_add_task_comment(
                    db,
                    entity_id=entity_id,
                    params=params,
                    actor_agent_id=getattr(runtime_envelope, "agent_id", None),
                    user_id=user_id,
                )

            # Documents
            if action == "list_documents":
                return await runtime_manor_list_documents(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "upload_document":
                return await runtime_manor_upload_document(
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                    workspace_id=authorization_workspace_id or "",
                    conversation_id=conversation_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    approval_token=approval_token,
                    runtime_envelope=runtime_envelope,
                )

            if action == "search_documents":
                return await runtime_manor_search_documents(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                    user_wording=active_user_message,
                )

            if action == "list_workspace_artifacts":
                return await runtime_manor_list_workspace_artifacts(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                    workspace_id=authorization_workspace_id or "",
                    task_id=task_id or "",
                )

            if action == "get_document":
                return await runtime_manor_get_document(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "delete_document":
                return await runtime_manor_delete_document(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    params=params,
                )

            if action == "list_document_folders":
                return await runtime_manor_list_document_folders(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "create_document_folder":
                return await runtime_manor_create_document_folder(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "rename_document_folder":
                return await runtime_manor_rename_document_folder(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "move_document_folder":
                return await runtime_manor_move_document_folder(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "delete_document_folder":
                return await runtime_manor_delete_document_folder(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action in {"move_document_to_folder", "move_documents_to_folder"}:
                return await runtime_manor_move_documents_to_folder(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    params=params,
                )

            if action == "list_document_groups":
                return await runtime_manor_list_document_groups(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                )

            # Agents
            if action == "list_agents":
                from packages.core.constants.agents import is_master_agent

                return await runtime_manor_list_agents(
                    db,
                    entity_id=entity_id,
                    user_id=user_id or None,
                    workspace_id=authorization_workspace_id,
                    require_schedulable_scope=bool(
                        agent_id and not is_master_agent(agent_id)
                    ),
                    params=params,
                )

            # Dashboard
            if action == "get_dashboard_summary":
                return await runtime_manor_get_dashboard_summary(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                )

            # Workspace
            if action == "list_workspaces":
                return await runtime_manor_list_workspaces(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                )

            if action == "get_workspace":
                return await runtime_manor_get_workspace(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
                    params=params,
                )

            if action == "get_workspace_daily_summary":
                return await runtime_manor_get_workspace_daily_summary(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id or "",
                    params=params,
                )

            if action == "start_workspace_draft":
                return await runtime_manor_start_workspace_draft(
                    entity_id=entity_id,
                    user_id=user_id or "",
                    params=params,
                    runtime_envelope=runtime_envelope,
                )

            if action == "continue_workspace_draft":
                return await runtime_manor_continue_workspace_draft(
                    entity_id=entity_id,
                    user_id=user_id or "",
                    params=params,
                )

            if action == "update_workspace":
                return await runtime_manor_update_workspace(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
                    params=params,
                )

            if action == "get_operating_model":
                return await runtime_manor_get_operating_model(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
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
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
                    params=params,
                )

            if action == "get_goal_status":
                return await runtime_get_goal_status_action(
                    entity_id=entity_id,
                    params=params,
                )

            if action in {"get_stat_status", "record_stat_assessment", "add_assessed_metrics"}:
                return await runtime_stat_action(
                    action=action, entity_id=entity_id, user_id=user_id,
                    workspace_id=workspace_id or params.get("workspace_id"), params=params,
                    origin={"workflow_run_id": workflow_run_id, "task_id": task_id, "conversation_id": conversation_id},
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
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
                    params=params,
                )

            if action == "get_workspace_dashboard":
                return await runtime_manor_get_workspace_dashboard(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    runtime_workspace_id=authorization_workspace_id,
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
                    user_id=user_id or None,
                )
                await db.commit()
                return json.dumps({"id": skill.id, "name": skill.name, "created": True}, default=str)

            if action == "update_skill":
                skill = await runtime_update_skill(
                    db,
                    skill_id=params.get("skill_id", ""),
                    change_description=params.get("change_description", ""),
                    entity_id=entity_id,
                    user_id=user_id or None,
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
                skill_runtime_context = runtime_context or runtime_tool_call_context_from_kwargs(
                    runtime_tool_kwargs,
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=authorization_workspace_id or "",
                    conversation_id=conversation_id,
                    task_id=task_id,
                    runtime_envelope=runtime_envelope,
                )
                return await runtime_invoke_skill_action(
                    entity_id=entity_id,
                    skill_id=params.get("skill_id") or params.get("skill", ""),
                    input_text=params.get("input", ""),
                    skill_params=params.get("params"),
                    runtime_context=skill_runtime_context,
                    user_id=skill_runtime_context.user_id,
                    conversation_id=skill_runtime_context.conversation_id,
                )

            # Automations / Scheduled Jobs
            if action == "create_scheduled_job":

                caller_agent_id = agent_id or ""
                if runtime_context is not None:
                    caller_agent_id = agent_id or MANOR_AGENT_ID
                return await runtime_manor_create_scheduled_job(
                    entity_id=entity_id,
                    params=params,
                    caller_agent_id=caller_agent_id,
                    caller_agent_subscription_id=(
                        runtime_agent_subscription_id_from_context(runtime_context)
                        if runtime_context is not None
                        else None
                    ),
                    user_id=user_id,
                    workspace_id=authorization_workspace_id or "",
                    conversation_id=conversation_id,
                    task_id=task_id,
                )

            if action == "list_scheduled_jobs":
                return await runtime_manor_list_scheduled_jobs(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                    runtime_kwargs=runtime_tool_kwargs,
                )

            if action == "cancel_scheduled_job":
                return await runtime_manor_cancel_scheduled_job(
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                    runtime_kwargs=runtime_tool_kwargs,
                )

            if action == "toggle_scheduled_job":
                return await runtime_manor_toggle_scheduled_job(
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                    runtime_kwargs=runtime_tool_kwargs,
                )

            if action == "run_scheduled_job_now":
                return await runtime_manor_run_scheduled_job_now(
                    entity_id=entity_id,
                    user_id=user_id,
                    workspace_id=authorization_workspace_id,
                    params=params,
                    runtime_kwargs=runtime_tool_kwargs,
                )

            # Notifications
            if action == "list_notifications":
                return await runtime_manor_list_notifications(
                    db,
                    entity_id=entity_id,
                    params=params,
                    user_id=user_id,
                )

            if action == "notify_members":
                return await runtime_notify_members_action(
                    entity_id=entity_id,
                    params=params,
                    workspace_id=authorization_workspace_id,
                    agent_id=getattr(runtime_envelope, "agent_id", None),
                )

            # Conversations
            if action == "list_conversations":
                return await runtime_manor_list_conversations(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                )

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
                return await runtime_manor_list_channel_bindings(
                    db, entity_id=entity_id, user_id=user_id,
                )

            if action == "bind_channel":
                return await runtime_manor_bind_channel(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                    params=params,
                )

            if action == "unbind_channel":
                return await runtime_manor_unbind_channel(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
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

            # CRM actions share the validated HTTP service and current actor.
            if action in CRM_ACTIONS:
                return await runtime_crm_action(
                    db, action=action, params=params, entity_id=entity_id,
                    user_id=user_id, workspace_id=workspace_id,
                    runtime_kwargs=runtime_tool_kwargs,
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

            return _unsupported_action_response(action)

    except ImportError as ie:
        return json.dumps({"error": f"Service not available: {ie}"})
    except Exception as e:
        logger.exception("manor action=%s failed: %s", action, e)
        return json.dumps({"error": f"Action '{action}' failed: {e}"})


# ── Implemented-action set ──────────────────────────────────────────────────
#
# The dispatcher's own `if action == "..."` chain is the source of truth for
# what is callable. Contract tests require an exact match with the catalog.

def _implemented_action_names() -> frozenset[str]:
    try:
        source = inspect.getsource(_dispatch_action)
    except (OSError, TypeError):  # no source available — fail open
        return frozenset(_ALL_ACTIONS)
    names = set(re.findall(r'action == "([a-z_]+)"', source))
    for group in re.findall(r"action in \{([^}]*)\}", source):
        names.update(re.findall(r'"([a-z_]+)"', group))
    names.update(CRM_ACTIONS)
    return frozenset(names)


_IMPLEMENTED_ACTIONS: frozenset[str] = _implemented_action_names()

# Accepted before the catalog check: the catalog search and a legacy alias.
_GATEWAY_ACTIONS = frozenset({"search", "create_workspace"})


def manor_action_is_callable(action: str) -> bool:
    """Whether ``manor`` has a handler for ``action``; anything else runs nothing."""
    return action in _GATEWAY_ACTIONS or (action in _ALL_ACTIONS and action in _IMPLEMENTED_ACTIONS)


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
            "Manor platform (incl. Manor Calendar, local Knowledge saves): action='search' finds actions. "
            "action='workspace' uses nested params.action. "
            "App targets/Workflow attachments use configuration drafts; author/run Workflows via dedicated actions. "
            "CRM uses company-scoped client/contact/activity/deal/followup actions. Followups/email activities are records; Ledger links stay in the current Workspace. "
            "Use dedicated actions for ScheduledJobs/Agent Initiatives, never action='workspace'. "
            "search_documents finds documents by name and text; generate_file creates artifacts. "
            "create_agent_initiative is only for private Agent/Manor Chat. "
            "Workspace recurrence uses create_scheduled_job; ScheduledJobs lack conversational delivery, mute, quiet-hours and Initiative history. "
            "Use exact active params.agent_id from action='list_agents'; omit for the calling Agent."
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
                    "description": "search: English keywords in any user language.",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action parameters. Workspace Task: action='workspace', "
                        "params={action:'create_task',params:{title,description,"
                        "runtime_instructions,rules,start}}. description holds objective, "
                        "filename and deliverables; runtime_instructions holds task "
                        "rules and output language; start=true runs now. A drafted "
                        "Task: params={action:'start_task',params:{task_id}}. "
                        "Workspace input: action='workspace', params={action:'resolve_hitl',"
                        "params:{message_id,action:'respond',response:'the user reply'}}. "
                        "hitl_id if no message_id; payload.fields/answers "
                        "and payload.attachments hold structured input/file refs. "
                        "approve/reject only decide approvals (optional note). "
                        "Workspace config: action='workspace', params={action:'operation',"
                        "params:{action:'create_draft',patches:[{op,payload}]}}. "
                        + WORKSPACE_OPERATION_EAGER_CONFIGURATION_GUIDANCE
                        + "Rules: rule.add for new ones, rule.update for existing ones. "
                    ),
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

    acting_user_id = runtime_context.user_id
    if (
        acting_user_id is None
        and "_user_id_from_context" not in kwargs
        and "_runtime_envelope_from_context" not in kwargs
    ):
        # Backward-compatible direct/internal calls may still pass ``user_id``.
        # Runtime tool calls always carry an injected context key, even when
        # anonymous, so model-supplied top-level values cannot reach this path.
        acting_user_id = str(kwargs.get("user_id") or "").strip() or None
    if refused := await _collaborator_refused(
        action, _merge_action_params(kwargs), entity_id=entity_id, user_id=acting_user_id,
        workspace_id=runtime_context.workspace_id,
    ):
        return refused

    # The Manor platform's own catalog: Manor actions, Workspace actions and
    # operations (inside a Workspace) and Manor's MCP servers. search_tools
    # finds general tools, third-party integrations and Skills.
    if action == "search":
        from packages.core.ai.runtime.manor_capability_catalog import (
            runtime_manor_mcp_scope,
            runtime_manor_search,
        )

        # Only the trusted run context scopes the search; params carry the
        # model's query words, never identity, mode or provider routing.
        search_params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else {}
        return await runtime_manor_search(
            entity_id=entity_id,
            user_id=acting_user_id or "",
            workspace_id=runtime_context.workspace_id if isinstance(runtime_context.workspace_id, str) else None,
            query=kwargs.get("query") or search_params.get("query"),
            queries=kwargs.get("queries") or search_params.get("queries"),
            max_results=kwargs.get("max_results") or search_params.get("max_results"),
            mcp_tool_in_scope=runtime_manor_mcp_scope(
                runtime_context.runtime_envelope, runtime_context.allowed_tool_names,
            ),
        )

    if not manor_action_is_callable(action):
        return _unsupported_action_response(action)

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
        user_id=acting_user_id,
        agent_id=runtime_context.agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=runtime_context.task_id,
        workflow_run_id=runtime_context.workflow_run_id,
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        runtime_envelope=runtime_context.runtime_envelope,
        active_user_message=runtime_context.active_user_message,
        manual_skill_selected=runtime_context.manual_skill_selected,
        allowed_tool_names=runtime_allowed_tool_names_from_context(kwargs),
        runtime_context=runtime_context,
    )


def get_tools():
    return [(MANOR_SCHEMA, _manor_handler)]
