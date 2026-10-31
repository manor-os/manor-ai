"""First-party Workflow MCP-style tools for Manor agents.

There are two related surfaces:

* Authoring tools let an authenticated Manor agent create, inspect, validate,
  edit, deploy, test, and delete workflow definitions in its own entity.
* Runtime tools let an agent discover and call workflows explicitly published
  through an enabled ``mcp`` binding.

The visual editor's AI Edit and ``ai_edit_workflow`` both call
``services.workflow_generator``. This module is only the Agent/ToolPool
adapter; workflow semantics stay in the shared service and runner layers.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from contextvars import ContextVar
from time import perf_counter
from typing import Any

from packages.core.database import async_session
from packages.core.services.workflow_run_trace import visible_workflow_tags


_WORKFLOW_TOOL_DEPTH: ContextVar[int] = ContextVar("workflow_tool_depth", default=0)
_WORKFLOW_PROJECT_STATE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WORKFLOW_PROJECT_STATE_PATH_LIMIT = 24
_WORKFLOW_PROJECT_STATE_PATH_DEPTH = 8
_WORKFLOW_PROJECT_EXECUTION_ERROR_LIMIT = 1000


def _schema(
    name: str,
    description: str,
    properties: dict[str, dict] | None = None,
    required: list[str] | None = None,
) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


WORKFLOW_REF = {
    "workflow": {
        "type": "string",
        "description": "Workflow id or exact workflow name in the current Manor entity.",
    },
}

LIST_WORKFLOWS_SCHEMA = _schema(
    "list_workflows",
    "List workflows published as callable Manor Agent tools through an active MCP binding.",
    {
        "target_workspace_id": {
            "type": "string",
            "description": "Optional Workspace whose published workflow tools should be listed.",
        },
    },
)
LIST_WORKSPACE_FLOWS_SCHEMA = _schema(
    "list_workspace_flows",
    "List user-facing Flows installed in Workspaces the current user can access.",
    {
        "query": {
            "type": "string",
            "description": "Optional Flow title, slug, or Workspace name filter.",
        },
    },
)
START_WORKSPACE_FLOW_SCHEMA = _schema(
    "start_workspace_flow",
    "Start one user-facing Workspace Flow. In Global Manor Chat this requires Flows mode.",
    {
        "binding_id": {"type": "string", "description": "Binding id returned by list_workspace_flows."},
        "flow": {"type": "string", "description": "Flow title or stable slug when binding_id is unavailable."},
        "target_workspace": {"type": "string", "description": "Workspace id or exact name. Required for ambiguous Flow names."},
        "inputs": {"type": "object", "description": "Optional structured starter input values."},
        "source_brief": {"type": "string", "description": "The complete user request and constraints."},
    },
)
RUN_WORKFLOW_SCHEMA = _schema(
    "run_workflow",
    "Run a workflow published as a Manor Agent tool. Use list_workflows first.",
    {
        **WORKFLOW_REF,
        "inputs": {"type": "object", "description": "Trigger input variables."},
        "target_workspace_id": {
            "type": "string",
            "description": "Optional Workspace that supplies Knowledge, connectors, approvals, and runtime context.",
        },
    },
    ["workflow"],
)
LIST_DEFINITIONS_SCHEMA = _schema(
    "list_workflow_definitions",
    (
        "Query editable workflow definitions in one call, including drafts and "
        "inactive workflows. Values within a plural filter are ORed; different "
        "filters and inclusive ranges are ANDed before pagination. Total is exact."
    ),
    {
        "query": {"type": "string", "description": "Optional name/description search."},
        "status": {"type": "string", "description": "Legacy single exact status filter."},
        "statuses": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
            "description": "Match any listed status (OR).",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 30,
            "uniqueItems": True,
        },
        "workspace_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "created_by_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "active": {"type": "boolean"},
        "version_min": {"type": "integer", "minimum": 1},
        "version_max": {"type": "integer", "minimum": 1},
        **{
            field: {"type": "string", "format": "date-time"}
            for field in (
                "created_after",
                "created_before",
                "updated_after",
                "updated_before",
            )
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
)
GET_WORKFLOW_SCHEMA = _schema(
    "get_workflow",
    "Get a complete workflow definition including nodes, edges, variables, metadata, version, and validation.",
    WORKFLOW_REF,
    ["workflow"],
)
CREATE_WORKFLOW_SCHEMA = _schema(
    "create_workflow",
    "Create a complete Manor workflow. Provide either a natural-language prompt (recommended for conversation) or an exact steps graph. The graph is validated before saving.",
    {
        "name": {"type": "string", "description": "Workflow name. Optional when prompt is provided."},
        "prompt": {"type": "string", "description": "Natural-language workflow design request. Uses the same AI engine as canvas AI Edit."},
        "steps": {"type": "array", "items": {"type": "object"}, "description": "Exact canonical node graph; use instead of prompt for deterministic authoring."},
        "description": {"type": "string"},
        "icon": {"type": "string", "default": "flow"},
        "variables": {"type": "object"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "allow_invalid": {"type": "boolean", "default": False, "description": "Save an incomplete draft graph despite validation errors."},
    },
)
AI_EDIT_WORKFLOW_SCHEMA = _schema(
    "ai_edit_workflow",
    "Edit an existing workflow from a natural-language request using the same AI graph editor as the Flow canvas. Returns the full updated graph and saves it by default.",
    {
        **WORKFLOW_REF,
        "prompt": {"type": "string", "description": "Requested change to the workflow."},
        "save": {"type": "boolean", "default": True, "description": "Persist the generated graph. Set false for preview."},
        "update_name": {"type": "boolean", "default": False, "description": "Also replace the current workflow name with the generated name."},
        "expected_version": {"type": "integer", "description": "Reject saving if the workflow has changed since this version."},
        "allow_invalid": {"type": "boolean", "default": False},
    },
    ["workflow", "prompt"],
)
UPDATE_WORKFLOW_SCHEMA = _schema(
    "update_workflow",
    "Update workflow metadata, variables, or the complete node graph. Use expected_version to avoid overwriting another editor.",
    {
        **WORKFLOW_REF,
        "name": {"type": "string"},
        "description": {"type": "string"},
        "icon": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object"}},
        "variables": {"type": "object"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "is_active": {"type": "boolean"},
        "status": {"type": "string"},
        "expected_version": {"type": "integer"},
        "allow_invalid": {"type": "boolean", "default": False},
    },
    ["workflow"],
)
VALIDATE_WORKFLOW_SCHEMA = _schema(
    "validate_workflow",
    "Validate a saved workflow or an unsaved steps graph. Checks entry node, node ids/types, edges, and reachability without executing it.",
    {
        **WORKFLOW_REF,
        "steps": {"type": "array", "items": {"type": "object"}},
    },
)
DEPLOY_WORKFLOW_SCHEMA = _schema(
    "deploy_workflow",
    "Deploy a valid workflow by creating a binding. Use trigger_type='mcp' to publish it as a callable Manor Agent tool.",
    {
        "workflow": {
            "type": "string",
            "description": (
                "Workflow id, exact workflow name, or the Blueprint slug of a Flow "
                "installed by a Workspace Blueprint in the target workspace."
            ),
        },
        "trigger_type": {
            "type": "string",
            "enum": ["mcp", "manual", "webhook", "event", "workspace_event", "schedule"],
            "default": "mcp",
        },
        "target_workspace_id": {"type": "string", "description": "Optional target workspace id."},
        "name": {"type": "string", "description": "Optional deployment label."},
        "description": {"type": "string", "description": "Description exposed by an MCP deployment."},
        "trigger_config": {"type": "object"},
        "variables": {"type": "object"},
        "cron": {"type": "string", "description": "Cron expression for a recurring schedule."},
        "run_at": {
            "type": "string",
            "description": "Timezone-aware ISO time for a one-shot schedule instead of cron; a past time runs on the next scheduler tick.",
        },
        "timezone": {"type": "string", "default": "UTC"},
        "inputs": {"type": "object", "description": "Declared run input values for a scheduled workflow."},
        "stop_after": {"type": "string", "description": "Exclusive timezone-aware end of the schedule."},
        "owner_project_id": {"type": "string", "description": "Optional Workspace WorkflowProject owning this runtime schedule."},
        "idempotency_key": {"type": "string", "description": "Stable execution key to prevent duplicate schedules on retry."},
    },
    ["workflow"],
)
DELETE_WORKFLOW_SCHEMA = _schema(
    "delete_workflow",
    "Delete a workflow definition. Refuses while deployments exist unless delete_bindings is explicitly true.",
    {
        **WORKFLOW_REF,
        "delete_bindings": {"type": "boolean", "default": False},
    },
    ["workflow"],
)
TEST_WORKFLOW_SCHEMA = _schema(
    "test_workflow",
    "Run any valid workflow definition inline for testing, without requiring an MCP deployment.",
    {
        **WORKFLOW_REF,
        "inputs": {"type": "object"},
    },
    ["workflow"],
)
TEST_NODE_SCHEMA = _schema(
    "test_workflow_node",
    "Execute one canonical workflow node in isolation with test inputs and return output, errors, and duration.",
    {
        "step": {"type": "object", "description": "Canonical workflow step definition."},
        "inputs": {"type": "object", "description": "Test workflow variables available to the node."},
    },
    ["step"],
)
LIST_RUNS_SCHEMA = _schema(
    "list_workflow_runs",
    (
        "Query workflow execution history in one call. Values within a plural "
        "filter are ORed; different filters and inclusive ranges are ANDed before "
        "pagination. Total is exact."
    ),
    {
        "workflow": {"type": "string", "description": "Optional workflow id or exact name."},
        "workflow_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "target_workspace_id": {"type": "string"},
        "workspace_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "binding_id": {"type": "string"},
        "binding_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "status": {"type": "string", "description": "Legacy single status filter."},
        "statuses": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
        },
        "trigger_sources": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
        },
        "started_by_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "attempt_number_min": {"type": "integer", "minimum": 1},
        "attempt_number_max": {"type": "integer", "minimum": 1},
        **{
            field: {"type": "string", "format": "date-time"}
            for field in (
                "created_after",
                "created_before",
                "updated_after",
                "updated_before",
                "started_after",
                "started_before",
                "completed_after",
                "completed_before",
            )
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
)
GET_RUN_SCHEMA = _schema(
    "get_workflow_run",
    "Get a workflow run with per-node results, variables, status, timing, and errors.",
    {"run_id": {"type": "string"}},
    ["run_id"],
)
CANCEL_RUN_SCHEMA = _schema(
    "cancel_workflow_run",
    "Cancel a pending, running, or paused workflow run.",
    {"run_id": {"type": "string"}},
    ["run_id"],
)
RESUME_RUN_SCHEMA = _schema(
    "resume_workflow_run",
    "Resume a paused workflow and optionally supply approval/event variables.",
    {
        "run_id": {"type": "string"},
        "inputs": {"type": "object"},
        "execute": {"type": "boolean", "default": True},
    },
    ["run_id"],
)
IMPORT_WORKFLOW_SCHEMA = _schema(
    "import_workflow",
    "Import an n8n, Dify, or ComfyUI workflow export into Manor. Use dry_run first to inspect mapping coverage.",
    {
        "content": {"type": "string", "description": "Raw exported JSON or YAML."},
        "name": {"type": "string"},
        "dry_run": {"type": "boolean", "default": True},
        "target_workspace_id": {"type": "string"},
        "create_binding": {"type": "boolean", "default": False},
    },
    ["content"],
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(message: str, **extra: Any) -> str:
    return _dump({"ok": False, "error": message, **extra})


def _query_string_values(
    kwargs: dict[str, Any],
    plural_field: str,
    *,
    singular_field: str | None = None,
    maximum: int = 50,
) -> list[str]:
    raw = kwargs.get(plural_field)
    if raw in (None, ""):
        values: list[Any] = []
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        raise ValueError(f"{plural_field} must be an array")
    if singular_field:
        single = kwargs.get(singular_field)
        if single not in (None, ""):
            values.insert(0, single)
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= maximum:
            break
    return normalized


def _query_optional_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


def _workflow_dict(wf, *, include_steps: bool = True) -> dict:
    payload = {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description or "",
        "icon": wf.icon or "flow",
        "trigger_type": wf.trigger_type,
        "trigger_config": dict(wf.trigger_config or {}),
        "variables": dict(wf.variables or {}),
        "category": wf.category,
        "tags": visible_workflow_tags(wf.tags),
        "is_active": bool(wf.is_active),
        "status": wf.status,
        "version": int(wf.version or 1),
    }
    if include_steps:
        payload["steps"] = list(wf.steps or [])
    return payload


def _run_dict(run, *, detailed: bool = True) -> dict:
    payload = {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "workspace_id": run.workspace_id,
        "binding_id": run.binding_id,
        "trigger_source": run.trigger_source,
        "status": run.status,
        "current_step_id": run.current_step_id,
        "error": run.error,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }
    if detailed:
        payload["variables"] = dict(run.variables or {})
        payload["step_results"] = dict(run.step_results or {})
        payload["trigger_data"] = dict(run.trigger_data or {})
    return payload


def _binding_dict(binding) -> dict:
    return {
        "id": binding.id,
        "workflow_id": binding.workflow_id,
        "workspace_id": binding.workspace_id,
        "name": binding.name,
        "trigger_type": binding.trigger_type,
        "trigger_config": dict(binding.trigger_config or {}),
        "variables": dict(binding.variables or {}),
        "enabled": bool(binding.enabled),
        "status": binding.status,
    }


async def _resolve_workflow(db, entity_id: str, target: object, *, workspace_id: str | None = None):
    from packages.core.services import workflow_service as svc

    value = str(target or "").strip()
    if not value:
        return None
    by_id = await svc.get_workflow(db, value, entity_id)
    if by_id is not None:
        return by_id if workspace_id is None or by_id.workspace_id in {None, workspace_id} else None
    if workspace_id is not None:
        installed = await _installed_blueprint_workflow(db, entity_id, value, workspace_id=workspace_id)
        if installed is not None:
            return installed
    workflows = await svc.list_workflows(db, entity_id)
    matches = [wf for wf in workflows if wf.name.lower() == value.lower()]
    if workspace_id is not None:
        local = [wf for wf in matches if wf.workspace_id == workspace_id]
        matches = local or [wf for wf in matches if wf.workspace_id is None]
        return matches[0] if len(matches) == 1 else None
    return next(iter(matches), None)


async def _installed_blueprint_workflow(db, entity_id: str, key: str, *, workspace_id: str):
    """A Flow installed by a Workspace Blueprint, found by its Blueprint slug.

    Installation records each Flow's portable slug, so Blueprint steps can
    name a Flow in their own Workspace without depending on its editable name.
    """
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowDefinition, WorkflowTemplateInstallation

    rows = (await db.execute(
        select(WorkflowDefinition)
        .join(WorkflowTemplateInstallation, WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id)
        .where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.source_type == "workspace_blueprint",
            WorkflowTemplateInstallation.component_key == key,
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.workspace_id == workspace_id,
        )
    )).scalars().unique().all()
    return rows[0] if len(rows) == 1 else None


async def _exposed_workflows(
    db,
    entity_id: str,
    *,
    workspace_id: str | None = None,
) -> list[tuple[Any, Any]]:
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    rows = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowBinding.trigger_type == "mcp",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )).all()
    from packages.core.services.workspace_workflow_router import prefer_workspace_bindings

    preferred = prefer_workspace_bindings(rows, workspace_id=workspace_id)
    if workspace_id is not None:
        return preferred

    # Ordinary Agent Chat has no Workspace context. Keep entity-level bindings
    # preferred, but make a workflow published in exactly one Workspace
    # callable without forcing the user to copy an opaque Workspace id. When a
    # workflow is published in multiple Workspaces, selection stays explicit.
    selected_workflow_ids = {str(workflow.id) for _, workflow in preferred}
    workspace_candidates: dict[str, list[tuple[Any, Any]]] = {}
    for binding, workflow in rows:
        if binding.workspace_id is None or str(workflow.id) in selected_workflow_ids:
            continue
        workspace_candidates.setdefault(str(workflow.id), []).append((binding, workflow))
    for candidates in workspace_candidates.values():
        if len(candidates) == 1:
            preferred.append(candidates[0])
    return preferred


def _requested_workspace_id(kwargs: dict[str, Any]) -> str | None:
    """Prefer an explicit Agent choice over the conversation's implicit context."""
    return str(
        kwargs.get("target_workspace_id") or kwargs.get("workspace_id") or ""
    ).strip() or None


