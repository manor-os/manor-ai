from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.tool_registry import runtime_execute_tool
from packages.core.ai.runtime.tool_surface import runtime_prepare_named_tool_surface_for_turn
from packages.core.ai.runtime.tool_visibility import (
    WORKSPACE_LEDGER_QUERY_TOOL,
    WORKSPACE_LEDGER_VISUALIZATION_TOOL,
)


RUNTIME_WORKFLOW_CONTEXT_KEYS = (
    "workspace_id",
    "conversation_id",
    "task_id",
    "workflow_project_id",
    "workflow_project_root",
    "workflow_action_grant_id",
    "workflow_step_id",
    "workflow_scene_id",
    "workflow_batch_capture",
    "approved_plan_version",
    "workflow_effect_deadline_at",
    "workflow_effect_deadline_action_keys",
)
RUNTIME_WORKFLOW_TOOL_CONTEXT_ARGUMENTS = {
    "workflow_run_id": "_workflow_run_id_from_context",
    "workflow_lineage_root_run_id": "_workflow_lineage_root_run_id_from_context",
    "workflow_project_id": "_workflow_project_id_from_context",
    "workflow_project_root": "_workflow_project_root_from_context",
    "workflow_action_grant_id": "_workflow_action_grant_id_from_context",
    "workflow_step_id": "_workflow_step_id_from_context",
    "workflow_scene_id": "_workflow_scene_id_from_context",
    "workflow_batch_capture": "_workflow_batch_capture_from_context",
    "approved_plan_version": "_approved_plan_version_from_context",
    "workflow_effect_deadline_at": "_workflow_effect_deadline_at_from_context",
    "workflow_effect_deadline_action_keys": "_workflow_effect_deadline_action_keys_from_context",
}


@dataclass(frozen=True)
class RuntimeWorkflowToolStepResult:
    """Result for a workflow step that executes one Runtime-scoped tool."""

    output: Any
    structured_output: Any | None
    envelope: RuntimeEnvelope
    allowed_tool_names: set[str]


def _runtime_workflow_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _runtime_workflow_context_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_first_workflow_context_value(
    key: str,
    sources: list[dict[str, Any]],
) -> str | None:
    for source in sources:
        value = _runtime_workflow_context_value(source.get(key))
        if value:
            return value
    return None


def runtime_workflow_run_context(run: Any) -> dict[str, str | None]:
    """Extract workspace execution context carried by a workflow run."""

    trigger_data = _runtime_workflow_mapping(getattr(run, "trigger_data", None))
    variables = _runtime_workflow_mapping(getattr(run, "variables", None))
    has_trusted_runtime_context = "_workflow_runtime_context" in trigger_data
    trusted = _runtime_workflow_mapping(
        trigger_data.get("_workflow_runtime_context")
    )
    run_fields = {
        key: getattr(run, key, None)
        for key in RUNTIME_WORKFLOW_CONTEXT_KEYS
    }
    descriptive_sources = [
        run_fields,
        trusted,
        _runtime_workflow_mapping(trigger_data.get("runtime_context")),
        _runtime_workflow_mapping(trigger_data.get("context")),
        _runtime_workflow_mapping(trigger_data.get("execution_target")),
        trigger_data,
        _runtime_workflow_mapping(variables.get("runtime_context")),
        _runtime_workflow_mapping(variables.get("context")),
    ]
    scheduled = str(getattr(run, "trigger_source", "") or "").strip() == "schedule"
    # New scheduler runs persist this key, including an empty mapping, so it
    # is authoritative for routing context. Older scheduled runs predate that
    # contract and retain the descriptive extraction used by paused/retried
    # workflow records.
    scheduled_sources = (
        [trusted]
        if has_trusted_runtime_context
        else descriptive_sources[1:] + [run_fields]
    )
    context = {
        "workflow_run_id": _runtime_workflow_context_value(getattr(run, "id", None)),
        "workflow_lineage_root_run_id": _runtime_workflow_context_value(
            getattr(run, "lineage_root_run_id", None) or getattr(run, "id", None)
        ),
        "workspace_id": _runtime_first_workflow_context_value(
            "workspace_id", descriptive_sources
        ),
        "conversation_id": _runtime_first_workflow_context_value(
            "conversation_id",
            scheduled_sources if scheduled else descriptive_sources[1:] + [run_fields],
        ),
        "task_id": _runtime_first_workflow_context_value(
            "task_id",
            scheduled_sources if scheduled else descriptive_sources[1:] + [run_fields],
        ),
    }
    for key in (
        "workflow_project_id",
        "workflow_project_root",
        "workflow_action_grant_id",
        "workflow_step_id",
        "workflow_scene_id",
        "workflow_batch_capture",
        "approved_plan_version",
        "workflow_effect_deadline_at",
    ):
        context[key] = _runtime_workflow_context_value(trusted.get(key))
    action_keys = trusted.get("workflow_effect_deadline_action_keys")
    context["workflow_effect_deadline_action_keys"] = (
        [key.strip() for key in action_keys if isinstance(key, str) and key.strip()]
        if isinstance(action_keys, list) else []
    )
    return context


