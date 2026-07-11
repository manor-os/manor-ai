"""Workflow service — CRUD for definitions & runs, step execution engine."""
from __future__ import annotations

import logging
import operator
import re
import time
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    ChatSurface,
    attach_runtime_meta,
    runtime_execute_workflow_tool_step,
    runtime_invoke_skill,
    runtime_persist_workflow_service_runtime_events,
    runtime_prepare_named_tool_surface_for_turn,
    runtime_prepare_trace_envelope_for_turn,
    runtime_request_for_surface_turn,
    runtime_workflow_run_context,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun

logger = logging.getLogger(__name__)


# ── Workflow Definitions ──

async def list_workflows(db: AsyncSession, entity_id: str) -> list[WorkflowDefinition]:
    result = await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.entity_id == entity_id)
        .order_by(WorkflowDefinition.created_at.desc())
    )
    return list(result.scalars().all())


async def get_workflow(db: AsyncSession, workflow_id: str, entity_id: str) -> WorkflowDefinition | None:
    result = await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.id == workflow_id, WorkflowDefinition.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def create_workflow(
    db: AsyncSession,
    entity_id: str,
    name: str,
    steps: list,
    *,
    description: str | None = None,
    trigger_type: str = "manual",
    trigger_config: dict | None = None,
    variables: dict | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
) -> WorkflowDefinition:
    wf = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        steps=steps,
        description=description,
        trigger_type=trigger_type,
        trigger_config=trigger_config or {},
        variables=variables or {},
        category=category,
        tags=tags or [],
    )
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    return wf


async def update_workflow(
    db: AsyncSession, workflow_id: str, entity_id: str, **kwargs
) -> WorkflowDefinition | None:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(wf, key):
            setattr(wf, key, value)
    await db.flush()
    await db.refresh(wf)
    return wf


async def delete_workflow(db: AsyncSession, workflow_id: str, entity_id: str) -> bool:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return False
    await db.delete(wf)
    await db.flush()
    return True


# ── Workflow Runs ──