async def _list_workflows(entity_id: str = "", **kwargs: Any) -> str:
    workspace_id = _requested_workspace_id(kwargs)
    async with async_session() as db:
        pairs = await _exposed_workflows(db, entity_id, workspace_id=workspace_id)
        return _dump({
            "ok": True,
            "workflows": [
                {
                    "id": wf.id,
                    "name": wf.name,
                    "description": (binding.trigger_config or {}).get("description") or wf.description or "",
                    "inputs": sorted((wf.variables or {}).keys()),
                    "binding_id": binding.id,
                    "workspace_id": binding.workspace_id,
                }
                for binding, wf in pairs
            ],
        })


def _run_workspace_id(kwargs: dict[str, Any]) -> str | None:
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
        runtime_trusted_workspace_id_from_context,
    )

    return runtime_trusted_workspace_id_from_context(runtime_tool_call_context_from_kwargs(kwargs))


async def _workspace_flow_rows(
    db,
    *,
    entity_id: str,
    user_id: str,
    require_control: bool = False,
    run_workspace_id: str | None = None,
) -> list[tuple[Any, Any, Any, Any]]:
    """The Workspace flows a user may see (or, ``require_control``, start).

    An external collaborator's persisted company is their own, not the
    customer's: their flows are those of the run's Workspace when their live
    grant covers it (their seat there is the owner seat, so they control it).
    """
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace import Workspace
    from packages.core.services.collaborator_scope import actor_workspace_scope
    from packages.core.services.workspace_access import (
        user_can_read_workspace,
        user_can_write_workspace_artifacts,
    )
    from packages.core.services.workspace_workflow_router import normalize_chat_entrypoint

    user = await db.get(User, user_id) if user_id else None
    if user is None:
        return []
    collaborator_scope = await actor_workspace_scope(db, user_id=user.id, entity_id=entity_id)
    if collaborator_scope is not None:
        collaborator_scope = collaborator_scope & {str(run_workspace_id or "")}
    elif user.entity_id != entity_id:
        return []
    rows = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition, Workspace)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .join(Workspace, Workspace.id == WorkflowBinding.workspace_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
            Workspace.deleted_at.is_(None),
        )
        .order_by(Workspace.name.asc(), WorkflowBinding.name.asc(), WorkflowBinding.id.asc())
    )).all()
    visible: list[tuple[Any, Any, Any, Any]] = []
    for binding, workflow, workspace in rows:
        entrypoint = normalize_chat_entrypoint(binding, workflow)
        if entrypoint is None:
            continue
        if collaborator_scope is not None:
            if workspace.id in collaborator_scope:
                visible.append((entrypoint, binding, workflow, workspace))
            continue
        if not await user_can_read_workspace(db, workspace=workspace, user=user):
            continue
        if require_control and not await user_can_write_workspace_artifacts(
            db,
            workspace_id=workspace.id,
            user_id=user.id,
            entity_role=user.role,
        ):
            continue
        visible.append((entrypoint, binding, workflow, workspace))
    return visible