def runtime_workflow_tool_context_args(
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map durable Workflow capture context to hidden Runtime tool arguments."""

    source = context if isinstance(context, dict) else {}
    result = {
        argument: value
        for key, argument in RUNTIME_WORKFLOW_TOOL_CONTEXT_ARGUMENTS.items()
        if (value := (source.get(key) if key == "workflow_effect_deadline_action_keys"
                      else _runtime_workflow_context_value(source.get(key)))) is not None
    }
    workflow_run_id = _runtime_workflow_context_value(source.get("workflow_run_id"))
    if workflow_run_id:
        result["_performance_run_id_from_context"] = workflow_run_id
    return result



async def _runtime_workflow_tool_step_ledger_read_grants(
    request: AIRuntimeRequest,
    tool_name: str,
    arguments: dict[str, Any],
) -> set[str]:
    """Return the Ledger read tools a Ledger query tool step reads through.

    ``query_ledger`` and ``visualize_workspace_ledgers`` authorize each contract
    by its paired read tool. Workspace agents inherit those from the installed
    Ledgers; a tool step's surface holds only the step's own tool. A step that
    names a Ledger query tool reads the Ledgers installed in the run's
    Workspace: ``query_ledger`` only the contract it queries. The tools still
    refuse external surfaces themselves.
    """

    if tool_name not in {WORKSPACE_LEDGER_QUERY_TOOL, WORKSPACE_LEDGER_VISUALIZATION_TOOL}:
        return set()
    if not request.entity_id or not request.workspace_id:
        return set()
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace
    from packages.core.services.ledger_query_service import (
        workspace_ledger_read_tool,
        workspace_ledger_runtime_tools,
    )

    async with async_session() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == request.workspace_id,
                Workspace.entity_id == request.entity_id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
    if workspace is None:
        return set()
    if tool_name == WORKSPACE_LEDGER_QUERY_TOOL:
        read_tool = workspace_ledger_read_tool(workspace.settings, arguments.get("contract_id"))
        return {read_tool} if read_tool else set()
    return workspace_ledger_runtime_tools(workspace.settings)


async def runtime_execute_workflow_tool_step(
    *,
    request: AIRuntimeRequest,
    tool_name: str,
    arguments: dict[str, Any],
    active_user_message: str | None = None,
    workflow_context: dict[str, Any] | None = None,
) -> RuntimeWorkflowToolStepResult:
    """Resolve and execute a workflow tool step through Runtime-owned plumbing."""

    ledger_read_grants = await _runtime_workflow_tool_step_ledger_read_grants(
        request, tool_name, arguments,
    )
    surface = runtime_prepare_named_tool_surface_for_turn(
        request,
        tool_names=[tool_name, *sorted(ledger_read_grants)],
    )
    runtime_arguments = dict(arguments)
    runtime_arguments.update(runtime_workflow_tool_context_args(workflow_context))
    # A deterministic tool node has a durable invocation even though it does
    # not enter the agent loop or receive a provider tool-call ID. Retries use
    # the same run/node identity; a new WorkflowRun is a new operation.
    runtime_arguments.pop("_runtime_run_id_from_context", None)
    runtime_arguments.pop("_runtime_tool_call_id_from_context", None)
    context = workflow_context or {}
    run_id = _runtime_workflow_context_value(context.get("workflow_run_id"))
    step_id = _runtime_workflow_context_value(context.get("workflow_step_id"))
    execution_node_id = _runtime_workflow_context_value(context.get("workflow_execution_node_id"))
    if run_id and step_id:
        runtime_arguments["_runtime_run_id_from_context"] = f"workflow:{run_id}"
        runtime_arguments["_runtime_tool_call_id_from_context"] = (
            f"execution:{execution_node_id}" if execution_node_id else f"node:{step_id}"
        )
    output = await runtime_execute_tool(
        tool_name,
        runtime_arguments,
        entity_id=request.entity_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        active_user_message=active_user_message or request.input_preview,
        allowed_tool_names=surface.allowed_tool_names,
        runtime_envelope=surface.envelope,
    )
    return RuntimeWorkflowToolStepResult(
        output=output,
        structured_output=getattr(output, "structured_content", None),
        envelope=surface.envelope,
        allowed_tool_names=surface.allowed_tool_names,
    )