async def start_workflow(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str,
    *,
    variables: dict | None = None,
    trigger_data: dict | None = None,
    started_by: str | None = None,
) -> WorkflowRun:
    """Start a new workflow run."""
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        raise ValueError("Workflow not found")

    # Merge workflow-level default variables with runtime overrides
    merged_vars = dict(wf.variables or {})
    if variables:
        merged_vars.update(variables)

    steps = wf.steps or []
    first_step_id = steps[0]["id"] if steps else None

    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow_id,
        entity_id=entity_id,
        status="running",
        current_step_id=first_step_id,
        variables=merged_vars,
        step_results={},
        trigger_data=trigger_data or {},
        started_by=started_by,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, run_id: str, entity_id: str) -> WorkflowRun | None:
    result = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def list_runs(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[WorkflowRun]:
    q = select(WorkflowRun).where(WorkflowRun.entity_id == entity_id)
    if workflow_id:
        q = q.where(WorkflowRun.workflow_id == workflow_id)
    if status:
        q = q.where(WorkflowRun.status == status)
    q = q.order_by(WorkflowRun.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Step Execution Engine ──

async def execute_workflow_step(db: AsyncSession, run_id: str, entity_id: str) -> dict:
    """Execute the next step in a workflow run.

    Step types:
    - "agent": invoke a skill or run agentic loop
    - "tool": execute a single tool
    - "condition": evaluate expression against variables
    - "wait": pause and wait for external input
    - "transform": transform variables (map/filter data)
    - "notify": send notification

    Returns: {step_id, status, output, next_step_id}
    """
    run = await get_run(db, run_id, entity_id)
    if not run or run.status not in ("running", "pending"):
        return {"error": "Run not active"}

    workflow = await get_workflow(db, run.workflow_id, entity_id)
    if not workflow:
        return {"error": "Workflow not found"}

    steps = workflow.steps or []
    current_id = run.current_step_id

    # Find current step
    step = None
    if current_id:
        step = next((s for s in steps if s["id"] == current_id), None)
    else:
        step = steps[0] if steps else None

    if not step:
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return {"status": "completed"}

    # Execute based on type
    start = time.time()
    result: dict = {"step_id": step["id"], "status": "completed", "output": None}
    runtime_context = runtime_workflow_run_context(run)
    runtime_envelope = None

    try:
        step_type = step.get("type", "tool")
        config = step.get("config", {})

        # Attribute execution to whoever started the run — MCP calls use
        # this to resolve the caller's personal OAuth tokens.
        user_id = run.started_by or None

        if step_type == "agent":
            # Use skill system or agentic loop
            skill_name = config.get("skill", "")
            input_text = _render_variables(config.get("input", ""), run.variables or {})
            configured_tools = {str(name) for name in (config.get("tools") or []) if name}
            allowed_tool_names = None
            runtime_request = runtime_request_for_surface_turn(
                surface=ChatSurface.WORKFLOW_AGENT_STEP,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=runtime_context.get("workspace_id"),
                conversation_id=runtime_context.get("conversation_id"),
                task_id=runtime_context.get("task_id"),
                message=input_text,
                legacy_path="services.workflow_service.agent_step",
            )
            if configured_tools:
                runtime_surface_result = runtime_prepare_named_tool_surface_for_turn(
                    runtime_request,
                    tool_names=configured_tools,
                )
                runtime_envelope = runtime_surface_result.envelope
                allowed_tool_names = runtime_surface_result.allowed_tool_names
            else:
                runtime_envelope = runtime_prepare_trace_envelope_for_turn(runtime_request)
            skill_result = await runtime_invoke_skill(
                db,
                skill_name,
                entity_id,
                input_text,
                user_id=user_id,
                workspace_id=runtime_context.get("workspace_id"),
                conversation_id=runtime_context.get("conversation_id"),
                task_id=runtime_context.get("task_id"),
                allowed_tool_names=allowed_tool_names,
                runtime_envelope=runtime_envelope,
            )
            result["output"] = skill_result.get("content", "")

        elif step_type == "tool":
            tool_name = config.get("tool", "")
            args = {
                k: _render_variables(str(v), run.variables or {})
                for k, v in config.get("args", {}).items()
            }
            runtime_request = runtime_request_for_surface_turn(
                surface=ChatSurface.WORKFLOW_AGENT_STEP,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=runtime_context.get("workspace_id"),
                conversation_id=runtime_context.get("conversation_id"),
                task_id=runtime_context.get("task_id"),
                message=str(step.get("name") or tool_name),
                legacy_path="services.workflow_service.tool_step",
            )
            tool_step_result = await runtime_execute_workflow_tool_step(
                request=runtime_request,
                tool_name=tool_name,
                arguments=args,
            )
            runtime_envelope = tool_step_result.envelope
            result["output"] = tool_step_result.output

        elif step_type == "condition":
            expr = config.get("expression", "true")
            condition_met = _evaluate_condition(expr, run.variables or {})
            result["output"] = condition_met
            # Determine next step based on condition
            if condition_met:
                next_steps = step.get("true_next", step.get("next", []))
            else:
                next_steps = step.get("false_next", [])
            result["next_override"] = next_steps[0] if next_steps else None

        elif step_type == "wait":
            run.status = "paused"
            result["status"] = "waiting"

        elif step_type == "notify":
            # Placeholder for notification integration
            result["output"] = "Notification sent"

        elif step_type == "transform":
            # Update variables
            transforms = config.get("set", {})
            updated_vars = dict(run.variables or {})
            for key, value in transforms.items():
                updated_vars[key] = _render_variables(str(value), run.variables or {})
            run.variables = updated_vars
            result["output"] = updated_vars

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        logger.error("Workflow step %s failed: %s", step["id"], e)

    attach_runtime_meta(result, runtime_envelope)
    result["duration_ms"] = (time.time() - start) * 1000

    # Record step result
    step_results = dict(run.step_results or {})
    step_results[step["id"]] = result
    run.step_results = step_results

    # Advance to next step
    if result["status"] == "completed":
        next_id = result.get("next_override") or (
            step.get("next", [None])[0] if step.get("next") else None
        )
        if next_id:
            run.current_step_id = next_id
        else:
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
    elif result["status"] == "failed":
        run.status = "failed"
        run.error = result.get("error")

    await db.flush()
    await db.refresh(run)
    await runtime_persist_workflow_service_runtime_events(
        runtime_envelope,
    )
    return result


# ── Helpers ──

def _render_variables(template: str, variables: dict) -> str:
    """Replace {{var}} in template with variable values."""
    def replacer(m):
        return str(variables.get(m.group(1).strip(), m.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", replacer, template)


def _evaluate_condition(expression: str, variables: dict) -> bool:
    """Simple condition evaluator. Supports: var > N, var == 'val', var != N, etc."""
    ops = {
        "==": operator.eq,
        "!=": operator.ne,
        ">=": operator.ge,
        "<=": operator.le,
        ">": operator.gt,
        "<": operator.lt,
    }
    for op_str, op_fn in sorted(ops.items(), key=lambda x: -len(x[0])):
        if op_str in expression:
            parts = expression.split(op_str, 1)
            left = parts[0].strip()
            right = parts[1].strip()
            left_val = variables.get(left, left)
            try:
                left_val = float(left_val)
                right_val = float(right)
            except (ValueError, TypeError):
                right_val = right.strip("'\"")
                left_val = str(left_val)
            return op_fn(left_val, right_val)
    return bool(variables.get(expression.strip(), False))