def _workspace_flow_descriptor(entrypoint, binding, workflow, workspace) -> dict[str, Any]:
    config = dict(binding.config or {})
    return {
        "binding_id": binding.id,
        "flow_id": workflow.id,
        "flow_slug": str(config.get("workspace_blueprint_workflow_slug") or workflow.id),
        "title": entrypoint.title,
        "description": entrypoint.description,
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "inputs": [dict(item) for item in entrypoint.run_inputs],
    }


async def _list_workspace_flows(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    query = str(kwargs.get("query") or "").strip().casefold()
    async with async_session() as db:
        rows = await _workspace_flow_rows(
            db,
            entity_id=entity_id,
            user_id=user_id,
            run_workspace_id=_run_workspace_id(kwargs),
        )
        descriptors = [
            _workspace_flow_descriptor(*row)
            for row in rows
        ]
        if query:
            descriptors = [
                item for item in descriptors
                if query in " ".join(
                    str(item.get(key) or "").casefold()
                    for key in ("title", "flow_slug", "workspace_name")
                )
            ]
        return _dump({"ok": True, "flows": descriptors})


def _global_flow_mode_allowed(kwargs: dict[str, Any]) -> bool:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = context.runtime_envelope
    surface = getattr(getattr(envelope, "surface", None), "value", getattr(envelope, "surface", None))
    if surface != ChatSurface.GLOBAL_OWNER_CHAT.value:
        return True
    metadata = getattr(envelope, "metadata", None)
    return isinstance(metadata, dict) and str(metadata.get("chat_mode") or "") == "flows"


def _global_flow_context(kwargs: dict[str, Any]) -> bool:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    envelope = runtime_tool_call_context_from_kwargs(kwargs).runtime_envelope
    surface = getattr(getattr(envelope, "surface", None), "value", getattr(envelope, "surface", None))
    return surface == ChatSurface.GLOBAL_OWNER_CHAT.value


def _flow_reference_matches(descriptor: dict[str, Any], flow_ref: str) -> bool:
    return flow_ref in {
        str(descriptor.get("flow_id") or "").casefold(),
        str(descriptor.get("title") or "").strip().casefold(),
        str(descriptor.get("flow_slug") or "").strip().casefold(),
    }


def _same_user_facing_flow(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_refs = {
        str(left.get("title") or "").strip().casefold(),
        str(left.get("flow_slug") or "").strip().casefold(),
    } - {""}
    right_refs = {
        str(right.get("title") or "").strip().casefold(),
        str(right.get("flow_slug") or "").strip().casefold(),
    } - {""}
    return bool(left_refs & right_refs)


def _explicit_workspace_matches(
    matches: list[tuple[Any, Any, Any, Any, dict[str, Any]]],
    *,
    source_text: str,
) -> list[tuple[Any, Any, Any, Any, dict[str, Any]]]:
    normalized = source_text.casefold()
    return [
        item for item in matches
        if str(item[4]["workspace_id"]).casefold() in normalized
        or str(item[4]["workspace_name"]).strip().casefold() in normalized
    ]


async def _start_workspace_flow(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    if not _global_flow_mode_allowed(kwargs):
        return _error("flows_mode_required")

    from sqlalchemy import select

    from packages.core.models.task import Conversation, Message
    from packages.core.services.workspace_flow_launcher import launch_workspace_flow

    binding_id = str(kwargs.get("binding_id") or "").strip()
    flow_ref = str(kwargs.get("flow") or "").strip().casefold()
    workspace_ref = str(kwargs.get("target_workspace") or "").strip().casefold()
    original_user_message = str(
        kwargs.get("_active_user_message_from_context") or ""
    ).strip()
    source_brief = str(kwargs.get("source_brief") or original_user_message).strip()
    if _global_flow_context(kwargs) and original_user_message:
        source_brief = original_user_message
    conversation_id = str(kwargs.get("conversation_id") or "").strip()
    if not conversation_id:
        return _error("conversation_required")
    if not binding_id and not flow_ref:
        return _error("flow_required")

    async with async_session() as db:
        rows = await _workspace_flow_rows(
            db,
            entity_id=entity_id,
            user_id=user_id,
            require_control=True,
            run_workspace_id=_run_workspace_id(kwargs),
        )
        described = [(*row, _workspace_flow_descriptor(*row)) for row in rows]
        matches = []
        for entrypoint, binding, workflow, workspace, descriptor in described:
            if binding_id and binding.id != binding_id:
                continue
            if flow_ref and not _flow_reference_matches(descriptor, flow_ref):
                continue
            matches.append((entrypoint, binding, workflow, workspace, descriptor))

        ambiguity_matches = matches
        if binding_id and len(matches) == 1:
            selected = matches[0][4]
            ambiguity_matches = [
                item for item in described
                if _same_user_facing_flow(item[4], selected)
            ]
        if _global_flow_context(kwargs) and len(ambiguity_matches) > 1:
            explicit = _explicit_workspace_matches(
                ambiguity_matches,
                source_text=str(kwargs.get("_active_user_message_from_context") or ""),
            )
            if len(explicit) != 1:
                return _error(
                    "ambiguous_workflow",
                    candidates=[item[4] for item in ambiguity_matches],
                )
            explicit_binding_id = explicit[0][1].id
            if binding_id and explicit_binding_id != binding_id:
                return _error(
                    "ambiguous_workflow",
                    candidates=[item[4] for item in ambiguity_matches],
                )
            matches = [item for item in matches if item[1].id == explicit_binding_id]
        if workspace_ref:
            matches = [
                item for item in matches
                if workspace_ref in {
                    str(item[3].id).casefold(),
                    str(item[3].name).strip().casefold(),
                }
            ]
        if not matches:
            return _error("workspace_flow_not_found")
        if len(matches) > 1:
            return _error(
                "ambiguous_workflow",
                candidates=[item[4] for item in matches],
            )

        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
                Conversation.user_id == user_id,
            )
        )).scalar_one_or_none()
        if conversation is None:
            return _error("conversation_not_found")
        context = kwargs.get("_runtime_envelope_from_context")
        metadata = getattr(context, "metadata", None)
        origin_message_id = str(
            ((metadata or {}).get("origin_user_message_id") or "")
            if isinstance(metadata, dict) else ""
        ).strip()
        if not origin_message_id:
            origin_message_id = str((await db.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )).scalar_one_or_none() or "")
        if not origin_message_id:
            return _error("origin_message_not_found")

        entrypoint, binding, _workflow, workspace, descriptor = matches[0]
        user_requested = await _workflow_tool_user_requested(
            db, entity_id=entity_id, workspace_id=workspace.id, kwargs=kwargs,
        )
        launched = await launch_workspace_flow(
            db,
            source="global_chat" if user_requested and _global_flow_context(kwargs) else "mcp",
            entrypoint=entrypoint,
            binding=binding,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            source_brief=source_brief,
            input_values=(
                dict(kwargs["inputs"])
                if isinstance(kwargs.get("inputs"), dict) else None
            ),
            starter_policy="always_review",
            user_requested=user_requested,
        )
        return _dump({
            "ok": True,
            "created": launched.created,
            "flow": descriptor,
            "run": _run_dict(launched.run),
        })


