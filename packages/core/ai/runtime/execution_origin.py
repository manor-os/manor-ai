"""Trusted user-request lineage for runtime tools, independent of attribution."""
from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile, normalize_runtime_profile
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


def runtime_envelope_task_id(envelope: Any, *, entity_id: str) -> str | None:
    """The Task whose run made this tool call: the Runtime envelope's, only.

    ``task_id`` is also a public business target on several tools, so a
    model-supplied argument never establishes it. Provenance, not authority.
    """
    task_id = str(getattr(envelope, "task_id", None) or "").strip()
    envelope_entity_id = getattr(envelope, "entity_id", None)
    if not task_id or (envelope_entity_id is not None and envelope_entity_id != entity_id):
        return None
    return task_id


def runtime_origin_task_id(kwargs: dict[str, Any] | None, *, entity_id: str) -> str | None:
    """The Task whose run made this tool call, from the call's Runtime envelope."""

    try:
        context = runtime_tool_call_context_from_kwargs(dict(kwargs or {}), entity_id=entity_id)
    except Exception:
        return None
    return runtime_envelope_task_id(context.runtime_envelope, entity_id=entity_id)


async def runtime_tool_is_user_requested(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str | None,
    kwargs: dict[str, Any],
) -> bool:
    """Recognize foreground turns or the exact manual parent of background work.

    A run's user/Agent attribution, tool name, or historical user message is
    not a fresh human request. Missing runtime provenance never mints manual
    admission; direct authenticated API callers record their own explicit intent.
    This receipt does not replace the caller's authorization or approval gates.
    """
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id)
    profile = normalize_runtime_profile(getattr(context.runtime_envelope, "profile", None))
    if profile is None:
        return False
    if profile not in {RuntimeProfile.BACKGROUND_WORKER, RuntimeProfile.WORKFLOW_STEP}:
        return True
    if context.workspace_id is not None and context.workspace_id != workspace_id:
        return False
    # The innermost Workflow owns the continuation when both identities exist.
    # A mismatched/automatic Workflow must not borrow a manual Task's receipt.
    if context.workflow_run_id:
        from packages.core.models.workflow import WorkflowRun
        from packages.core.services.workflow_service import workflow_run_is_user_requested

        run = await db.get(WorkflowRun, context.workflow_run_id)
        return bool(
            run is not None
            and run.entity_id == entity_id
            and run.workspace_id == workspace_id
            and workflow_run_is_user_requested(run)
        )
    # `task_id` is also a public business target on several tools. Only the
    # Runtime envelope can establish that Task as this invocation's parent.
    parent_task_id = getattr(context.runtime_envelope, "task_id", None)
    if parent_task_id:
        from packages.core.models.task import Task
        from packages.core.services.task_execution_origin import task_has_manual_execution

        task = await db.get(Task, parent_task_id)
        return bool(
            task is not None
            and task.entity_id == entity_id
            and task.workspace_id == workspace_id
            and task_has_manual_execution(task)
        )
    return False
