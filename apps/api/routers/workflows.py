"""Workflow endpoints — definitions, runs, and step execution."""
from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.workflow_import import UnknownWorkflowFormat, import_workflow
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.database import get_db
from packages.core.models.permission import Capability, ResourceType, Visibility
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowDefinition
from packages.core.services import workflow_service as svc
from packages.core.services.workflow_run_trace import summarize_trace_text
from packages.core.services.resource_access import (
    ResourceDescriptor,
    is_read_capability,
    user_can_access_resource,
)
from packages.core.services.workspace_access import (
    is_entity_admin_role,
    user_can_control_workspace_run,
    user_can_read_workspace_id,
    user_readable_workspace_ids,
    user_can_write_workspace_id,
    user_writable_workspace_ids,
)
from apps.api.deps import get_current_user
from apps.api.routers.workspace_chat import WORKSPACE_WORKFLOW_RUN_ACTION_KINDS
from packages.core.constants.pending_actions import PendingActionKind

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


async def _require_workflow(
    db: AsyncSession,
    user: User,
    workflow_id: str,
    capability: str = Capability.VIEW,
) -> WorkflowDefinition:
    """Load a workflow definition and authorize through the unified gateway.

    ``created_by`` acts as the owner — this table already tracked its creator,
    so no separate owner column was needed. A failed read yields 404, a failed
    write on a readable workflow yields 403.
    """
    wf = await svc.get_workflow(db, workflow_id, user.entity_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")

    descriptor = ResourceDescriptor.from_row(wf, ResourceType.WORKFLOW)
    if await user_can_access_resource(
        db,
        descriptor=descriptor,
        entity_id=user.entity_id,
        user_id=user.id,
        role=getattr(user, "role", None),
        capability=capability,
    ):
        return wf

    if is_read_capability(capability):
        raise HTTPException(404, "Workflow not found")
    if await user_can_access_resource(
        db,
        descriptor=descriptor,
        entity_id=user.entity_id,
        user_id=user.id,
        role=getattr(user, "role", None),
        capability=Capability.VIEW,
    ):
        raise HTTPException(403, "Insufficient permissions for this workflow")
    raise HTTPException(404, "Workflow not found")


# ── Schemas ──

class WorkflowCreateRequest(BaseModel):
    name: str
    steps: list[dict]
    description: str | None = None
    icon: str = "flow"
    trigger_type: str = "manual"
    trigger_config: dict = {}
    variables: dict = {}
    category: str | None = None
    tags: list[str] = []
    # Scope. A null workspace keeps the workflow shared entity-wide; naming
    # one binds it to that workspace's membership rules.
    workspace_id: str | None = None
    visibility: str = Visibility.ENTITY


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    steps: list[dict] | None = None
    description: str | None = None
    icon: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    variables: dict | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_active: bool | None = None
    status: str | None = None


class WorkflowResponse(BaseModel):
    id: str
    entity_id: str
    created_by: str | None = None
    name: str
    description: str | None = None
    icon: str = "flow"
    trigger_type: str
    trigger_config: dict = {}
    steps: list[dict] = []
    variables: dict = {}
    category: str | None = None
    tags: list[str] = []
    is_active: bool = True
    version: int = 1
    status: str = "active"
    created_at: datetime
    updated_at: datetime | None = None


class WorkflowImportRequest(BaseModel):
    content: str  # raw exported workflow (JSON for n8n/ComfyUI, YAML for Dify)
    name: str | None = None
    workspace_id: str | None = None
    business_line: str | None = None
    create_binding: bool = False
    dry_run: bool = False  # preview detection + report without persisting


class AiEditRequest(BaseModel):
    prompt: str
    current_steps: list[dict] | None = None  # present = edit existing graph


class RunNodeRequest(BaseModel):
    step: dict  # a single step definition: {id, type, name, config, ...}
    variables: dict | None = None  # optional upstream context for {{templates}}


class BindingCreateRequest(BaseModel):
    workflow_id: str
    workspace_id: str | None = None
    business_line: str | None = None
    name: str | None = None
    trigger_type: str = "manual"
    trigger_config: dict = {}
    variables: dict = {}
    config: dict = {}


class BindingUpdateRequest(BaseModel):
    workflow_id: str | None = None
    workspace_id: str | None = None
    business_line: str | None = None
    name: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    variables: dict | None = None
    config: dict | None = None
    enabled: bool | None = None
    status: str | None = None


class WorkflowTriggerRequest(BaseModel):
    trigger_type: str = "event"  # event | workspace_event | schedule | webhook
    event_name: str | None = None
    workspace_id: str | None = None
    trigger_data: dict = {}


class RunStartRequest(BaseModel):
    variables: dict | None = None
    trigger_data: dict | None = None
    execute: bool = True  # run inline to completion; false = create only (manual /step)


class RunResponse(BaseModel):
    id: str
    workflow_id: str
    entity_id: str
    workspace_id: str | None = None
    binding_id: str | None = None
    trigger_source: str | None = None
    status: str
    current_step_id: str | None = None
    variables: dict = {}
    step_results: dict = {}
    trigger_data: dict = {}
    error: str | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    retry_of_run_id: str | None = None
    retry_from_step_id: str | None = None
    attempt_number: int = 1
    lineage_status: str = "canonical"
    workflow_name: str | None = None
    current_step_name: str | None = None
    workflow_definition_fingerprint: str | None = None
    definition_snapshot: dict = {}
    execution_trace: list = []
    capabilities: dict[str, bool] | None = None
    workflow_steps: list[dict] | None = None
    business_outcome: str | None = None
    intervention: dict | None = None
    processed_count: int | None = None
    total_count: int | None = None
    artifact_count: int | None = None
    history_blocker: object | None = None


class ResumeRequest(BaseModel):
    variables: dict | None = None
    execute: bool = True


class RetryRunRequest(BaseModel):
    from_step_id: str | None = None
    variables: dict | None = None
    execute: bool = True


# ── Helpers ──

def _wf_to_dict(wf) -> dict:
    return WorkflowResponse(
        id=wf.id,
        entity_id=wf.entity_id,
        created_by=wf.created_by,
        name=wf.name,
        description=wf.description,
        icon=wf.icon or "flow",
        trigger_type=wf.trigger_type,
        trigger_config=wf.trigger_config or {},
        steps=wf.steps or [],
        variables=wf.variables or {},
        category=wf.category,
        tags=wf.tags or [],
        is_active=wf.is_active,
        version=wf.version,
        status=wf.status,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
    ).model_dump()


def _run_to_dict(
    run,
    *,
    include_detail: bool = True,
    summary: bool = False,
    can_control: bool | None = None,
    compact_state: dict | None = None,
) -> dict:
    loaded_trigger_data = run.__dict__.get("trigger_data")
    loaded_trigger_data = (
        loaded_trigger_data if isinstance(loaded_trigger_data, dict) else {}
    )
    trigger_data = {} if summary else loaded_trigger_data
    workflow_name = (
        getattr(run, "summary_workflow_name", None)
        if summary
        else None
    ) or loaded_trigger_data.get("_workflow_name")
    workflow_name = workflow_name if isinstance(workflow_name, str) else None
    node_names = loaded_trigger_data.get("_workflow_node_names")
    node_names = node_names if isinstance(node_names, dict) else {}
    current_step_name = (
        getattr(run, "summary_current_step_name", None)
        if summary
        else None
    ) or node_names.get(run.current_step_id)
    current_step_name = (
        current_step_name if isinstance(current_step_name, str) else None
    )
    persisted_history_state = _persisted_workflow_history_state(run)
    history_blocker = persisted_history_state.get("history_blocker")
    summary_error = None
    if summary and history_blocker is not None:
        summary_error = (
            history_blocker
            if isinstance(history_blocker, str)
            else summarize_trace_text(history_blocker)
        )
    response_data = {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "entity_id": run.entity_id,
        "workspace_id": run.workspace_id,
        "binding_id": run.binding_id,
        "trigger_source": run.trigger_source,
        "status": run.status,
        "current_step_id": run.current_step_id,
        "error": summary_error if summary else run.error,
        "started_by": run.started_by,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "updated_at": run.__dict__.get("updated_at"),
        "retry_of_run_id": (
            run.summary_effective_retry_of_run_id
            if summary
            else run.effective_retry_of_run_id
        ),
        "retry_from_step_id": (
            run.summary_effective_retry_from_step_id
            if summary
            else run.effective_retry_from_step_id
        ),
        "attempt_number": run.effective_attempt_number,
        "lineage_status": run.lineage_status,
        "workflow_name": workflow_name,
        "current_step_name": current_step_name,
    }
    response_data.update(persisted_history_state)
    if not summary:
        variables = run.variables or {}
        project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
        state = project.get("state") if isinstance(project.get("state"), dict) else {}
        response_data.update({
            "variables": variables,
            "step_results": run.step_results or {},
            "trigger_data": trigger_data,
            "workflow_definition_fingerprint": trigger_data.get(
                "_workflow_definition_fingerprint"
            ),
            "business_outcome": state.get("business_outcome"),
        })
    if can_control is not None:
        response_data["capabilities"] = {"can_control": can_control}
    if summary:
        response_data.update(compact_state or {})
    if include_detail:
        response_data.update({
            "definition_snapshot": getattr(run, "definition_snapshot", None) or {},
            "execution_trace": getattr(run, "execution_trace", None) or [],
        })
    return RunResponse(**response_data).model_dump(
        mode="json",
        exclude=(
            {
                "variables",
                "step_results",
                "trigger_data",
                "definition_snapshot",
                "execution_trace",
                "workflow_definition_fingerprint",
            }
            if summary
            else {
                "workflow_steps",
                "intervention",
                *(() if include_detail else ("definition_snapshot", "execution_trace")),
            }
        ),
    )


def _persisted_workflow_history_state(run) -> dict:
    summary = getattr(run, "summary_history_state", None)
    if not isinstance(summary, dict):
        trigger_data = run.__dict__.get("trigger_data")
        trigger_data = trigger_data if isinstance(trigger_data, dict) else {}
        summary = trigger_data.get("_workflow_history_summary")
    if not isinstance(summary, dict):
        return {}

    def count(key: str) -> int | None:
        value = summary.get(key)
        return max(0, int(value)) if isinstance(value, int) and not isinstance(value, bool) else None

    outcome = summary.get("business_outcome")
    return {
        "business_outcome": outcome if isinstance(outcome, str) and outcome else None,
        "processed_count": count("processed_count"),
        "total_count": count("total_count"),
        "artifact_count": count("artifact_count"),
        "history_blocker": summary.get("blocker"),
    }


def _compact_workflow_run_state(run) -> dict:
    from packages.core.services.workflow_chat_projection import (
        _workflow_business_state,
        _workflow_error_payload,
        _workflow_retry_input_schema,
        _workflow_retry_values,
        workflow_progress_steps,
    )
    from packages.core.services.workflow_run_trace import summarize_trace_value

    workflow_steps = workflow_progress_steps(
        run,
        activity_status=str(run.status or ""),
    )
    business_outcome, retry_state = _workflow_business_state(run)
    raw_error = _workflow_error_payload(run)
    error = summarize_trace_value(raw_error) if raw_error is not None else None
    current_step_result = (run.step_results or {}).get(run.current_step_id)
    current_step_result = (
        current_step_result if isinstance(current_step_result, dict) else {}
    )
    retry_from_step_id = str(
        retry_state.get("retry_from_step_id")
        or run.effective_retry_from_step_id
        or (run.current_step_id if run.status == "failed" else "")
        or ""
    ).strip()
    retryable = run.status == "failed" or (
        run.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    )
    intervention = None
    if retryable and retry_from_step_id:
        editable_input_schema = _workflow_retry_input_schema(run, retry_state)
        observed_problem = retry_state.get("observed_problem")
        intervention = {
            "kind": "workflow_retry",
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "business_outcome": business_outcome,
            "phase": retry_state.get("phase") or "execution",
            "step_id": retry_state.get("step_id") or run.current_step_id,
            "retry_from_step_id": retry_from_step_id,
            "retry_segment_ids": summarize_trace_value(
                retry_state.get("segment_ids") or []
            ),
            "observed_problem": summarize_trace_value(
                error if observed_problem is None else observed_problem
            ),
            "required_change": summarize_trace_value(
                retry_state.get("required_change")
                or "Correct the failed input or external state, then retry this step."
            ),
            "editable_input_schema": summarize_trace_value(editable_input_schema),
            "preserved_receipts": summarize_trace_value(
                retry_state.get("preserved_receipts") or []
            ),
            "values": summarize_trace_value(
                _workflow_retry_values(run, retry_state, editable_input_schema)
            ),
            "options": ["retry", "cancel"],
        }
    elif (
        run.status == "paused"
        and run.current_step_id
        and str(current_step_result.get("wait_type") or "").lower() == "event"
    ):
        observed_problem = (
            current_step_result.get("error")
            or current_step_result.get("output_summary")
            or current_step_result.get("output")
            or current_step_result.get("prompt")
            or current_step_result.get("message")
        )
        if observed_problem is None:
            trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
            node_names = trigger_data.get("_workflow_node_names")
            node_names = node_names if isinstance(node_names, dict) else {}
            step_name = str(node_names.get(run.current_step_id) or run.current_step_id)
            observed_problem = f"{step_name} is paused and waiting to resume."
        intervention = {
            "kind": "workflow_resume",
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "step_id": run.current_step_id,
            "observed_problem": summarize_trace_value(observed_problem),
            "options": ["resume", "cancel"],
        }
    return {
        "workflow_steps": workflow_steps,
        "business_outcome": business_outcome,
        "error": error,
        "intervention": intervention,
    }


async def _message_backed_workflow_intervention(
    db: AsyncSession,
    run,
) -> dict | None:
    if run.trigger_source != "workspace_chat" or not run.workspace_id:
        return None
    from sqlalchemy import or_, select

    from packages.core.models.task import Conversation, Message
    from packages.core.services.workflow_run_trace import summarize_trace_value

    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    context = trigger_data.get("_workspace_chat_entrypoint")
    if not isinstance(context, dict):
        return None
    conversation_id = str(context.get("conversation_id") or "").strip()
    if not conversation_id:
        return None

    conditions = (
        Conversation.id == conversation_id,
        Conversation.entity_id == run.entity_id,
        Conversation.workspace_id == run.workspace_id,
        Message.conversation_id == conversation_id,
        Message.pending_action.isnot(None),
        Message.pending_action["kind"].as_string().in_(
            tuple(WORKSPACE_WORKFLOW_RUN_ACTION_KINDS)
        ),
        Message.pending_action["workflow_run_id"].as_string() == run.id,
        or_(
            Message.resolved_at.is_(None),
            Message.pending_action["kind"].as_string()
            == PendingActionKind.WORKFLOW_RETRY.value,
        ),
    )
    activity_message_id = str(context.get("activity_message_id") or "").strip()
    message = None
    if activity_message_id:
        message = (await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == activity_message_id, *conditions)
            .limit(1)
        )).scalar_one_or_none()
    if message is None:
        message = (await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(*conditions)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )).scalar_one_or_none()
    action = message.pending_action if message and isinstance(message.pending_action, dict) else None
    if message and message.resolved_at is not None:
        latest_attempt = await svc.latest_workflow_run_family_attempt(db, run)
        if latest_attempt.status != "cancelled":
            return None
    sanitized = summarize_trace_value(action) if action else None
    if not isinstance(sanitized, dict):
        return None
    sanitized["message_id"] = message.id
    sanitized["source"] = "workspace_chat"
    return sanitized


