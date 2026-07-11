from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.tool_registry import runtime_execute_tool
from packages.core.ai.runtime.tool_surface import runtime_prepare_named_tool_surface_for_turn


RUNTIME_WORKFLOW_CONTEXT_KEYS = ("workspace_id", "conversation_id", "task_id")


@dataclass(frozen=True)
class RuntimeWorkflowToolStepResult:
    """Result for a workflow step that executes one Runtime-scoped tool."""

    output: str
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
    sources = [
        trigger_data,
        _runtime_workflow_mapping(trigger_data.get("runtime_context")),
        _runtime_workflow_mapping(trigger_data.get("context")),
        _runtime_workflow_mapping(trigger_data.get("execution_target")),
        variables,
        _runtime_workflow_mapping(variables.get("runtime_context")),
        _runtime_workflow_mapping(variables.get("context")),
    ]
    return {
        key: _runtime_first_workflow_context_value(key, sources)
        for key in RUNTIME_WORKFLOW_CONTEXT_KEYS
    }


async def runtime_execute_workflow_tool_step(
    *,
    request: AIRuntimeRequest,
    tool_name: str,
    arguments: dict[str, Any],
    active_user_message: str | None = None,
) -> RuntimeWorkflowToolStepResult:
    """Resolve and execute a workflow tool step through Runtime-owned plumbing."""

    surface = runtime_prepare_named_tool_surface_for_turn(
        request,
        tool_names=[tool_name],
    )
    output = await runtime_execute_tool(
        tool_name,
        arguments,
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
        envelope=surface.envelope,
        allowed_tool_names=surface.allowed_tool_names,
    )