async def _list_workflow_definitions(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    try:
        limit = max(1, min(int(kwargs.get("limit") or 25), 100))
        offset = max(0, int(kwargs.get("offset") or 0))
        filters = {
            "query": str(kwargs.get("query") or "").strip() or None,
            "statuses": _query_string_values(
                kwargs,
                "statuses",
                singular_field="status",
                maximum=20,
            ),
            "categories": _query_string_values(kwargs, "categories", maximum=30),
            "workspace_ids": _query_string_values(kwargs, "workspace_ids"),
            "created_by_ids": _query_string_values(kwargs, "created_by_ids"),
            "active": kwargs.get("active") if isinstance(kwargs.get("active"), bool) else None,
            "created_after": kwargs.get("created_after"),
            "created_before": kwargs.get("created_before"),
            "updated_after": kwargs.get("updated_after"),
            "updated_before": kwargs.get("updated_before"),
            "version_min": _query_optional_int(kwargs.get("version_min"), "version_min"),
            "version_max": _query_optional_int(kwargs.get("version_max"), "version_max"),
        }
    except (TypeError, ValueError) as exc:
        return _error(str(exc), code="invalid_workflow_filters")
    async with async_session() as db:
        try:
            workflows, total = await svc.query_workflows(
                db,
                entity_id,
                **filters,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            return _error(str(exc), code="invalid_workflow_filters")
        items = [_workflow_dict(wf, include_steps=False) for wf in workflows]
        return _dump({
            "ok": True,
            "workflows": items,
            "total": total,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
        })


async def _get_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        payload = _workflow_dict(wf)
        payload["validation"] = svc.validate_workflow_definition(wf)
        payload["deployments"] = [
            _binding_dict(binding)
            for binding in await svc.list_bindings(db, entity_id, workflow_id=wf.id)
        ]
        return _dump({"ok": True, "workflow": payload})


async def _collaborator_steps_refused(db, entity_id: str, kwargs: dict[str, Any], steps: Any) -> str | None:
    """A collaborator-steered call saves only steps that stay inside the run Workspace.

    Steps a model wrote or generate_workflow produced alike: subworkflows,
    Agents, Skills, Knowledge and tools must be the run Workspace's.
    """
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
        runtime_trusted_workspace_id_from_context,
    )
    from packages.core.services.collaborator_scope import workflow_steps_outside
    from packages.core.services.collaborator_steering import collaborators_acting_now

    collaborators = collaborators_acting_now(entity_id)
    if not collaborators:
        return None
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if await workflow_steps_outside(
        db,
        entity_id=entity_id,
        workspace_id=runtime_trusted_workspace_id_from_context(context),
        steps=steps,
        user_id=context.user_id if context.user_id in collaborators else None,
    ):
        return _error("The Workflow names a Workflow, Agent, Skill, Knowledge or tool outside this Workspace.")
    return None


async def _create_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc
    from packages.core.services.workflow_generator import generate_workflow

    steps = kwargs.get("steps")
    variables = kwargs.get("variables")
    generated = None
    prompt = str(kwargs.get("prompt") or "").strip()
    if not isinstance(steps, list):
        if not prompt:
            return _error("Provide either 'prompt' or a complete 'steps' graph")
        try:
            generated = await generate_workflow(
                prompt, None, entity_id,
                user_id=str(kwargs.get("user_id") or "").strip() or None,
            )
        except Exception as exc:
            return _error(f"AI workflow generation failed: {exc}")
        steps = generated.get("steps")
        if not isinstance(variables, dict):
            variables = generated.get("variables")

    validation = svc.validate_workflow_steps(steps)
    if not validation["valid"] and not bool(kwargs.get("allow_invalid")):
        return _error("Workflow graph is invalid", validation=validation)

    name = str(kwargs.get("name") or (generated or {}).get("name") or "Untitled workflow").strip()
    if not name:
        return _error("Workflow name is required")
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
        runtime_trusted_workspace_id_from_context,
    )
    from packages.core.services.collaborator_steering import collaborators_acting_now

    # A collaborator authors Workflows owned by the run Workspace only.
    owner_workspace_id = (
        runtime_trusted_workspace_id_from_context(runtime_tool_call_context_from_kwargs(kwargs))
        if collaborators_acting_now(entity_id)
        else None
    )
    async with async_session() as db:
        if refused := await _collaborator_steps_refused(db, entity_id, kwargs, steps):
            return refused
        wf = await svc.create_workflow(
            db,
            entity_id=entity_id,
            workspace_id=owner_workspace_id,
            created_by=str(kwargs.get("user_id") or "").strip() or None,
            name=name[:255],
            steps=steps or [],
            description=str(kwargs.get("description") or "").strip() or None,
            icon=str(kwargs.get("icon") or "flow").strip() or "flow",
            variables=variables if isinstance(variables, dict) else {},
            category=str(kwargs.get("category") or "").strip() or None,
            tags=[str(tag) for tag in (kwargs.get("tags") or []) if str(tag).strip()],
        )
        await db.commit()
        return _dump({"ok": True, "created": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _ai_edit_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc
    from packages.core.services.workflow_generator import generate_workflow

    prompt = str(kwargs.get("prompt") or "").strip()
    if not prompt:
        return _error("AI Edit requires a prompt")
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        expected = kwargs.get("expected_version")
        if expected is not None and int(expected) != int(wf.version or 1):
            return _error("Workflow version conflict", expected_version=int(expected), current_version=int(wf.version or 1))
        try:
            generated = await generate_workflow(
                prompt, list(wf.steps or []), entity_id,
                user_id=str(kwargs.get("user_id") or "").strip() or None,
            )
        except Exception as exc:
            return _error(f"AI workflow edit failed: {exc}")
        validation = svc.validate_workflow_steps(generated.get("steps"))
        if not validation["valid"] and not bool(kwargs.get("allow_invalid")):
            return _error("AI Edit produced an invalid graph", validation=validation, draft=generated)
        if refused := await _collaborator_steps_refused(db, entity_id, kwargs, generated.get("steps")):
            return refused
        if not bool(kwargs.get("save", True)):
            return _dump({"ok": True, "saved": False, "draft": generated, "validation": validation, "version": wf.version})
        changes: dict[str, Any] = {
            "steps": generated.get("steps") or [],
            "variables": {
                **dict(wf.variables or {}),
                **(
                    generated.get("variables")
                    if isinstance(generated.get("variables"), dict)
                    else {}
                ),
            },
        }
        if bool(kwargs.get("update_name")) and generated.get("name"):
            changes["name"] = str(generated["name"])[:255]
        wf = await svc.update_workflow(db, wf.id, entity_id, **changes)
        await db.commit()
        return _dump({"ok": True, "saved": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _update_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        expected = kwargs.get("expected_version")
        if expected is not None and int(expected) != int(wf.version or 1):
            return _error("Workflow version conflict", expected_version=int(expected), current_version=int(wf.version or 1))
        changes = {
            key: kwargs[key]
            for key in ("name", "description", "icon", "steps", "variables", "category", "tags", "is_active", "status")
            if key in kwargs
        }
        if not changes:
            return _error("No workflow changes were provided")
        validation = (
            svc.validate_workflow_steps(changes["steps"])
            if "steps" in changes
            else svc.validate_workflow_definition(wf)
        )
        if "steps" in changes and not validation["valid"] and not bool(kwargs.get("allow_invalid")):
            return _error("Workflow graph is invalid", validation=validation)
        if "steps" in changes and (refused := await _collaborator_steps_refused(db, entity_id, kwargs, changes["steps"])):
            return refused
        wf = await svc.update_workflow(db, wf.id, entity_id, **changes)
        await db.commit()
        return _dump({"ok": True, "updated": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _validate_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    steps = kwargs.get("steps")
    workflow = None
    if not isinstance(steps, list):
        async with async_session() as db:
            workflow = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
            if workflow is None:
                return _error("Provide 'workflow' or an unsaved 'steps' graph")
            steps = workflow.steps
    return _dump({
        "ok": True,
        "workflow_id": workflow.id if workflow is not None else None,
        "validation": (
            svc.validate_workflow_definition(workflow)
            if workflow is not None
            else svc.validate_workflow_steps(steps)
        ),
    })


async def _deploy_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    trigger_type = str(kwargs.get("trigger_type") or "mcp").strip().lower()
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id, user_id=user_id or None)
    workspace_id = str(kwargs.get("target_workspace_id") or context.workspace_id or "").strip() or None
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"), workspace_id=workspace_id)
        if wf is None:
            return _error("Workflow not found")
        validation = svc.validate_workflow_definition(wf)
        if not validation["valid"]:
            return _error("Workflow must be valid before deployment", validation=validation)
        if not wf.is_active or wf.status != "active":
            return _error("Workflow must be active before deployment")
        if trigger_type == "schedule":
            cron = str(kwargs.get("cron") or (kwargs.get("trigger_config") or {}).get("cron") or "").strip()
            run_at = str(kwargs.get("run_at") or "").strip() or None
            if bool(cron) == bool(run_at):
                return _error("Scheduled deployment requires exactly one of cron or run_at")
            try:
                job = await svc.schedule_workflow(
                    db,
                    entity_id=entity_id,
                    workflow_id=wf.id,
                    cron=cron or None,
                    run_at=run_at,
                    name=str(kwargs.get("name") or wf.name),
                    workspace_id=workspace_id,
                    timezone_str=str(kwargs.get("timezone") or "UTC"),
                    created_by=context.user_id,
                    conversation_id=context.conversation_id,
                    inputs=kwargs.get("inputs"),
                    stop_after=kwargs.get("stop_after"),
                    owner_project_id=kwargs.get("owner_project_id"),
                    idempotency_key=kwargs.get("idempotency_key"),
                )
            except svc.ScheduledWorkflowInputsError as exc:
                return _error(str(exc), required_inputs=exc.labels)
            except (ValueError, PermissionError) as exc:
                return _error(str(exc))
            await db.commit()
            return _dump({"ok": True, "kind": "automation", "scheduled_job_id": job.id, "workflow_id": wf.id})

        existing = await svc.list_bindings(db, entity_id, workspace_id=workspace_id, workflow_id=wf.id)
        match = next((binding for binding in existing if binding.trigger_type == trigger_type and binding.status == "active"), None)
        if match is not None:
            return _dump({"ok": True, "created": False, "deployment": _binding_dict(match), "validation": validation})
        config = dict(kwargs.get("trigger_config") or {})
        if trigger_type == "mcp" and kwargs.get("description"):
            config["description"] = str(kwargs["description"])
        binding = await svc.create_workflow_binding(
            db,
            entity_id=entity_id,
            workflow_id=wf.id,
            workspace_id=workspace_id,
            name=str(kwargs.get("name") or wf.name),
            trigger_type=trigger_type,
            trigger_config=config,
            variables=kwargs.get("variables") if isinstance(kwargs.get("variables"), dict) else {},
        )
        await db.commit()
        return _dump({"ok": True, "created": True, "deployment": _binding_dict(binding), "validation": validation})


async def _delete_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        bindings = await svc.list_bindings(db, entity_id, workflow_id=wf.id)
        if bindings and not bool(kwargs.get("delete_bindings")):
            return _error(
                "Workflow has deployments; set delete_bindings=true to remove them with the definition",
                binding_ids=[binding.id for binding in bindings],
            )
        for binding in bindings:
            await svc.delete_binding(db, binding.id, entity_id)
        try:
            deleted = await svc.delete_workflow(db, wf.id, entity_id)
        except svc.WorkflowInUseError as exc:
            return _error(
                str(exc),
                binding_ids=exc.binding_ids,
                scheduled_job_ids=exc.scheduled_job_ids,
            )
        await db.commit()
        return _dump({"ok": deleted, "deleted": deleted, "workflow_id": wf.id, "deleted_bindings": len(bindings)})


async def _execute_run(run_id: str) -> None:
    from packages.core.ai.workflow_runner import WorkflowRunner

    await WorkflowRunner().run(run_id)


async def _workflow_tool_user_requested(db, *, entity_id: str, workspace_id: str | None, kwargs: dict) -> bool:
    from packages.core.ai.runtime.execution_origin import runtime_tool_is_user_requested

    return await runtime_tool_is_user_requested(
        db, entity_id=entity_id, workspace_id=workspace_id, kwargs=kwargs,
    )


async def _run_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    if _global_flow_context(kwargs):
        return _error("workspace_flow_launcher_required")

    target = str(kwargs.get("workflow") or "").strip()
    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    workspace_id = _requested_workspace_id(kwargs)
    if not target:
        return _error("run_workflow requires a workflow")
    async with async_session() as db:
        pairs = await _exposed_workflows(db, entity_id, workspace_id=workspace_id)
        match = next(((binding, wf) for binding, wf in pairs if wf.id == target or wf.name.lower() == target.lower()), None)
        if match is None:
            if workspace_id is None:
                from sqlalchemy import select

                from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

                candidates = (await db.execute(
                    select(WorkflowBinding, WorkflowDefinition)
                    .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
                    .where(
                        WorkflowBinding.entity_id == entity_id,
                        WorkflowBinding.enabled.is_(True),
                        WorkflowBinding.status == "active",
                        WorkflowBinding.trigger_type == "mcp",
                        WorkflowDefinition.is_active.is_(True),
                        WorkflowDefinition.status == "active",
                    )
                )).all()
                target_candidates = [
                    (binding, wf)
                    for binding, wf in candidates
                    if wf.id == target or wf.name.lower() == target.lower()
                ]
                workspace_ids = sorted({
                    str(binding.workspace_id)
                    for binding, _ in target_candidates
                    if binding.workspace_id
                })
                if len(workspace_ids) > 1:
                    return _error(
                        "Workflow is published in multiple Workspaces; provide target_workspace_id",
                        workflow=target,
                        workspace_ids=workspace_ids,
                        available=[wf.name for _, wf in pairs],
                    )
            return _error("Workflow is not published as an Agent tool", available=[wf.name for _, wf in pairs])
        depth = _WORKFLOW_TOOL_DEPTH.get()
        if depth >= 3:
            return _error("Workflow nesting limit reached")
        binding, workflow = match
        execution_workspace_id = workspace_id or binding.workspace_id
        runtime_context = {
            key: value
            for key, raw_value in (
                ("workspace_id", execution_workspace_id),
                ("conversation_id", kwargs.get("conversation_id")),
                ("task_id", kwargs.get("task_id")),
            )
            if (value := str(raw_value or "").strip())
        }
        run = await svc.start_workflow_from_binding(
            db,
            binding,
            variables=inputs,
            trigger_data={
                "_workflow_tool_depth": depth + 1,
                **({"runtime_context": runtime_context} if runtime_context else {}),
            },
            trigger_source="mcp",
            started_by=user_id or None,
            execution_workspace_id=execution_workspace_id,
            user_requested=await _workflow_tool_user_requested(
                db, entity_id=entity_id, workspace_id=execution_workspace_id, kwargs=kwargs,
            ),
        )
        conversation_id = runtime_context.get("conversation_id")
        if conversation_id:
            from sqlalchemy import select

            from packages.core.models.task import Conversation
            from packages.core.services.conversation_messages import add_message
            from packages.core.services.workflow_chat_projection import workflow_progress_steps

            conversation = (await db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.entity_id == entity_id,
                    Conversation.user_id == user_id,
                )
            )).scalar_one_or_none()
            if conversation is not None:
                origin_context = {
                    "enabled": True,
                    "route_source": "agent_tool",
                    "conversation_id": conversation.id,
                    "projection": {
                        "progress": True,
                        "step_outputs": "explicit",
                        "final_output": True,
                    },
                    "wait_bridge": True,
                }
                activity_message = await add_message(
                    db,
                    conversation.id,
                    role="system",
                    content=f"Started {workflow.name}. Preparing the first workflow step.",
                    message_kind="workflow_activity",
                    refs=[
                        {"type": "workflow", "id": run.workflow_id, "title": workflow.name},
                        {"type": "workflow_run", "id": run.id},
                    ],
                    meta={
                        "workflow_run_id": run.id,
                        "workflow_binding_id": binding.id,
                        "workflow_title": workflow.name,
                        "workflow_status": "queued",
                        "workflow_route_source": "agent_tool",
                        "workflow_steps": workflow_progress_steps(
                            run,
                            activity_status="queued",
                        ),
                    },
                )
                origin_context["activity_message_id"] = activity_message.id
                updated_trigger_data = dict(run.trigger_data or {})
                updated_trigger_data["_workflow_chat_origin"] = origin_context
                run.trigger_data = updated_trigger_data
        await db.commit()
        run_id = run.id
    depth_token = _WORKFLOW_TOOL_DEPTH.set(depth + 1)
    try:
        await _execute_run(run_id)
    finally:
        _WORKFLOW_TOOL_DEPTH.reset(depth_token)
    async with async_session() as db:
        done = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": done is not None and done.status != "failed", "run": _run_dict(done) if done else None})


async def _test_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        validation = svc.validate_workflow_definition(wf)
        if not validation["valid"]:
            return _error("Workflow is invalid", validation=validation)
        try:
            run = await svc.start_workflow(db, entity_id, wf.id, variables=inputs, started_by=user_id or None, trigger_source="agent_test")
        except ValueError as exc:
            return _error(str(exc))
        await db.commit()
        run_id = run.id
    await _execute_run(run_id)
    async with async_session() as db:
        done = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": done is not None and done.status != "failed", "run": _run_dict(done) if done else None})


async def _test_workflow_node(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    step = kwargs.get("step")
    if not isinstance(step, dict):
        return _error("step must be an object")
    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    from packages.core.services.workflow_service import execute_node_test

    started = perf_counter()
    async with async_session() as db:
        try:
            result = await execute_node_test(
                db,
                entity_id=entity_id,
                user_id=user_id or None,
                step=step,
                variables=inputs,
            )
        except Exception as exc:
            return _error(str(exc), step_id=step.get("id"), duration_ms=round((perf_counter() - started) * 1000, 2))
    return _dump({"ok": result.get("status") != "failed", "step_id": step.get("id"), "result": result, "duration_ms": round((perf_counter() - started) * 1000, 2)})


async def _list_workflow_runs(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    try:
        limit = max(1, min(int(kwargs.get("limit") or 20), 100))
        offset = max(0, int(kwargs.get("offset") or 0))
        filters = {
            "workflow_ids": _query_string_values(kwargs, "workflow_ids"),
            "workspace_ids": _query_string_values(
                kwargs,
                "workspace_ids",
                singular_field="target_workspace_id",
            ),
            "binding_ids": _query_string_values(
                kwargs,
                "binding_ids",
                singular_field="binding_id",
            ),
            "statuses": _query_string_values(
                kwargs,
                "statuses",
                singular_field="status",
                maximum=20,
            ),
            "trigger_sources": _query_string_values(
                kwargs,
                "trigger_sources",
                maximum=20,
            ),
            "started_by_ids": _query_string_values(kwargs, "started_by_ids"),
            "created_after": kwargs.get("created_after"),
            "created_before": kwargs.get("created_before"),
            "updated_after": kwargs.get("updated_after"),
            "updated_before": kwargs.get("updated_before"),
            "started_after": kwargs.get("started_after"),
            "started_before": kwargs.get("started_before"),
            "completed_after": kwargs.get("completed_after"),
            "completed_before": kwargs.get("completed_before"),
            "attempt_number_min": _query_optional_int(
                kwargs.get("attempt_number_min"),
                "attempt_number_min",
            ),
            "attempt_number_max": _query_optional_int(
                kwargs.get("attempt_number_max"),
                "attempt_number_max",
            ),
        }
    except (TypeError, ValueError) as exc:
        return _error(str(exc), code="invalid_workflow_run_filters")
    async with async_session() as db:
        if kwargs.get("workflow"):
            wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
            if wf is None:
                return _error("Workflow not found")
            filters["workflow_ids"] = list(dict.fromkeys([
                wf.id,
                *filters["workflow_ids"],
            ]))
        try:
            runs, total = await svc.query_runs(
                db,
                entity_id,
                **filters,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            return _error(str(exc), code="invalid_workflow_run_filters")
        items = [_run_dict(run, detailed=False) for run in runs]
        return _dump({
            "ok": True,
            "runs": items,
            "total": total,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
        })


async def _user_can_read_workflow_run(db, *, run, entity_id: str, user_id: str) -> bool:
    from packages.core.models.user import User
    from packages.core.services.workspace_access import user_can_read_workspace_id

    user = await db.get(User, user_id) if user_id else None
    if user is None or str(user.entity_id) != entity_id:
        return False
    if not run.workspace_id:
        return True
    return await user_can_read_workspace_id(
        db,
        workspace_id=str(run.workspace_id),
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )


async def _user_can_control_workflow_run(db, *, run, entity_id: str, user_id: str) -> bool:
    from packages.core.models.user import User
    from packages.core.services.workspace_access import user_can_control_workspace_run

    user = await db.get(User, user_id) if user_id else None
    if user is None or str(user.entity_id) != entity_id:
        return False
    return await user_can_control_workspace_run(
        db,
        run=run,
        user_id=user.id,
        entity_role=user.role,
    )


async def _get_workflow_run(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        run = await svc.get_run(db, str(kwargs.get("run_id") or ""), entity_id)
        if run is None or not await _user_can_read_workflow_run(
            db,
            run=run,
            entity_id=entity_id,
            user_id=user_id,
        ):
            return _error("Workflow run not found")
        return _dump({"ok": True, "run": _run_dict(run)})


async def _cancel_workflow_run(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        run = await svc.get_run(db, str(kwargs.get("run_id") or ""), entity_id)
        if run is None or not await _user_can_control_workflow_run(
            db,
            run=run,
            entity_id=entity_id,
            user_id=user_id,
        ):
            return _error("Workflow run not found")
        if run.status in {"completed", "cancelled", "failed"}:
            return _error(f"Run is already {run.status}", run=_run_dict(run, detailed=False))
        from packages.core.constants.task import CancelSource
        from packages.core.services.workflow_run_control import (
            active_subworkflow_descendant_ids,
            cancel_active_subworkflow_descendants,
            cancel_run,
        )

        descendant_run_ids = await active_subworkflow_descendant_ids(db, run)
        # An agent acted, even on the user's behalf: a system cancel.
        cancel_run(run, actor_id=user_id, source=CancelSource.SYSTEM)
        cancelled_children = await cancel_active_subworkflow_descendants(
            db,
            run,
            actor_id=user_id,
            descendant_run_ids=descendant_run_ids,
        )
        from packages.core.services.workflow_run_execution_claim import (
            revoke_workflow_run_execution_claims,
        )

        await revoke_workflow_run_execution_claims(
            db,
            [run.id, *descendant_run_ids],
        )
        from packages.core.ai.workflow_runner import (
            finalize_workflow_terminal_effects_best_effort,
            mark_workflow_terminal_effects_pending,
        )
        from packages.core.services.workflow_chat_projection import (
            project_workflow_run_status,
        )

        mark_workflow_terminal_effects_pending(run)
        for child in cancelled_children:
            mark_workflow_terminal_effects_pending(child)
        await project_workflow_run_status(db, run=run)
        await db.commit()
        response = _dump({"ok": True, "run": _run_dict(run)})
        await finalize_workflow_terminal_effects_best_effort(
            run,
            db,
            context="workflow tool cancellation",
        )
        return response


async def _resume_workflow_run(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services import workflow_service as svc

    run_id = str(kwargs.get("run_id") or "")
    async with async_session() as db:
        run = await svc.get_run(db, run_id, entity_id)
        if run is None or not await _user_can_control_workflow_run(
            db,
            run=run,
            entity_id=entity_id,
            user_id=user_id,
        ):
            return _error("Workflow run not found")
        if run.status != "paused":
            return _error("Workflow run is not paused", status=run.status)
    outcome = await WorkflowRunner.resume(
        run_id,
        kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else None,
        entity_id=entity_id,
        resumed_by=user_id or None,
        execute=bool(kwargs.get("execute", True)),
    )
    if outcome in {
        "not_found",
        "not_paused",
        "invalid_approval",
        "invalid_input",
        "definition_changed",
        "continuation_required",
        "wait_changed",
    }:
        return _error(f"Could not resume workflow run: {outcome}")
    async with async_session() as db:
        run = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": True, "run": _run_dict(run) if run else None})


async def _import_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.workflow_import import UnknownWorkflowFormat, import_workflow
    from packages.core.services import workflow_service as svc

    content = str(kwargs.get("content") or "")
    try:
        if bool(kwargs.get("dry_run", True)):
            result = import_workflow(content, name=str(kwargs.get("name") or "").strip() or None)
            return _dump({"ok": True, "saved": False, "report": result.report.to_dict(), "definition": result.definition})
        async with async_session() as db:
            wf, binding, report = await svc.import_workflow_definition(
                db,
                entity_id=entity_id,
                raw=content,
                name=str(kwargs.get("name") or "").strip() or None,
                workspace_id=str(kwargs.get("target_workspace_id") or "").strip() or None,
                create_binding=bool(kwargs.get("create_binding")),
            )
            await db.commit()
            return _dump({"ok": True, "saved": True, "workflow": _workflow_dict(wf), "binding_id": binding.id if binding else None, "report": report})
    except UnknownWorkflowFormat as exc:
        return _error(str(exc))


async def _expand_schedule_occurrences(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services.schedule_occurrences import expand_schedule_occurrences
    try:
        return _dump(expand_schedule_occurrences(
            start_at=kwargs["start_at"], end_at=kwargs["end_at"], cron=kwargs["cron"],
            timezone=kwargs.get("timezone", "UTC"), lead_seconds=kwargs.get("lead_seconds", 0),
            max_occurrences=kwargs.get("max_occurrences", 500),
        ))
    except (ValueError, KeyError, TypeError) as exc:
        return _error(str(exc))


async def _query_workflow_projects(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
    from packages.core.services.workflow_project_service import list_workflow_projects
    from packages.core.services.workspace_access import lock_workspace_access_boundary, user_can_read_workspace_id

    try:
        context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id)
        if not context.workspace_id or not context.user_id:
            raise PermissionError("An authenticated Workspace runtime is required")
        stages = kwargs.get("stages")
        if stages is not None and (not isinstance(stages, list) or any(not isinstance(s, str) for s in stages)):
            raise ValueError("stages must be a list of stage names")
        state_paths = _workflow_project_state_paths(kwargs.get("state_paths"))
        include_execution = kwargs.get("include_execution", False)
        if not isinstance(include_execution, bool):
            raise ValueError("include_execution must be a boolean")
        async with async_session() as db:
            workspace = await lock_workspace_access_boundary(db, workspace_id=context.workspace_id, entity_id=entity_id)
            from packages.core.services.workspace_autonomy import workspace_allows_configured_work

            if not workspace_allows_configured_work(workspace):
                raise PermissionError("Workspace is unavailable")
            if not await user_can_read_workspace_id(
                db, workspace_id=context.workspace_id, entity_id=entity_id, user_id=context.user_id,
            ):
                raise PermissionError("Workspace read access is required")
            rows, cursor = await list_workflow_projects(
                db, entity_id=entity_id, workspace_id=context.workspace_id,
                project_type=str(kwargs.get("project_type") or ""), stages=set(stages) if stages else None,
                parent_id=kwargs.get("parent_id"), parent_state_key=str(kwargs.get("parent_state_key") or "parent_project_id"),
                limit=int(kwargs.get("limit") or 20), after_id=kwargs.get("after_id"),
            )
            executions = await _workflow_project_executions(
                db, entity_id=entity_id, workspace_id=context.workspace_id, rows=rows,
            ) if include_execution else {}
            return _dump({
                "projects": [{"project_id": row.id, "project_type": row.project_type,
                    "project_key": row.project_key, "schema_version": row.schema_version,
                    "current_stage": row.current_stage,
                    "state": _workflow_project_state_projection(row.state, state_paths), "revision": row.revision,
                    "last_run_id": row.last_run_id, "created_by": row.created_by,
                    **({"execution": executions.get(row.id)} if include_execution else {})} for row in rows],
                "has_more": cursor is not None, "next_cursor": cursor,
            })
    except (PermissionError, ValueError, TypeError) as exc:
        return _error(str(exc))


def _workflow_project_state_paths(value: Any) -> list[tuple[str, ...]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= _WORKFLOW_PROJECT_STATE_PATH_LIMIT:
        raise ValueError("state_paths must contain 1-24 bounded state paths")
    paths = []
    for raw_path in value:
        if not isinstance(raw_path, str):
            raise ValueError("state_paths must contain dot-separated state paths")
        path = tuple(raw_path.split("."))
        if (not path or len(path) > _WORKFLOW_PROJECT_STATE_PATH_DEPTH
                or any(not _WORKFLOW_PROJECT_STATE_KEY.fullmatch(key) for key in path)):
            raise ValueError("state_paths must contain bounded dot-separated object paths")
        paths.append(path)
    return paths


def _workflow_project_state_projection(state: Any, paths: list[tuple[str, ...]] | None) -> Any:
    if paths is None:
        return state
    projected: dict[str, Any] = {}
    missing = object()
    for path in paths:
        value = state
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = missing
                break
            value = value[key]
        if value is missing:
            continue
        target = projected
        for key in path[:-1]:
            child = target.get(key)
            if not isinstance(child, dict):
                child = {}
                target[key] = child
            target = child
        target[path[-1]] = deepcopy(value)
    return projected


async def _workflow_project_executions(db: Any, *, entity_id: str, workspace_id: str, rows: list[Any]) -> dict[str, dict[str, Any]]:
    from sqlalchemy import select
    from packages.core.models.workflow import WorkflowRun

    run_ids = {row.last_run_id for row in rows if row.last_run_id}
    if not run_ids:
        return {}
    runs = list((await db.execute(select(WorkflowRun).where(
        WorkflowRun.id.in_(run_ids), WorkflowRun.entity_id == entity_id, WorkflowRun.workspace_id == workspace_id,
    ))).scalars())
    by_id = {
        run.id: {"run_id": run.id, "status": run.status, "current_step_id": run.current_step_id,
                 "error": run.error[:_WORKFLOW_PROJECT_EXECUTION_ERROR_LIMIT] if run.error else None}
        for run in runs
    }
    return {row.id: by_id[row.last_run_id] for row in rows if row.last_run_id in by_id}


QUERY_WORKFLOW_PROJECTS_SCHEMA = _schema(
    "query_workflow_projects",
    "Read a cursor page of Workflow projects in the current Workspace, including execution state and receipts. Optionally select bounded dot-separated state paths to keep large project state compact.",
    {"project_type": {"type": "string"}, "stages": {"type": "array", "items": {"type": "string"}},
     "parent_id": {"type": "string"}, "parent_state_key": {"type": "string", "default": "parent_project_id"},
     "after_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
     "include_execution": {"type": "boolean", "default": False,
                           "description": "Include the linked same-Workspace WorkflowRun status, current step, and bounded error when available."},
     "state_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 24,
                     "description": "Optional dot-separated object paths from project state; returns only requested present paths while preserving explicit nulls."}},
    ["project_type"],
)


EXPAND_SCHEDULE_OCCURRENCES_SCHEMA = _schema(
    "expand_schedule_occurrences",
    "Expand a finite recurring queue using the Scheduler calendar. Does not create jobs or publish content.",
    {"start_at": {"type": "string"}, "end_at": {"type": "string"},
     "cron": {"type": "string"}, "timezone": {"type": "string", "default": "UTC"},
     "lead_seconds": {"type": "number", "minimum": 0, "maximum": 2678400},
     "max_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000}},
    ["start_at", "end_at", "cron"],
)


def get_tools() -> list[tuple[dict, Any]]:
    """Return the complete first-party Workflow tool package."""
    return [
        (QUERY_WORKFLOW_PROJECTS_SCHEMA, _query_workflow_projects),
        (EXPAND_SCHEDULE_OCCURRENCES_SCHEMA, _expand_schedule_occurrences),
        (LIST_WORKSPACE_FLOWS_SCHEMA, _list_workspace_flows),
        (START_WORKSPACE_FLOW_SCHEMA, _start_workspace_flow),
        (LIST_WORKFLOWS_SCHEMA, _list_workflows),
        (RUN_WORKFLOW_SCHEMA, _run_workflow),
        (LIST_DEFINITIONS_SCHEMA, _list_workflow_definitions),
        (GET_WORKFLOW_SCHEMA, _get_workflow),
        (CREATE_WORKFLOW_SCHEMA, _create_workflow),
        (AI_EDIT_WORKFLOW_SCHEMA, _ai_edit_workflow),
        (UPDATE_WORKFLOW_SCHEMA, _update_workflow),
        (VALIDATE_WORKFLOW_SCHEMA, _validate_workflow),
        (DEPLOY_WORKFLOW_SCHEMA, _deploy_workflow),
        (DELETE_WORKFLOW_SCHEMA, _delete_workflow),
        (TEST_WORKFLOW_SCHEMA, _test_workflow),
        (TEST_NODE_SCHEMA, _test_workflow_node),
        (LIST_RUNS_SCHEMA, _list_workflow_runs),
        (GET_RUN_SCHEMA, _get_workflow_run),
        (CANCEL_RUN_SCHEMA, _cancel_workflow_run),
        (RESUME_RUN_SCHEMA, _resume_workflow_run),
        (IMPORT_WORKFLOW_SCHEMA, _import_workflow),
    ]