async def _run_can_control(db: AsyncSession, run, user: User) -> bool:
    return await user_can_control_workspace_run(
        db,
        run=run,
        user_id=user.id,
        entity_role=user.role,
    )


async def _run_control_capabilities(
    db: AsyncSession,
    runs: list,
    user: User,
) -> dict[str, bool]:
    workspace_ids = {
        str(run.workspace_id)
        for run in runs
        if getattr(run, "workspace_id", None)
    }
    writable_workspace_ids = await user_writable_workspace_ids(
        db,
        workspace_ids=workspace_ids,
        user_id=user.id,
        role=user.role,
    )
    entity_admin = is_entity_admin_role(user.role)
    return {
        run.id: bool(
            str(getattr(run, "started_by", "") or "") == str(user.id)
            or entity_admin
            or str(getattr(run, "workspace_id", "") or "") in writable_workspace_ids
        )
        for run in runs
    }


async def _require_run_read(db: AsyncSession, run, user: User) -> None:
    if run.workspace_id and not await user_can_read_workspace_id(
        db,
        workspace_id=run.workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    ):
        raise HTTPException(404, "Run not found")


async def _require_run_control(db: AsyncSession, run, user: User) -> None:
    if not await _run_can_control(db, run, user):
        raise HTTPException(403, "Workflow Run control permission required")


# ── Workflow Definition — collection endpoints ──

@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await svc.list_workflows(db, user.entity_id)
    visible = [
        w for w in items
        if await user_can_access_resource(
            db,
            descriptor=ResourceDescriptor.from_row(w, ResourceType.WORKFLOW),
            entity_id=user.entity_id,
            user_id=user.id,
            role=getattr(user, "role", None),
            capability=Capability.VIEW,
        )
    ]
    return [_wf_to_dict(w) for w in visible]


@router.post("", status_code=201)
async def create_workflow(
    body: WorkflowCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.workspace_id and not await user_can_write_workspace_id(
        db,
        workspace_id=body.workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        role=getattr(user, "role", None),
    ):
        raise HTTPException(403, "Cannot create a workflow in this workspace")
    wf = await svc.create_workflow(
        db,
        entity_id=user.entity_id,
        created_by=user.id,
        name=body.name,
        steps=body.steps,
        description=body.description,
        icon=body.icon,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        variables=body.variables,
        category=body.category,
        tags=body.tags,
        workspace_id=body.workspace_id,
        visibility=body.visibility,
    )
    await db.commit()
    return _wf_to_dict(wf)


@router.post("/ai-edit")
async def ai_edit_workflow(
    body: AiEditRequest,
    user: User = Depends(get_current_user),
):
    """AI-generate or edit a workflow graph, streaming progress as SSE.

    Mirrors the agent ``/generate-stream`` endpoint: emits ``step`` frames to
    keep the connection alive past Cloudflare's 100s 524 timeout, then a
    terminal ``done`` (``{name, steps, variables}``) or ``error`` frame. The
    result is NOT persisted — the canvas applies it via the normal update path,
    so the user can review / undo before saving.
    """
    from fastapi.responses import StreamingResponse

    from packages.core.services.workflow_generator import generate_workflow_streaming
    from packages.core.services.sse_events import format_sse

    async def event_stream():
        try:
            result = None
            async for kind, payload in generate_workflow_streaming(
                body.prompt, body.current_steps, user.entity_id,
            ):
                if kind == "step":
                    yield format_sse("step", {"label": payload})
                elif kind == "workflow":
                    result = payload
            if result is None:
                yield format_sse("error", {"message": "Workflow generation did not produce a result"})
                return
            yield format_sse("done", result)
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            logger.exception("workflow ai-edit failed")
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/run-node")
async def run_node(
    body: RunNodeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a single node standalone (n8n "Execute node" / ComfyUI-style).

    Runs the given step definition in isolation with an ephemeral, non-persisted
    run context so the user can test a node's config (and verify its output)
    without running the whole workflow. Returns the step result.
    """
    from packages.core.models.workflow import WorkflowRun
    from packages.core.ai.workflow_runner import WorkflowRunner

    run = WorkflowRun(
        id="adhoc",
        workflow_id="adhoc",
        entity_id=user.entity_id,
        status="running",
        variables=dict(body.variables or {}),
        step_results={},
        trigger_data={},
        started_by=user.id,
    )
    started_at = perf_counter()
    try:
        # Use the same guarded path as a workflow run so standalone tests get
        # typed input binding, retry/timeout behavior, and the resolved input
        # snapshot shown by the node editor.
        result = await WorkflowRunner()._execute_step_safe(body.step, run, db)
    except Exception as exc:  # noqa: BLE001 — surface failures, never 500 the editor
        logger.exception("run-node failed")
        return {
            "status": "failed",
            "error": str(exc),
            "step_id": body.step.get("id"),
            "inputs": dict(body.variables or {}),
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
        }
    return {
        **result,
        "step_id": body.step.get("id"),
        "inputs": result.get("inputs", dict(body.variables or {})),
        "duration_ms": round((perf_counter() - started_at) * 1000, 2),
    }


@router.post("/import")
async def import_workflow_endpoint(
    body: WorkflowImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a workflow exported from ComfyUI, n8n, or Dify.

    ``dry_run`` returns the detected format + import report (coverage, unmapped
    nodes) without persisting, so the UI can preview before committing.
    """
    try:
        if body.dry_run:
            result = import_workflow(body.content, name=body.name)
            return {"report": result.report.to_dict(), "definition": result.definition}

        wf, binding, report = await svc.import_workflow_definition(
            db,
            entity_id=user.entity_id,
            created_by=user.id,
            raw=body.content,
            name=body.name,
            workspace_id=body.workspace_id,
            business_line=body.business_line,
            create_binding=body.create_binding,
        )
        await db.commit()
        return {
            "workflow": _wf_to_dict(wf),
            "binding_id": binding.id if binding else None,
            "report": report,
        }
    except UnknownWorkflowFormat as exc:
        raise HTTPException(422, str(exc))


# ── Workflow Bindings + Triggers ──

def _binding_to_dict(b) -> dict:
    return {
        "id": b.id,
        "entity_id": b.entity_id,
        "workflow_id": b.workflow_id,
        "workspace_id": b.workspace_id,
        "business_line": b.business_line,
        "name": b.name,
        "trigger_type": b.trigger_type,
        "trigger_config": b.trigger_config or {},
        "variables": b.variables or {},
        "config": b.config or {},
        "enabled": b.enabled,
        "status": b.status,
    }


@router.post("/bindings", status_code=201)
async def create_binding(
    body: BindingCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deploy a workflow into a run context (entity / workspace / business line).

    A ``schedule`` trigger is an automation, not a binding — it creates a
    ScheduledJob (the workflow stays independent) and returns the automation.
    """
    wf = await svc.get_workflow(db, body.workflow_id, user.entity_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")

    if body.trigger_type == "schedule":
        cfg = body.trigger_config or {}
        cron = cfg.get("cron") or cfg.get("schedule")
        if not cron:
            raise HTTPException(400, "A schedule trigger needs a 'cron' in trigger_config")
        job = await svc.schedule_workflow(
            db,
            entity_id=user.entity_id,
            workflow_id=body.workflow_id,
            cron=str(cron),
            name=body.name or wf.name,
            workspace_id=body.workspace_id,
            timezone_str=str(cfg.get("timezone") or "UTC"),
            created_by=user.id,
        )
        await db.commit()
        return {
            "kind": "automation",
            "scheduled_job_id": job.id,
            "job_id": job.job_id,
            "cron": job.cron_expr,
            "workflow_id": body.workflow_id,
            "workspace_id": body.workspace_id,
        }

    if body.trigger_type == "manual" and body.workspace_id:
        existing = await svc.list_bindings(
            db,
            user.entity_id,
            workspace_id=body.workspace_id,
            workflow_id=body.workflow_id,
        )
        if any(item.trigger_type == "manual" for item in existing):
            raise HTTPException(409, "Workflow is already attached to this workspace")

    reference_id = (body.config or {}).get("workspace_workflow_binding_id")
    if reference_id:
        try:
            await svc.validate_workspace_workflow_reference(
                db,
                user.entity_id,
                str(reference_id),
                workflow_id=body.workflow_id,
                workspace_id=body.workspace_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    binding = await svc.create_workflow_binding(
        db,
        entity_id=user.entity_id,
        workflow_id=body.workflow_id,
        workspace_id=body.workspace_id,
        business_line=body.business_line,
        name=body.name or wf.name,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        variables=body.variables,
        config=body.config,
    )
    await db.commit()
    return {"kind": "binding", **_binding_to_dict(binding)}


@router.get("/bindings")
async def list_bindings(
    workspace_id: str | None = Query(None),
    business_line: str | None = Query(None),
    workflow_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await svc.list_bindings(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        business_line=business_line,
        workflow_id=workflow_id,
    )
    return [_binding_to_dict(b) for b in items]


@router.put("/bindings/{binding_id}")
async def update_binding(
    binding_id: str,
    body: BindingUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.trigger_type == "schedule":
        raise HTTPException(400, "Schedules are managed as automations")
    current = await svc.get_binding(db, binding_id, user.entity_id)
    if not current:
        raise HTTPException(404, "Binding not found")
    if body.workflow_id:
        workflow = await svc.get_workflow(db, body.workflow_id, user.entity_id)
        if not workflow:
            raise HTTPException(404, "Workflow not found")
    effective_config = body.config if body.config is not None else current.config
    reference_id = (effective_config or {}).get("workspace_workflow_binding_id")
    if reference_id:
        try:
            await svc.validate_workspace_workflow_reference(
                db,
                user.entity_id,
                str(reference_id),
                workflow_id=body.workflow_id or current.workflow_id,
                workspace_id=body.workspace_id or current.workspace_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    binding = await svc.update_binding(
        db,
        binding_id,
        user.entity_id,
        **body.model_dump(exclude_unset=True),
    )
    if not binding:
        raise HTTPException(404, "Binding not found")
    await db.commit()
    return _binding_to_dict(binding)


@router.post("/bindings/{binding_id}/run", status_code=201)
async def run_binding(
    binding_id: str,
    body: RunStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test or manually run a deployed workspace workflow binding."""
    binding = await svc.get_binding(db, binding_id, user.entity_id)
    if not binding:
        raise HTTPException(404, "Binding not found")
    try:
        execution_binding = await svc.execution_binding_for_automation(db, binding)
        trigger_data = dict(body.trigger_data if body and body.trigger_data else {"manual_test": True})
        if execution_binding.id != binding.id:
            trigger_data.setdefault("automation_binding_id", binding.id)
        run = await svc.start_workflow_from_binding(
            db,
            execution_binding,
            variables=body.variables if body else None,
            trigger_data=trigger_data,
            trigger_source="manual",
            started_by=user.id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()

    if body is None or body.execute:
        try:
            await WorkflowRunner().run(run.id)
        except Exception:
            logger.exception("inline workflow binding run failed: %s", run.id)
        await db.refresh(run)
    else:
        WorkflowRunner.enqueue(run.id)
    return _run_to_dict(run)


@router.delete("/bindings/{binding_id}", status_code=204)
async def delete_binding(
    binding_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await svc.get_binding(db, binding_id, user.entity_id)
    if not binding:
        raise HTTPException(404, "Binding not found")
    if binding.workspace_id and binding.trigger_type == "manual":
        references = await svc.binding_automation_references(db, binding)
        if references:
            raise HTTPException(
                409,
                "Remove automations that use this workspace workflow before detaching it",
            )
    if not await svc.delete_binding(db, binding_id, user.entity_id):
        raise HTTPException(404, "Binding not found")
    await db.commit()


@router.post("/trigger")
async def fire_trigger(
    body: WorkflowTriggerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fire a trigger — start a run for every enabled binding that matches."""
    runs = await svc.dispatch_trigger(
        db,
        user.entity_id,
        trigger_type=body.trigger_type,
        event_name=body.event_name,
        workspace_id=body.workspace_id,
        trigger_data=body.trigger_data,
        started_by=user.id,
    )
    await db.commit()
    # Drive execution in the background (best-effort; enqueue swallows broker errors)
    for run in runs:
        WorkflowRunner.enqueue(run.id)
    return {"started": len(runs), "runs": [_run_to_dict(r) for r in runs]}


@router.post("/webhook/{token}")
async def inbound_webhook(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public inbound webhook — fires every webhook binding matching ``token``.

    Unauthenticated by design: the token is the binding's shared secret. The
    request body (if JSON) is passed to the run as trigger_data.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"body": payload}

    runs = await svc.dispatch_webhook(db, token, payload=payload)
    await db.commit()
    # Run inline so a Respond-to-Webhook node can return a synchronous response
    # to the caller (n8n behaviour). The first run that sets ``__response`` wins.
    from fastapi.responses import JSONResponse, PlainTextResponse

    response = None
    for run in runs:
        await WorkflowRunner().run(run.id)
        await db.refresh(run)
        if response is None:
            response = (run.variables or {}).get("__response")
    if isinstance(response, dict):
        status = int(response.get("status", 200) or 200)
        body = response.get("body")
        if isinstance(body, (dict, list)):
            return JSONResponse(body, status_code=status)
        return PlainTextResponse("" if body is None else str(body), status_code=status)
    return {"started": len(runs), "run_ids": [r.id for r in runs]}


# ── Run endpoints (must be before /{workflow_id} to avoid route shadowing) ──

@router.get("/runs")
async def list_runs(
    workspace_id: str | None = Query(None),
    binding_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List workflow runs for Workspace and binding-level history surfaces."""
    readable_workspace_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        workspace_ids={workspace_id} if workspace_id else None,
    )
    if workspace_id and workspace_id not in readable_workspace_ids:
        raise HTTPException(404, "Workspace not found")
    runs = await svc.list_runs(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        status=status,
        limit=limit,
        readable_workspace_ids=readable_workspace_ids,
        summary=True,
    )
    can_control_by_run_id = await _run_control_capabilities(db, runs, user)
    return [
        _run_to_dict(
            run,
            include_detail=False,
            summary=True,
            can_control=can_control_by_run_id[run.id],
            compact_state=_persisted_workflow_history_state(run),
        )
        for run in runs
    ]


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    detail: bool = Query(True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if detail:
        run = await svc.get_run(db, run_id, user.entity_id)
    else:
        from sqlalchemy import select
        from sqlalchemy.orm import defer

        from packages.core.models.workflow import WorkflowRun

        run = (await db.execute(
            select(WorkflowRun)
            .options(
                defer(WorkflowRun.execution_trace, raiseload=True),
            )
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.entity_id == user.entity_id,
            )
        )).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_read(db, run, user)
    compact_state = None
    if not detail:
        compact_state = _compact_workflow_run_state(run)
        compact_intervention = compact_state.get("intervention")
        message_intervention = await _message_backed_workflow_intervention(db, run)
        retry_message_is_unavailable = (
            isinstance(compact_intervention, dict)
            and compact_intervention.get("kind") == PendingActionKind.WORKFLOW_RETRY
            and run.trigger_source == "workspace_chat"
            and message_intervention is None
        )
        if retry_message_is_unavailable:
            compact_state["intervention"] = None
        if message_intervention is not None:
            if (
                message_intervention.get("truncated") is True
                and isinstance(compact_intervention, dict)
            ):
                compact_state["intervention"] = {
                    **compact_intervention,
                    "message_id": message_intervention["message_id"],
                    "source": message_intervention["source"],
                }
            else:
                compact_state["intervention"] = message_intervention
    return _run_to_dict(
        run,
        include_detail=detail,
        summary=not detail,
        can_control=await _run_can_control(db, run, user),
        compact_state=compact_state,
    )


@router.get("/runs/{run_id}/family")
async def get_run_family(
    run_id: str,
    limit: int = Query(svc.RUN_FAMILY_MAX_RUNS, ge=1, le=svc.RUN_FAMILY_MAX_RUNS),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return bounded retry summaries with explicit lineage trust status."""
    selected = await svc.get_run_summary(db, run_id, user.entity_id)
    if not selected:
        raise HTTPException(404, "Run not found")
    await _require_run_read(db, selected, user)
    runs = await svc.list_run_family(
        db,
        run_id,
        user.entity_id,
        limit=limit,
        selected_run=selected,
    )
    if not runs:
        raise HTTPException(404, "Run not found")
    can_control_by_run_id = await _run_control_capabilities(db, runs, user)
    return [
        _run_to_dict(
            run,
            include_detail=False,
            summary=True,
            can_control=can_control_by_run_id[run.id],
            compact_state=_persisted_workflow_history_state(run),
        )
        for run in runs
    ]


@router.post("/runs/{run_id}/step")
async def execute_step(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    result = await svc.execute_workflow_step(db, run_id, user.entity_id)
    if "error" in result and result.get("error") and "status" not in result:
        raise HTTPException(400, result["error"])
    await db.commit()
    return result


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    if run.status in ("completed", "cancelled"):
        raise HTTPException(400, f"Run already {run.status}")
    run.status = "cancelled"
    await db.flush()
    await db.commit()
    return _run_to_dict(run)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: ResumeRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    if run.status != "paused":
        raise HTTPException(400, "Run is not paused")
    entity_id = user.entity_id
    resumed_by = user.id
    # Release the request session's read transaction before the runner takes a
    # row lock in its own session, then refresh the response after execution.
    await db.rollback()
    from packages.core.ai.workflow_runner import WorkflowRunner

    outcome = await WorkflowRunner.resume(
        run_id,
        body.variables if body else None,
        entity_id=entity_id,
        resumed_by=resumed_by,
        execute=body.execute if body else True,
    )
    if outcome == "not_found":
        raise HTTPException(404, "Run not found")
    if outcome == "not_paused":
        raise HTTPException(409, "Run was already resumed")
    if outcome == "invalid_approval":
        raise HTTPException(400, "Approval resume requires a valid decision from the run owner")
    if outcome == "definition_changed":
        raise HTTPException(409, "Workflow definition changed since this run started")
    await db.refresh(run)
    return _run_to_dict(run)


@router.post("/runs/{run_id}/retry", status_code=201)
async def retry_run(
    run_id: str,
    body: RetryRunRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing_run = await svc.get_run(db, run_id, user.entity_id)
    if not existing_run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, existing_run, user)
    try:
        run = await svc.retry_workflow_run(
            db,
            run_id=run_id,
            entity_id=user.entity_id,
            started_by=user.id,
            from_step_id=body.from_step_id if body else None,
            variables=body.variables if body else None,
        )
    except ValueError as exc:
        message = str(exc)
        normalized = message.lower()
        status_code = (
            404
            if "not found" in normalized
            else 409
            if "changed since" in normalized
            else 400
        )
        raise HTTPException(status_code, message) from exc
    await db.commit()

    if body is None or body.execute:
        from packages.core.ai.workflow_runner import WorkflowRunner

        try:
            await WorkflowRunner().run(run.id)
        except Exception:
            logger.exception("inline workflow retry failed: %s", run.id)
        await db.refresh(run)
    return _run_to_dict(run)


# ── Workflow Definition — single-item endpoints ──

@router.get("/{workflow_id}/metadata")
async def get_workflow_metadata(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workflow(db, user, workflow_id, Capability.VIEW)
    metadata = await svc.get_workflow_metadata(db, workflow_id, user.entity_id)
    if not metadata:
        raise HTTPException(404, "Workflow not found")
    return metadata


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wf = await _require_workflow(db, user, workflow_id, Capability.VIEW)
    return _wf_to_dict(wf)


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workflow(db, user, workflow_id, Capability.EDIT)
    wf = await svc.update_workflow(
        db, workflow_id, user.entity_id,
        **body.model_dump(exclude_none=True),
    )
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await db.commit()
    return _wf_to_dict(wf)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workflow(db, user, workflow_id, Capability.DELETE)
    deleted = await svc.delete_workflow(db, workflow_id, user.entity_id)
    if not deleted:
        raise HTTPException(404, "Workflow not found")
    await db.commit()


# ── Workflow-scoped run endpoints ──

async def _prime_run_cache(db: AsyncSession, workflow_id: str, exclude_run_id: str) -> dict:
    """ComfyUI-style incremental re-execution: seed the runner cache from the
    most recent prior run of this workflow, so cache-eligible steps whose inputs
    are unchanged are reused instead of re-executed. Used for editor re-runs
    (iterate on one node without re-running the whole graph); scheduled and
    triggered runs always execute fresh.
    """
    from sqlalchemy import select
    from packages.core.models.workflow import WorkflowRun

    res = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.id != exclude_run_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    prior = res.scalar_one_or_none()
    return WorkflowRunner.prime_cache_from_results(prior.step_results if prior else None)


@router.post("/{workflow_id}/run", status_code=201)
async def start_run(
    workflow_id: str,
    body: RunStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Running a workflow uses it without altering the definition, so it takes
    # the read path — consistent with invoking a skill.
    await _require_workflow(db, user, workflow_id, Capability.VIEW)
    try:
        run = await svc.start_workflow(
            db,
            entity_id=user.entity_id,
            workflow_id=workflow_id,
            variables=body.variables if body else None,
            trigger_data=body.trigger_data if body else None,
            started_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e).lower() else 409, str(e))
    await db.commit()

    # By default execute the run inline so it actually runs (no Celery worker in
    # dev). ``execute=false`` creates the run only (manual /step driving). The
    # runner manages its own session + commits; we refresh for final state.
    if body is None or body.execute:
        run_id = run.id
        cache_index = await _prime_run_cache(db, workflow_id, run_id)
        try:
            await WorkflowRunner(cache_index=cache_index).run(run_id)
        except Exception:
            logger.exception("inline workflow run failed: %s", run_id)
        await db.refresh(run)
    return _run_to_dict(run)


@router.post("/{workflow_id}/run-stream")
async def start_run_stream(
    workflow_id: str,
    body: RunStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run a workflow inline, streaming per-node status as SSE so the canvas
    lights up node-by-node. Emits ``node`` events ({id, status}) as each step
    runs/finishes, then a terminal ``done`` (the full run) or ``error`` frame.
    """
    await _require_workflow(db, user, workflow_id, Capability.VIEW)
    import asyncio
    from fastapi.responses import StreamingResponse

    from packages.core.database import async_session
    from packages.core.services.sse_events import format_sse

    try:
        run = await svc.start_workflow(
            db,
            entity_id=user.entity_id,
            workflow_id=workflow_id,
            variables=body.variables if body else None,
            trigger_data=body.trigger_data if body else None,
            started_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e).lower() else 409, str(e))
    await db.commit()
    run_id = run.id
    entity_id = user.entity_id
    # Reuse unchanged steps from the previous run (ComfyUI incremental re-exec).
    cache_index = await _prime_run_cache(db, workflow_id, run_id)

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict):
        await queue.put(("node", ev))

    async def driver():
        try:
            await WorkflowRunner(cache_index=cache_index).run(run_id, progress=emit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream workflow run failed: %s", run_id)
            await queue.put(("error", str(exc)))
        finally:
            await queue.put(("end", None))

    async def event_stream():
        yield format_sse("run", {"run_id": run_id})
        task = asyncio.create_task(driver())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "node":
                    yield format_sse("node", payload)
                elif kind == "error":
                    yield format_sse("error", {"message": payload})
                elif kind == "end":
                    break
            await task
            # Reload final run state from a fresh session (the request db may be
            # torn down once streaming begins; the runner owns its own session).
            async with async_session() as s:
                final = await svc.get_run(s, run_id, entity_id)
                yield format_sse("done", _run_to_dict(final) if final else {"id": run_id, "status": "unknown"})
        except Exception as exc:  # noqa: BLE001
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: str,
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workflow(db, user, workflow_id, Capability.VIEW)
    readable_workspace_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    runs = await svc.list_runs(
        db,
        user.entity_id,
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        readable_workspace_ids=readable_workspace_ids,
        summary=True,
    )
    can_control_by_run_id = await _run_control_capabilities(db, runs, user)
    return [
        _run_to_dict(
            run,
            include_detail=False,
            summary=True,
            can_control=can_control_by_run_id[run.id],
        )
        for run in runs
    ]
