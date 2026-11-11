"""Workflow endpoints — definitions, runs, and step execution."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.workflow_failure import user_facing_run_error
from packages.core.services.decision_recommendation import project_recommendation
from packages.core.ai.workflow_import import UnknownWorkflowFormat, import_workflow
from packages.core.ai.workflow_runner import (
    WorkflowRunner,
    finalize_workflow_terminal_effects_best_effort,
    mark_workflow_terminal_effects_pending,
)
from packages.core.database import get_db
from packages.core.models.document import Document
from packages.core.models.permission import Capability, ResourceType, Visibility
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowDefinition, WorkflowProject, WorkflowTemplateInstallation
from packages.core.models.workspace import Workspace
from packages.core.revisions import StaleRevisionError
from packages.core.services import workflow_service as svc
from packages.core.services.workflow_service import ScheduledWorkflowInputsError
from packages.core.services.workspace_workflow_router import WorkflowRunInputError
from packages.core.services.workflow_run_trace import step_waits_in_background, visible_workflow_tags, workflow_result_outcome
from packages.core.services.document_access import user_can_read_document
from packages.core.services.resource_access import (
    ResourceDescriptor,
    is_read_capability,
    user_can_access_resource,
)
from packages.core.services.sensitive_data import sanitize_sensitive_payload
from packages.core.services.workflow_run_trace import summarize_trace_text
from packages.core.services.workspace_access import (
    is_entity_admin_role,
    resolve_workspace_read_access,
    user_can_control_workspace_run,
    user_can_read_workspace_id,
    user_readable_workspace_ids,
    user_can_write_workspace_id,
    user_writable_workspace_ids,
)
from apps.api.collaborator_boundary import collaborator_scope
from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from packages.core.services.collaborator_scope import (
    require_collaborator_references,
    require_collaborator_workflow_steps,
    workflow_workspace_ids_batch,
)
from apps.api.routers.workspace_chat import WORKSPACE_WORKFLOW_RUN_ACTION_KINDS
from packages.core.constants.pending_actions import PendingActionKind

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


async def _persisted_workspace_id(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> str | None:
    """Return a persisted Workspace id, including soft-deleted rows.

    Bindings predate a foreign key on ``workspace_id``, so labels that never
    identified a Workspace remain entity-level compatibility data. Once an id
    identifies a Workspace, however, deletion must fail closed rather than
    reclassifying its binding as entity-level.
    """
    normalized = str(workspace_id or "").strip()
    if not normalized:
        return None
    found = (await db.execute(
        select(Workspace.id).where(Workspace.id == normalized).limit(1)
    )).scalar_one_or_none()
    return str(found) if found else None


async def _require_binding_workspace_read(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> bool:
    """Authorize persisted Workspace bindings; legacy labels stay valid."""
    real_id = await _persisted_workspace_id(db, user, workspace_id)
    if real_id:
        if not await user_can_read_workspace_id(
            db,
            workspace_id=real_id,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        ):
            raise HTTPException(404, "Workspace not found")
        return True
    return False


async def _require_binding_workspace_write(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> bool:
    real_id = await _persisted_workspace_id(db, user, workspace_id)
    if real_id:
        if not await user_can_write_workspace_id(
            db,
            workspace_id=real_id,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        ):
            raise HTTPException(403, "You do not have write access to this workspace")
        return True
    return False


async def _require_binding_write(
    db: AsyncSession,
    user: User,
    *,
    workflow_id: str,
    workspace_id: str | None,
) -> bool:
    """Authorize a binding mutation through its Workspace or definition."""
    workspace_scoped = await _require_binding_workspace_write(
        db,
        user,
        workspace_id,
    )
    if not workspace_scoped:
        await _require_workflow(db, user, workflow_id, Capability.EDIT)
    return workspace_scoped


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
    validate_steps: bool = False
    # The ``version`` this edit started from; the save is refused with 409 if
    # the workflow has been saved since. Omitted, the save applies as before.
    expected_version: int | None = Field(None, ge=1)


class WorkflowTemplateInstallRequest(BaseModel):
    workspace_id: str | None = None


class WorkflowResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    created_by: str | None = None
    name: str
    description: str | None = None
    icon: str = "flow"
    trigger_type: str
    trigger_config: dict = {}
    steps: list[dict] = []
    # Only on summaries, which leave `steps` empty.
    step_count: int | None = None
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
    # /{workflow_id}/run: true = run inline to completion, false = create only
    # (manual /step). /bindings/{id}/run always queues the run on a worker.
    execute: bool = True
    # /{workflow_id}/run: hand the run to a worker and return at once.
    queue: bool = False


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
    resume_action: dict | None = None
    processed_count: int | None = None
    total_count: int | None = None
    artifact_count: int | None = None
    history_blocker: object | None = None
    publication_receipts: list[dict] = []


class ResumeRequest(BaseModel):
    variables: dict | None = None
    expected_step_id: str | None = None
    execute: bool = Field(
        default=True,
        description="Deprecated compatibility field. Resumes are always queued.",
    )


class RuntimeApprovalDecisionRequest(BaseModel):
    choice: Literal["approve", "reject"]
    review: dict | None = None


class WorkflowEffectReviewRequest(BaseModel):
    step_id: str
    effect_fingerprint: str = Field(min_length=64, max_length=64)
    decision: Literal["not_applied", "applied"]
    confirmed: Literal[True]
    note: str = Field(min_length=20, max_length=2000)
    result: dict | None = None
    project_revisions: dict[str, int] = Field(default_factory=dict)


class SuppliedStepOutputRequest(BaseModel):
    """Work finished outside the run that stands in for one step's result."""
    step_id: str = Field(min_length=1, max_length=200)
    # Must match the step's declared output_schema and name every document.
    output: dict
    document_ids: list[str] = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=2000)


class RetryRunRequest(BaseModel):
    from_step_id: str | None = None
    variables: dict | None = None
    execute: bool = True
    effect_review: WorkflowEffectReviewRequest | None = None
    # A person retrying a run they fixed wants the fix: resume at the
    # checkpoint on the Workflow as it is now. "original" replays the
    # definition the failed attempt ran.
    definition: Literal["current", "original"] = "current"
    # Accept a file made elsewhere (in chat, by hand) as this step's output and
    # continue from there: the steps after it, its QA included, still run.
    supplied_output: SuppliedStepOutputRequest | None = None


class ReviseRunRequest(BaseModel):
    """A change the person wants in a finished run's result (see production.revise)."""
    notes: str = Field(default="", max_length=2000)
    # The version (MediaJob id) picked for each unit, by unit id.
    unit_versions: dict[str, str] = Field(default_factory=dict, max_length=200)


class WorkflowProjectCandidateResponse(BaseModel):
    id: str
    project_type: str
    current_stage: str
    title: str
    youtube_video_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class WorkflowRecordChoiceResponse(BaseModel):
    value: str
    label: str
    description: str | None = None


# ── Helpers ──

def _wf_to_dict(wf, *, workspace_name: str | None = None) -> dict:
    return WorkflowResponse(
        id=wf.id,
        entity_id=wf.entity_id,
        workspace_id=wf.workspace_id,
        workspace_name=workspace_name,
        created_by=wf.created_by,
        name=wf.name,
        description=wf.description,
        icon=wf.icon or "flow",
        trigger_type=wf.trigger_type,
        trigger_config=wf.trigger_config or {},
        steps=wf.steps or [],
        variables=wf.variables or {},
        category=wf.category,
        tags=visible_workflow_tags(wf.tags),
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
        "error": summary_error if summary else user_facing_run_error(run),
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
        from packages.core.services.workflow_publication_receipts import (
            publication_receipts_from_step_results,
        )

        response_data.update({
            "variables": variables,
            "step_results": run.step_results or {},
            "trigger_data": trigger_data,
            "workflow_definition_fingerprint": trigger_data.get(
                "_workflow_definition_fingerprint"
            ),
            "business_outcome": state.get("business_outcome") or workflow_result_outcome(variables) or None,
            "publication_receipts": publication_receipts_from_step_results(
                run.step_results
            ),
        })
    if can_control is not None:
        response_data["capabilities"] = {"can_control": can_control}
    if can_control and include_detail and not summary:
        from packages.core.services.workflow_run_control import run_resume_action

        from packages.core.i18n import get_locale

        response_data["resume_action"] = project_recommendation(run_resume_action(run, locale=get_locale()))
    if summary:
        response_data.update(compact_state or {})
    if include_detail:
        response_data.update({
            "definition_snapshot": getattr(run, "definition_snapshot", None) or {},
            "execution_trace": getattr(run, "execution_trace", None) or [],
        })
    response_data = _in_viewer_language(run, response_data)
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


def _in_viewer_language(run, payload: dict) -> dict:
    """Name the Flow and its steps in the requesting person's language."""
    from packages.core.i18n import get_locale
    from packages.core.services.viewer_language import (
        RUN_DISPLAY_I18N_KEY,
        localize_run_names,
        run_names_for,
    )

    translations = getattr(run, "summary_display_i18n", None)
    if not isinstance(translations, dict):
        trigger_data = run.__dict__.get("trigger_data")
        translations = trigger_data.get(RUN_DISPLAY_I18N_KEY) if isinstance(trigger_data, dict) else None
    return localize_run_names(payload, run_names_for(translations, get_locale()))


def _persisted_workflow_history_state(run) -> dict:
    summary = getattr(run, "summary_history_state", None)
    if not isinstance(summary, dict):
        trigger_data = run.__dict__.get("trigger_data")
        trigger_data = trigger_data if isinstance(trigger_data, dict) else {}
        summary = trigger_data.get("_workflow_history_summary")
    if not isinstance(summary, dict):
        return {}

    summary_version = summary.get("schema_version")
    progress_is_compatible = (
        isinstance(summary_version, int)
        and not isinstance(summary_version, bool)
        and summary_version >= 2
    )

    def count(key: str) -> int | None:
        value = summary.get(key)
        return max(0, int(value)) if isinstance(value, int) and not isinstance(value, bool) else None

    outcome = summary.get("business_outcome")
    workflow_steps = summary.get("workflow_steps")
    return {
        "business_outcome": outcome if isinstance(outcome, str) and outcome else None,
        "processed_count": count("processed_count") if progress_is_compatible else None,
        "total_count": count("total_count") if progress_is_compatible else None,
        "workflow_steps": (
            workflow_steps
            if progress_is_compatible and isinstance(workflow_steps, list)
            else None
        ),
        "artifact_count": count("artifact_count"),
        "history_blocker": summary.get("blocker"),
    }


def _run_business_outcome(run) -> str:
    variables = run.variables if isinstance(run.variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    return str(state.get("business_outcome") or workflow_result_outcome(variables)).strip().lower()


def _response_artifact_refs(response_data: dict) -> list[dict]:
    refs: list[dict] = []

    def append_refs(value) -> None:
        if isinstance(value, dict):
            refs.append(value)
        elif isinstance(value, list):
            refs.extend(ref for ref in value if isinstance(ref, dict))

    for entry in response_data.get("execution_trace") or []:
        if not isinstance(entry, dict):
            continue
        append_refs(entry.get("artifact_refs"))
    step_results = response_data.get("step_results")
    if isinstance(step_results, dict):
        for result in step_results.values():
            if not isinstance(result, dict):
                continue
            append_refs(result.get("artifact_refs"))
    return refs


async def _enrich_visible_artifact_names(
    db: AsyncSession,
    response_data: dict,
    *,
    run,
    user: User,
) -> dict:
    refs = _response_artifact_refs(response_data)
    document_ids = {
        str(ref.get("document_id") or "").strip()
        for ref in refs
        if str(ref.get("document_id") or "").strip()
    }
    if not document_ids:
        return response_data

    from sqlalchemy import select

    documents = list((await db.execute(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.id.in_(document_ids),
        )
    )).scalars().all())
    visible_names: dict[str, str] = {}
    for document in documents:
        if await user_can_read_document(
            db,
            document,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
            workspace_id=run.workspace_id,
        ):
            visible_names[document.id] = document.name
    for ref in refs:
        document_id = str(ref.get("document_id") or "").strip()
        if document_id in visible_names:
            ref["name"] = visible_names[document_id]
    return response_data


async def _attach_step_activity(response_data: dict, run) -> None:
    """Put what each step's long work reported (phases, counts, warnings) on its row."""

    steps = response_data.get("workflow_steps")
    if not isinstance(steps, list) or not steps:
        return
    from packages.core.services.runtime_activity import read_activity

    activity = await read_activity(str(run.id))
    if not activity:
        return
    for step in steps:
        if isinstance(step, dict) and (entry := activity.get(str(step.get("id") or ""))):
            step["activity"] = entry


def _compact_workflow_run_state(run, *, restartable: bool = False) -> dict:
    from packages.core.services.workflow_run_control import run_resume_action
    from packages.core.services.workflow_chat_projection import (
        _workflow_business_state,
        _workflow_error_payload,
        _workflow_retry_input_schema,
        _workflow_retry_values,
        default_retry_change,
        workflow_progress_steps,
        workflow_retry_from_step_id,
        workflow_step_display_name,
    )
    from packages.core.services.workflow_run_trace import (
        summarize_trace_value,
        workflow_execution_steps,
        workflow_run_stage_counts,
    )

    persisted_state = _persisted_workflow_history_state(run)
    workflow_steps = workflow_progress_steps(
        run,
        activity_status=str(run.status or ""),
        existing_steps=persisted_state.get("workflow_steps"),
    )
    # Stages done against the longest route through the Flow: the same figures the
    # history summary keeps. The displayed steps are only the path reached so far.
    progress = workflow_run_stage_counts(run, workflow_execution_steps(run))
    business_outcome, retry_state = _workflow_business_state(run)
    raw_error = _workflow_error_payload(run)
    error = summarize_trace_value(raw_error) if raw_error is not None else None
    current_step_result = (run.step_results or {}).get(run.current_step_id)
    current_step_result = (
        current_step_result if isinstance(current_step_result, dict) else {}
    )
    retry_from_step_id = workflow_retry_from_step_id(run, retry_state)
    from packages.core.services.workflow_run_trace import STALLED_PROBLEM, STALLED_RESTART_CHANGE, workflow_run_stalled

    stalled = workflow_run_stalled(run)
    # A stalled run is retried by starting over on the current Flow, and only once it has changed.
    retryable = restartable if stalled else (run.status in {"failed", "cancelled"} or (
        run.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    ))
    if stalled and restartable:
        retry_from_step_id = next((
            str(node.get("id")) for node in (run.definition_snapshot or {}).get("nodes") or []
            if isinstance(node, dict) and node.get("type") == "trigger"
        ), retry_from_step_id)
        retry_state = {**retry_state, "required_change": STALLED_RESTART_CHANGE, "observed_problem": STALLED_PROBLEM}
    from packages.core.i18n import get_locale

    native_action = run_resume_action(run, locale=get_locale())
    intervention = None
    if retryable and retry_from_step_id:
        editable_input_schema = {} if stalled else _workflow_retry_input_schema(run, retry_state)
        observed_problem = retry_state.get("observed_problem")
        intervention = {
            "kind": "workflow_retry",
            "workflow_run_id": run.id,
            "workflow_binding_id": run.binding_id,
            "business_outcome": business_outcome,
            "phase": retry_state.get("phase") or "execution",
            "step_id": retry_state.get("step_id") or run.current_step_id,
            "retry_from_step_id": retry_from_step_id,
            "retry_from_step_name": workflow_step_display_name(run, retry_from_step_id),
            "retry_segment_ids": summarize_trace_value(
                retry_state.get("segment_ids") or []
            ),
            "observed_problem": summarize_trace_value(
                error if observed_problem is None else observed_problem
            ),
            "required_change": summarize_trace_value(
                retry_state.get("required_change")
                or default_retry_change(error if observed_problem is None else observed_problem)
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
    elif run.status in {"failed", "cancelled"}:
        from packages.core.services.workflow_agent_recovery import workflow_effect_review

        review = workflow_effect_review(run)
        if review:
            intervention = {
                "kind": "workflow_effect_review",
                "workflow_run_id": run.id,
                "workflow_binding_id": run.binding_id,
                "options": ["review"],
                **review,
            }
    elif native_action and (
        native_action.get("kind") in {"workflow_input", "workflow_approval"}
        or native_action.get("connection_provider")
    ):
        # Native input and approval waits need the same App entry as their
        # chat-backed forms, and a connection wait its connect entry. The
        # dialog rechecks the frozen request on opening.
        intervention = summarize_trace_value(native_action)
        if native_action.get("connection_provider"):
            intervention = {
                **intervention,
                "workflow_binding_id": run.binding_id,
                "observed_problem": native_action.get("prompt"),
                "options": ["resume", "cancel"],
            }
    elif (
        run.status == "paused"
        and run.current_step_id
        # An automatic retry, a render or a timer continues on its own.
        and not step_waits_in_background(current_step_result)
        and (
            str(current_step_result.get("wait_type") or "").lower() == "event"
            or current_step_result.get("resume_strategy") == "reexecute"
        )
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
    elif run.status == "paused":
        from packages.core.services.workflow_run_control import is_manually_paused

        if is_manually_paused(run):
            intervention = {
                "kind": "workflow_resume",
                "workflow_run_id": run.id,
                "workflow_binding_id": run.binding_id,
                "step_id": run.current_step_id,
                "observed_problem": "Workflow paused by an operator.",
                "options": ["resume", "cancel"],
            }
    return {
        "workflow_steps": workflow_steps,
        **progress,
        "business_outcome": business_outcome,
        "error": error,
        "retry_from_step_id": retry_from_step_id or None,
        "intervention": project_recommendation(intervention),
    }


async def _deepest_retryable_subworkflow_run(
    db: AsyncSession,
    run,
):
    """Resolve the latest attempt that controls this visible Run family."""
    from packages.core.models.workflow import WorkflowRun

    latest_attempt = await svc.latest_workflow_run_family_attempt(db, run)
    origin = (
        run
        if latest_attempt.id != run.id and latest_attempt.status == "cancelled"
        else latest_attempt
    )
    cursor = origin
    seen_run_ids = {str(run.id), str(cursor.id)}
    depth = 0

    while cursor.status == "failed" and cursor.current_step_id:
        if cursor.lineage_status != "canonical":
            return origin
        current_step_id = str(cursor.current_step_id)
        current_result = (cursor.step_results or {}).get(current_step_id)
        if not isinstance(current_result, dict):
            return cursor
        child_run_id = str(current_result.get("subrun_id") or "").strip()
        if not child_run_id:
            return cursor
        if (
            current_result.get("status") not in {"failed", "cancelled"}
            or child_run_id in seen_run_ids
            or depth >= 5
        ):
            return origin

        child = (await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == child_run_id,
                WorkflowRun.entity_id == origin.entity_id,
            )
        )).scalar_one_or_none()
        child_trigger = (
            child.trigger_data
            if child is not None and isinstance(child.trigger_data, dict)
            else {}
        )
        if (
            child is None
            or child.status != "failed"
            or child.workspace_id != origin.workspace_id
            or child.lineage_status != "canonical"
            or str(child_trigger.get("parent_run_id") or "") != str(cursor.id)
            or str(child_trigger.get("parent_step_id") or "") != current_step_id
            or child_trigger.get("parent_foreach_item_key")
        ):
            return origin

        latest_child = await svc.latest_workflow_run_family_attempt(db, child)
        if latest_child.id != child.id and latest_child.status == "cancelled":
            latest_child = child
        if (
            latest_child.lineage_status != "canonical"
            or latest_child.workflow_id != child.workflow_id
            or latest_child.workspace_id != origin.workspace_id
            or str(latest_child.lineage_root_run_id or latest_child.id)
            != str(child.lineage_root_run_id or child.id)
            or str(latest_child.id) in seen_run_ids
        ):
            return origin

        seen_run_ids.update({child_run_id, str(latest_child.id)})
        cursor = latest_child
        depth += 1

    return cursor


async def _run_waiting_action(db: AsyncSession, run) -> dict | None:
    """The decision a paused run itself waits on: its chat card or native wait."""
    from sqlalchemy import select

    from packages.core.i18n import get_locale
    from packages.core.models.task import Conversation, Message
    from packages.core.services.workflow_run_control import run_resume_action

    action = await _message_backed_workflow_intervention(db, run, full=True)
    if action and action.get("runtime_approval_id"):
        return action
    if action and (
        (
            action.get("step_id") == run.current_step_id
            and (
                action.get("kind") == PendingActionKind.WORKFLOW_APPROVAL
                or (
                    action.get("kind") == PendingActionKind.WORKFLOW_INPUT
                    and isinstance(action.get("inputs"), list)
                )
            )
        )
        # A run launched without its required inputs waits on the
        # starter form before any step runs; answer that same card here.
        or (
            action.get("kind") == PendingActionKind.WORKFLOW_STARTER_INPUT
            and isinstance(action.get("inputs"), list)
            and not run.step_results
        )
    ):
        result = (run.step_results or {}).get(run.current_step_id) or {}
        return {
            **action,
            "message_workspace_id": await db.scalar(
                select(Conversation.workspace_id)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(Message.id == action["message_id"])
            ),
            "prompt": result.get("output"),
            "review": result.get("review"),
            "review_title": result.get("review_title"),
        }
    return project_recommendation(run_resume_action(run, locale=get_locale()))


async def _descendant_waiting_action(db: AsyncSession, run) -> dict | None:
    """The first decision a paused descendant of ``run`` waits on, oldest first.

    Its ``workflow_run_id`` names that descendant, which is the run answered.
    """
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_control import active_subworkflow_descendant_ids

    descendant_ids = await active_subworkflow_descendant_ids(db, run)
    if not descendant_ids:
        return None
    waiting = (await db.execute(
        select(WorkflowRun).where(
            WorkflowRun.entity_id == run.entity_id,
            WorkflowRun.id.in_(descendant_ids),
            WorkflowRun.status == "paused",
        ).order_by(WorkflowRun.created_at, WorkflowRun.id)
    )).scalars().all()
    for descendant in waiting:
        action = await _run_waiting_action(db, descendant)
        if action:
            return {**action, "workflow_run_id": descendant.id}
    return None


async def _native_workflow_approval_intervention(db: AsyncSession, run) -> dict | None:
    """Project the existing native card without exposing its private continuation."""
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message

    result = (run.step_results or {}).get(run.current_step_id)
    if run.status != "paused" or not isinstance(result, dict):
        return None
    marker = result.get("runtime_approval")
    if (
        result.get("code") != "runtime_approval_required"
        or not isinstance(marker, dict)
        or marker.get("workflow_run_id") != run.id
        or marker.get("step_id") != run.current_step_id
    ):
        return None
    request = await db.get(HitlRequest, marker.get("request_id"))
    if (
        request is None
        or request.entity_id != run.entity_id
        or request.workspace_id != run.workspace_id
        or request.status != ApprovalStatus.PENDING
    ):
        return None
    conversation = await db.get(Conversation, request.origin_conversation_id)
    if (
        conversation is None
        or conversation.entity_id != run.entity_id
        or not (
            run.workspace_id and conversation.workspace_id == run.workspace_id
            or conversation.workspace_id is None and conversation.user_id == run.started_by
        )
    ):
        return None
    message = (await db.execute(select(Message).where(
        Message.conversation_id == conversation.id,
        Message.response_surface_event_id == f"workflow-runtime-hitl:{request.id}",
    ).limit(1))).scalar_one_or_none()
    cards = (message.meta or {}).get("hitl_requests", []) if message else []
    card = next((item for item in cards if isinstance(item, dict)
                 and item.get("id") == request.id and not item.get("resolved")), None)
    if card is None:
        return None
    review = sanitize_sensitive_payload({
        key: card[key] for key in ("tool", "action", "paths", "args_preview", "content", "review")
        if key in card and card[key] is not None
    })
    tool = str(review.get("tool") or "") if isinstance(review, dict) else ""
    tiktok_publish_review = tool in {"mcp__tiktok__post_video", "mcp__tiktok__post_photo"}
    public_review = review.get("review") if tiktok_publish_review else review
    return {
        "kind": PendingActionKind.WORKFLOW_APPROVAL.value,
        "action_id": request.id,
        "runtime_approval_id": request.id,
        "workflow_run_id": run.id,
        "step_id": run.current_step_id,
        "message_id": message.id,
        "message_workspace_id": conversation.workspace_id,
        "source": "workflow_runtime_approval",
        "title": _runtime_approval_title(card),
        "prompt": sanitize_sensitive_payload(card.get("prompt") or result.get("output")),
        "options": ["approve", "reject"],
        "tool": tool or None,
        "review_read_only": not tiktok_publish_review,
        "review": public_review,
    }


def _runtime_approval_title(card: dict) -> str:
    """Name the requested action and its files, e.g. ``Edit workspace file · CANVAS.md``."""
    action = str(card.get("action") or "").strip()
    paths = [path for path in card.get("paths") or [] if isinstance(path, str) and path.strip()]
    names = ", ".join(PurePosixPath(path).name for path in paths[:2])
    if len(paths) > 2:
        names += f" +{len(paths) - 2}"
    if re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", action):
        # An action key such as workspace.file.modify reads as its verb.
        verb = action.rsplit(".", 1)[-1].replace("_", " ")
        return f"{verb[:1].upper()}{verb[1:]} {names}".strip()
    label = action[:1].upper() + action[1:] if action else "Approval required"
    return f"{label} · {names}" if names else label


async def _message_backed_workflow_intervention(
    db: AsyncSession,
    run,
    *,
    full: bool = False,
) -> dict | None:
    from sqlalchemy import and_, or_, select

    from packages.core.models.task import Conversation, Message
    from packages.core.services.workflow_run_trace import summarize_trace_value

    native_approval = await _native_workflow_approval_intervention(db, run)
    if native_approval is not None:
        return native_approval

    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    context = trigger_data.get("_workflow_chat_origin")
    if not isinstance(context, dict):
        context = trigger_data.get("_workspace_chat_entrypoint")
    if not isinstance(context, dict):
        return None
    conversation_id = str(context.get("conversation_id") or "").strip()
    if not conversation_id:
        return None

    personal_conversation = and_(
        Conversation.workspace_id.is_(None),
        Conversation.user_id == run.started_by,
    )
    conversation_scope = (
        or_(
            Conversation.workspace_id == run.workspace_id,
            personal_conversation,
        )
        if run.workspace_id
        else personal_conversation
    )
    conditions = (
        Conversation.id == conversation_id,
        Conversation.entity_id == run.entity_id,
        conversation_scope,
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
    sanitized = (dict(action) if full else summarize_trace_value(action)) if action else None
    if not isinstance(sanitized, dict):
        return None
    action_kind = str((action or {}).get("kind") or "").strip()
    if action_kind:
        sanitized["kind"] = action_kind
    sanitized["message_id"] = message.id
    sanitized["source"] = "workspace_chat"
    if sanitized.get("retry_from_step_id") and not sanitized.get("retry_from_step_name"):
        # Cards saved before the name was recorded.
        from packages.core.services.workflow_chat_projection import workflow_step_display_name

        sanitized["retry_from_step_name"] = workflow_step_display_name(run, str(sanitized["retry_from_step_id"]))
    return sanitized


async def _run_can_control(db: AsyncSession, run, user: User) -> bool:
    if not getattr(run, "workspace_id", None):
        if str(getattr(run, "started_by", "") or "") == str(user.id):
            return True
        resolved_role, _ = await resolve_workspace_read_access(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        )
        return is_entity_admin_role(resolved_role)
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
        entity_id=user.entity_id,
        workspace_ids=workspace_ids,
        user_id=user.id,
        role=user.role,
    )
    readable_workspace_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        workspace_ids=workspace_ids,
        user_id=user.id,
        role=user.role,
    )
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    entity_admin = is_entity_admin_role(resolved_role)
    return {
        run.id: bool(
            (
                str(getattr(run, "started_by", "") or "") == str(user.id)
                and (
                    not getattr(run, "workspace_id", None)
                    or str(run.workspace_id) in readable_workspace_ids
                )
            )
            or (
                not getattr(run, "workspace_id", None)
                and entity_admin
            )
            or (
                bool(getattr(run, "workspace_id", None))
                and str(run.workspace_id) in writable_workspace_ids
            )
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
    summary: bool = Query(False, description="Leave out steps and variables, for pickers and name lookups."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == user.entity_id,
            or_(
                WorkflowDefinition.workspace_id.is_(None),
                exists(select(Workspace.id).where(
                    Workspace.id == WorkflowDefinition.workspace_id,
                    Workspace.entity_id == user.entity_id,
                    Workspace.deleted_at.is_(None),
                )),
            ),
            ~exists(select(WorkflowTemplateInstallation.id).where(
                WorkflowTemplateInstallation.entity_id == user.entity_id,
                WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id,
                WorkflowTemplateInstallation.source_type == "workspace_blueprint",
                WorkflowTemplateInstallation.installation_metadata["internal"].as_boolean().is_(True),
            )),
        ).order_by(WorkflowDefinition.created_at.desc())
    )).scalars().all()
    collaborator_ids = collaborator_scope(user)
    if collaborator_ids is not None:
        # An external collaborator sees only Workflows of the Workspaces they were granted.
        workspaces = await workflow_workspace_ids_batch(db, [w.id for w in items], entity_id=user.entity_id)
        items = [w for w in items if collaborator_ids & workspaces.get(w.id, set())]
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
    workspace_ids = {w.workspace_id for w in visible if w.workspace_id}
    workspace_names = dict((await db.execute(
        select(Workspace.id, Workspace.name).where(Workspace.id.in_(workspace_ids))
    )).all()) if workspace_ids else {}
    rows = [_wf_to_dict(w, workspace_name=workspace_names.get(w.workspace_id)) for w in visible]
    if not summary:
        return rows
    return [{**row, "steps": [], "step_count": len(row["steps"] or []), "variables": {}} for row in rows]


@router.get(
    "/projects/candidates",
    response_model=list[WorkflowProjectCandidateResponse],
)
async def list_workflow_project_candidates(
    workspace_id: str = Query(..., min_length=1),
    project_type: str = Query(..., min_length=1, max_length=80),
    stage: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Projects a ``workflow_project_select`` picker offers; no ``stage`` offers every stage."""
    from packages.core.services.workflow_project_select import list_projects, project_title

    await _require_binding_workspace_read(db, user, workspace_id)
    stages = {str(value or "").strip() for value in stage or []}
    stages.discard("")
    if len(stages) > 20 or any(len(value) > 40 for value in stages):
        raise HTTPException(422, "Up to twenty valid project stages are allowed")

    projects = await list_projects(
        db, entity_id=user.entity_id, workspace_id=workspace_id,
        project_type=project_type, stages=sorted(stages), limit=limit,
    )
    candidates: list[WorkflowProjectCandidateResponse] = []
    for project in projects:
        state = project.state if isinstance(project.state, dict) else {}
        youtube = state.get("youtube")
        youtube_video_id = (
            str(youtube.get("video_id") or "").strip()
            if isinstance(youtube, dict)
            else ""
        )
        candidates.append(WorkflowProjectCandidateResponse(
            id=project.id,
            project_type=project.project_type,
            current_stage=project.current_stage,
            title=project_title(project),
            youtube_video_id=youtube_video_id[:128] or None,
            created_at=project.created_at,
            updated_at=project.updated_at,
        ))
    return candidates


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
    # A collaborator's Workflow names only its Workspace's resources.
    await require_collaborator_workflow_steps(
        db, user_id=user.id, entity_id=user.entity_id, workspace_id=body.workspace_id, steps=body.steps, user=user,
    )
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


@router.get("/templates")
async def list_workflow_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List verified platform Flow templates by stable catalogue id."""
    from packages.core.services.workflow_template_service import list_flow_templates

    return await list_flow_templates(db, user.entity_id)


@router.post("/templates/{template_id}/install", status_code=201)
async def install_workflow_template(
    template_id: str,
    body: WorkflowTemplateInstallRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Install a portable template as an entity-owned WorkflowDefinition.

    The response deliberately includes both ids: ``template.id`` remains the
    Marketplace identity, while ``workflow.id`` is what every runtime caller
    and optional Workspace binding executes.
    """
    workspace_id = body.workspace_id if body else None
    if workspace_id and not await user_can_write_workspace_id(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        role=getattr(user, "role", None),
    ):
        raise HTTPException(403, "Cannot attach a Flow to this Workspace")

    from packages.core.services.workflow_template_service import install_flow_template

    try:
        result = await install_flow_template(
            db,
            template_id=template_id,
            entity_id=user.entity_id,
            installed_by=user.id,
            workspace_id=workspace_id,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return result


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
                user_id=user.id,
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
    started_at = perf_counter()
    try:
        # A real, short-lived run on the same guarded path as a workflow run,
        # so tools the node calls pass their run checks and standalone tests
        # get typed input binding, retry/timeout behavior, and the resolved
        # input snapshot shown by the node editor.
        result = await svc.execute_node_test(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            step=body.step,
            variables=body.variables,
        )
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

        if body.create_binding and body.workspace_id:
            if not await _require_binding_workspace_write(
                db,
                user,
                body.workspace_id,
            ):
                raise HTTPException(404, "Workspace not found")

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
    except ValueError as exc:
        if str(exc) == "Workspace not found":
            raise HTTPException(404, "Workspace not found") from exc
        raise


# ── Workflow Bindings + Triggers ──

def _binding_to_dict(b, *, include_sensitive: bool = False) -> dict:
    trigger_config = dict(b.trigger_config or {})
    variables = dict(b.variables or {})
    config = dict(b.config or {})
    if not include_sensitive:
        trigger_config = sanitize_sensitive_payload(trigger_config)
        trigger_config.pop("webhook_token", None)
        variables = {}
        config = {}
    return {
        "id": b.id,
        "entity_id": b.entity_id,
        "workflow_id": b.workflow_id,
        "workspace_id": b.workspace_id,
        "business_line": b.business_line,
        "name": b.name,
        "trigger_type": b.trigger_type,
        "trigger_config": trigger_config,
        "variables": variables,
        "config": config,
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
    # A collaborator deploys only a Workflow that already belongs to their Workspace.
    await require_collaborator_references(
        db,
        user_id=user.id,
        entity_id=user.entity_id,
        workspace_id=body.workspace_id,
        refs={
            "workflow": {body.workflow_id},
            "workflow_binding": {(body.config or {}).get("workspace_workflow_binding_id")},
        },
        user=user,
    )
    workspace_scoped = await _require_binding_workspace_write(
        db, user, body.workspace_id
    )
    if body.workspace_id and not workspace_scoped:
        raise HTTPException(404, "Workspace not found")
    wf = await _require_workflow(
        db,
        user,
        body.workflow_id,
        Capability.VIEW if workspace_scoped else Capability.EDIT,
    )

    if body.trigger_type == "schedule":
        cfg = body.trigger_config or {}
        cron = cfg.get("cron") or cfg.get("schedule")
        if not cron:
            raise HTTPException(400, "A schedule trigger needs a 'cron' in trigger_config")
        try:
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
        except ScheduledWorkflowInputsError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            if str(exc) == "Workspace not found":
                raise HTTPException(404, "Workspace not found") from exc
            raise
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

    try:
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
    except ValueError as exc:
        if str(exc) == "Workspace not found":
            raise HTTPException(404, "Workspace not found") from exc
        if str(exc) == "Workflow belongs to another workspace":
            raise HTTPException(409, str(exc)) from exc
        raise
    await db.commit()
    return {
        "kind": "binding",
        **_binding_to_dict(binding, include_sensitive=True),
    }


@router.get("/bindings")
async def list_bindings(
    workspace_id: str | None = Query(None),
    business_line: str | None = Query(None),
    workflow_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    requested_real_workspace = await _persisted_workspace_id(db, user, workspace_id)
    if requested_real_workspace:
        await _require_binding_workspace_read(db, user, requested_real_workspace)
    items = await svc.list_bindings(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        business_line=business_line,
        workflow_id=workflow_id,
    )
    workspace_ids = {
        str(binding.workspace_id)
        for binding in items
        if binding.workspace_id
    }
    persisted_workspace_ids = set((await db.execute(
        select(Workspace.id).where(
            Workspace.id.in_(workspace_ids),
        )
    )).scalars()) if workspace_ids else set()
    readable_workspace_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        workspace_ids={str(value) for value in persisted_workspace_ids},
    )
    workflows = list((await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == user.entity_id,
            WorkflowDefinition.id.in_({binding.workflow_id for binding in items}),
        )
    )).scalars()) if items else []
    workflows_by_id = {workflow.id: workflow for workflow in workflows}
    access_cache: dict[tuple[str, str], bool] = {}

    async def _can_access_workflow(workflow_id: str, capability: str) -> bool:
        cache_key = (workflow_id, capability)
        if cache_key in access_cache:
            return access_cache[cache_key]
        workflow = workflows_by_id.get(workflow_id)
        allowed = bool(workflow) and await user_can_access_resource(
            db,
            descriptor=ResourceDescriptor.from_row(workflow, ResourceType.WORKFLOW),
            entity_id=user.entity_id,
            user_id=user.id,
            role=getattr(user, "role", None),
            capability=capability,
        )
        access_cache[cache_key] = allowed
        return allowed

    visible = []
    for binding in items:
        real_workspace = (
            str(binding.workspace_id)
            if binding.workspace_id
            and str(binding.workspace_id) in persisted_workspace_ids
            else None
        )
        if real_workspace:
            if real_workspace in readable_workspace_ids:
                visible.append(binding)
            continue
        if await _can_access_workflow(binding.workflow_id, Capability.VIEW):
            visible.append(binding)

    writable_workspace_ids = await user_writable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        workspace_ids={str(value) for value in persisted_workspace_ids},
    )
    response = []
    for binding in visible:
        real_workspace = (
            str(binding.workspace_id)
            if binding.workspace_id
            and str(binding.workspace_id) in persisted_workspace_ids
            else None
        )
        include_sensitive = (
            real_workspace in writable_workspace_ids
            if real_workspace
            else await _can_access_workflow(binding.workflow_id, Capability.EDIT)
        )
        response.append(_binding_to_dict(binding, include_sensitive=include_sensitive))
    return response


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
    await _require_binding_write(
        db,
        user,
        workflow_id=current.workflow_id,
        workspace_id=current.workspace_id,
    )
    if collaborator_scope(user) is not None:
        # A collaborator's binding stays in its Workspace and runs that Workspace's Workflows.
        if any(
            key in body.model_fields_set and getattr(body, key) != getattr(current, key)
            for key in ("workspace_id", "business_line")
        ):
            raise HTTPException(403, "External collaborators cannot move a binding out of its Workspace")
        await require_collaborator_references(
            db,
            user_id=user.id,
            entity_id=user.entity_id,
            workspace_id=current.workspace_id,
            refs={
                "workflow": {body.workflow_id},
                "workflow_binding": {(body.config or {}).get("workspace_workflow_binding_id")},
            },
            user=user,
        )
    target_workspace_id = (
        body.workspace_id
        if "workspace_id" in body.model_fields_set
        else current.workspace_id
    )
    target_workspace_scoped = await _require_binding_workspace_write(
        db, user, target_workspace_id
    )
    if target_workspace_id and not target_workspace_scoped:
        raise HTTPException(404, "Workspace not found")
    await _require_workflow(
        db,
        user,
        body.workflow_id or current.workflow_id,
        Capability.VIEW if target_workspace_scoped else Capability.EDIT,
    )
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
    try:
        binding = await svc.update_binding(
            db,
            binding_id,
            user.entity_id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        if str(exc) == "Workspace not found":
            raise HTTPException(404, "Workspace not found") from exc
        if str(exc) == "Workflow belongs to another workspace":
            raise HTTPException(409, str(exc)) from exc
        raise
    if not binding:
        raise HTTPException(404, "Binding not found")
    await db.commit()
    return _binding_to_dict(binding, include_sensitive=True)


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
    workspace_scoped = await _require_binding_workspace_write(
        db, user, binding.workspace_id
    )
    if not workspace_scoped:
        await _require_workflow(db, user, binding.workflow_id, Capability.VIEW)
    from packages.core.services.workspace_flow_launcher import enqueue_binding_run, start_binding_run

    try:
        run, launched = await start_binding_run(
            db,
            binding=binding,
            user=user,
            variables=body.variables if body else None,
            trigger_data=body.trigger_data if body and body.trigger_data else None,
            workspace_scoped=workspace_scoped,
        )
    except WorkflowRunInputError as exc:
        raise HTTPException(422, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()

    # Execution always goes to the worker queue, whatever ``execute`` says:
    # a node can run for many minutes, and a request handler can neither
    # outlive its process nor be resumed by the claim-loss recovery sweep.
    # The response is the created run; callers poll /runs/{id} for progress.
    await enqueue_binding_run(db, run, launched)
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
    await _require_binding_write(
        db,
        user,
        workflow_id=binding.workflow_id,
        workspace_id=binding.workspace_id,
    )
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
    workspace_scoped = await _require_binding_workspace_write(
        db, user, body.workspace_id
    )
    if not workspace_scoped and not is_entity_admin_role(user.role):
        raise HTTPException(403, "Entity-level workflow triggers require an administrator")
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
    workflow_id: str | None = Query(None),
    include_unbound: bool = Query(False),
    app_action: str | None = Query(None, max_length=200),
    include_unlabelled: bool = Query(False),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List workflow runs for Workspace and binding-level history surfaces.

    ``app_action`` narrows to runs started from that App entry;
    ``include_unlabelled`` also keeps runs no entry claims (Chat, older runs).
    """
    if include_unbound:
        if not workspace_id or not binding_id or not workflow_id:
            raise HTTPException(422, "Unbound history requires a workspace, binding and workflow")
        binding = await svc.get_binding(db, binding_id, user.entity_id)
        if (
            binding is None
            or binding.workspace_id != workspace_id
            or binding.workflow_id != workflow_id
        ):
            raise HTTPException(422, "Binding does not match the requested Workspace workflow")
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
        workflow_id=workflow_id,
        include_unbound=include_unbound,
        app_action=app_action,
        include_unlabelled=include_unlabelled,
        status=status,
        limit=limit,
        readable_workspace_ids=readable_workspace_ids,
        summary=True,
        top_level_only=True,
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
    # A newer run has made this run's project since: going on would write it
    # back over that version, so the run is closed, not offered again.
    superseded = await svc.workflow_run_superseded_project(db, run) is not None
    if not detail:
        from packages.core.services.workflow_run_trace import workflow_run_stalled

        from packages.core.services.workflow_chat_projection import (
            workflow_chat_context,
        )

        trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
        history_summary = trigger_data.get("_workflow_history_summary")
        if (
            isinstance(history_summary, dict)
            and not (
                isinstance(history_summary.get("schema_version"), int)
                and not isinstance(history_summary.get("schema_version"), bool)
                and history_summary["schema_version"] >= 2
            )
        ):
            from sqlalchemy.orm.attributes import set_committed_value

            legacy_trace = await db.scalar(
                select(WorkflowRun.execution_trace).where(WorkflowRun.id == run.id)
            )
            set_committed_value(run, "execution_trace", legacy_trace or [])
        compact_state = _compact_workflow_run_state(
            run, restartable=await svc.stalled_run_restart(db, run) is not None,
        )
        intervention_run = run
        nested_retry_run = await _deepest_retryable_subworkflow_run(db, run)
        if nested_retry_run.id != run.id:
            nested_state = _compact_workflow_run_state(nested_retry_run)
            nested_status = str(nested_retry_run.status or "")
            # The root projection can be newer than an unchanged nested child.
            # Never move the compact response clock backwards to that child.
            compact_updated_at = max(
                (
                    updated_at
                    for updated_at in (
                        run.__dict__.get("updated_at"),
                        nested_retry_run.__dict__.get("updated_at"),
                    )
                    if updated_at is not None
                ),
                default=None,
            )
            intervention_run = nested_retry_run
            same_run_family = (
                nested_retry_run.workflow_id == run.workflow_id
                and str(nested_retry_run.lineage_root_run_id or nested_retry_run.id)
                == str(run.lineage_root_run_id or run.id)
            )
            if same_run_family:
                compact_state.update(nested_state)
                compact_state.update({
                    "status": nested_status,
                    "current_step_id": nested_retry_run.current_step_id,
                    "completed_at": nested_retry_run.completed_at,
                    "updated_at": compact_updated_at,
                })
            elif nested_status in {"pending", "running", "paused", "completed"}:
                visible_status = "running" if nested_status == "completed" else nested_status
                compact_state.update({
                    "status": visible_status,
                    "completed_at": None,
                    "updated_at": compact_updated_at,
                    "business_outcome": "in_progress",
                    "error": None,
                    "intervention": nested_state.get("intervention"),
                })
                workflow_steps = compact_state.get("workflow_steps")
                if isinstance(workflow_steps, list):
                    compact_state["workflow_steps"] = [
                        {
                            **step,
                            "status": "running",
                            **({"error": None} if "error" in step else {}),
                        }
                        if isinstance(step, dict)
                        and str(step.get("id") or "") == str(run.current_step_id or "")
                        else step
                        for step in workflow_steps
                    ]
            elif nested_status == "failed":
                compact_state.update({
                    "updated_at": compact_updated_at,
                    "error": nested_state.get("error"),
                    "retry_from_step_id": nested_state.get("retry_from_step_id"),
                    "intervention": nested_state.get("intervention"),
                })
            nested_intervention = nested_state.get("intervention")
            if (
                isinstance(nested_intervention, dict)
                and nested_intervention.get("kind")
                == PendingActionKind.WORKFLOW_RETRY
            ):
                compact_state["intervention"] = {
                    **nested_intervention,
                    "workflow_run_id": run.id,
                    "workflow_binding_id": run.binding_id,
                }
        elif run.status == "paused" and run.current_step_id:
            current_result = (run.step_results or {}).get(run.current_step_id)
            child_run_id = (
                str(current_result.get("subrun_id") or "").strip()
                if isinstance(current_result, dict)
                else ""
            )
            if child_run_id:
                child = (await db.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.id == child_run_id,
                        WorkflowRun.entity_id == run.entity_id,
                    )
                )).scalar_one_or_none()
                child_trigger = (
                    child.trigger_data
                    if child is not None and isinstance(child.trigger_data, dict)
                    else {}
                )
                if (
                    child is not None
                    and child.status == "paused"
                    and child.workspace_id == run.workspace_id
                    and str(child_trigger.get("parent_run_id") or "") == str(run.id)
                    and str(child_trigger.get("parent_step_id") or "")
                    == str(run.current_step_id)
                    and await _run_can_control(db, child, user)
                ):
                    child_state = _compact_workflow_run_state(child)
                    child_intervention = child_state.get("intervention")
                    # A parent paused on a sub-flow owns no decision: it offers the
                    # one its child waits on (a resume, a review or a form).
                    if (
                        isinstance(child_intervention, dict)
                        and child_intervention.get("kind") in {
                            "workflow_resume",
                            PendingActionKind.WORKFLOW_APPROVAL,
                            PendingActionKind.WORKFLOW_INPUT,
                        }
                    ):
                        compact_state["intervention"] = child_intervention
                        compact_state["error"] = child_state.get("error")
                        intervention_run = child
        compact_intervention = compact_state.get("intervention")
        message_intervention = await _message_backed_workflow_intervention(
            db,
            intervention_run,
        )
        retry_message_is_unavailable = (
            isinstance(compact_intervention, dict)
            and compact_intervention.get("kind") == PendingActionKind.WORKFLOW_RETRY
            and not (
                nested_retry_run.id != run.id
                and nested_retry_run.workflow_id != run.workflow_id
                and nested_retry_run.status == "failed"
            )
            and workflow_chat_context(intervention_run) is not None
            and message_intervention is None
            # Starting a stalled run over does not resume its chat card's frozen state; the card
            # was drawn when it failed, before the Flow changed and made the restart possible.
            and not workflow_run_stalled(intervention_run)
        )
        if retry_message_is_unavailable:
            compact_state["intervention"] = None
        if message_intervention is not None:
            if (
                message_intervention.get("truncated") is True
                and isinstance(compact_intervention, dict)
                and message_intervention.get("kind")
                == compact_intervention.get("kind")
            ):
                compact_state["intervention"] = {
                    **compact_intervention,
                    "message_id": message_intervention["message_id"],
                    "source": message_intervention["source"],
                }
            elif (
                isinstance(compact_intervention, dict)
                and message_intervention.get("kind") == PendingActionKind.WORKFLOW_RETRY
                and compact_intervention.get("kind") == PendingActionKind.WORKFLOW_RETRY
            ):
                # The card keeps its message; the target is resolved from the run now,
                # not the one frozen when the card was drawn.
                compact_state["intervention"] = {
                    **message_intervention,
                    **{
                        key: compact_intervention[key]
                        for key in ("step_id", "retry_from_step_id", "retry_from_step_name", "retry_segment_ids")
                        if key in compact_intervention
                    },
                }
            else:
                compact_state["intervention"] = message_intervention
        compact_intervention = compact_state.get("intervention")
        if (
            nested_retry_run.id != run.id
            and isinstance(compact_intervention, dict)
            and compact_intervention.get("kind") == PendingActionKind.WORKFLOW_RETRY
        ):
            projected_intervention = {
                **compact_intervention,
                "workflow_run_id": run.id,
                "workflow_binding_id": run.binding_id,
            }
            projected_intervention.pop("message_id", None)
            compact_state["intervention"] = projected_intervention
        if superseded and isinstance(compact_state.get("intervention"), dict) and compact_state["intervention"].get("kind") in {
            PendingActionKind.WORKFLOW_RETRY, "workflow_effect_review",
        }:
            compact_state["intervention"] = None
        review_action = compact_state.get("intervention")
        if isinstance(review_action, dict) and review_action.get("kind") == "workflow_effect_review":
            from packages.core.services.workflow_agent_recovery import (
                workflow_effect_review_blocker, workflow_effect_result_schema, workflow_released_project_reviews,
            )

            blocker = await workflow_effect_review_blocker(db, intervention_run)
            options = [] if blocker else ["review"]
            result_schema = workflow_effect_result_schema(intervention_run)
            if result_schema and not await workflow_effect_review_blocker(db, intervention_run, applied=True):
                options.append("review_applied")
            compact_state["intervention"] = {
                **review_action, "options": options,
                **({"blocked_reason": blocker} if blocker else {}),
                **({"result_schema": result_schema} if "review_applied" in options else {}),
                **({"project_revisions": {pid: item["revision"] for pid, item in
                    (await workflow_released_project_reviews(db, intervention_run)).items()}}
                   if "review_applied" in options else {}),
            }
        retry_action = compact_state.get("intervention")
        if isinstance(retry_action, dict) and retry_action.get("kind") == PendingActionKind.WORKFLOW_RETRY:
            # A step that ended because its Agent's integration is not connected
            # is fixed by connecting it, then continuing from that step.
            from packages.core.services.workspace_connection_status import workflow_step_connection_need

            retry_step_id = str(retry_action.get("step_id") or retry_action.get("retry_from_step_id") or "")
            need = await workflow_step_connection_need(
                db, run=intervention_run, step_id=retry_step_id, user_id=user.id,
            )
            if need:
                stored = (intervention_run.step_results or {}).get(retry_step_id)
                compact_state["intervention"] = {
                    **retry_action,
                    "connection_provider": need["provider"],
                    "connection_label": need["label"],
                    # A step that stopped to ask a person keeps its own words.
                    **({} if isinstance(stored, dict) and stored.get("needs_person") else {
                        "observed_problem": f"{need['label']} is not connected, so this step could not finish.",
                        "required_change": f"Connect {need['label']}, then continue from this step.",
                    }),
                }
    response_data = _run_to_dict(
        run,
        include_detail=detail,
        summary=not detail,
        can_control=await _run_can_control(db, run, user),
        compact_state=compact_state,
    )
    await _attach_step_activity(response_data, run)
    if superseded:
        response_data["superseded"] = True
        if isinstance(response_data.get("intervention"), dict) and response_data["intervention"].get("kind") in {
            PendingActionKind.WORKFLOW_RETRY, "workflow_effect_review",
        }:
            response_data["intervention"] = None
    if detail:
        if response_data.get("capabilities", {}).get("can_control") and run.status == "paused":
            # A run paused on a nested Flow owns no decision: the person
            # answers the one its child (or a deeper descendant) waits on.
            response_data["resume_action"] = (
                await _run_waiting_action(db, run)
                or await _descendant_waiting_action(db, run)
            )
        from packages.core.services.workflow_review_files import attach_review_document_ids

        # Reviews recorded before files were linked still open their Documents.
        step_results = response_data.get("step_results")
        await attach_review_document_ids(db, entity_id=user.entity_id, reviews=[
            *((result or {}).get("review") for result in (step_results or {}).values() if isinstance(result, dict)),
            (response_data.get("resume_action") or {}).get("review"),
        ])
        await _enrich_visible_artifact_names(
            db,
            response_data,
            run=run,
            user=user,
        )
    # A Workflow that declares its production shows its stages and units.
    from sqlalchemy import select

    from packages.core.i18n import get_locale
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.workflow_production import workflow_run_production

    workflow_steps = await db.scalar(select(WorkflowDefinition.steps).where(
        WorkflowDefinition.id == run.workflow_id, WorkflowDefinition.entity_id == run.entity_id,
    ))
    production = workflow_run_production(workflow_steps, run.variables, locale=get_locale())
    if production is not None:
        # Units the run family rendered locally (no media job): finished like a generated clip.
        from packages.core.models.workflow import WorkflowRun
        from packages.core.services.workflow_production import clip_unit_id, workflow_run_local_clips

        root = run.lineage_root_run_id or run.id
        family_ids = list((await db.scalars(select(WorkflowRun.id).where(
            WorkflowRun.entity_id == run.entity_id,
            WorkflowRun.workflow_id == run.workflow_id,
            or_(WorkflowRun.id == root, WorkflowRun.lineage_root_run_id == root),
        ).limit(svc.RUN_FAMILY_MAX_RUNS))).all())
        unit_ids = [unit["id"] for unit in production["units"]]
        production["units_made_locally"] = [
            {"unit_id": unit_id, "document_id": clip["document_id"], "name": clip["output_name"]}
            for clip in await workflow_run_local_clips(
                db, entity_id=run.entity_id, run_ids=[*family_ids, run.id],
            )
            if clip["kind"] == production["media"] and (unit_id := clip_unit_id(clip, unit_ids))
        ]
        response_data["production"] = production
    return response_data


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
    latest_attempt = await svc.latest_workflow_run_family_attempt(
        db,
        run,
        lock_root=True,
    )
    if latest_attempt.id != run.id:
        await _require_run_control(db, latest_attempt, user)
        run = latest_attempt
    business_outcome = _run_business_outcome(run)
    actionable_completed = (
        run.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    )
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    if run.status == "cancelled":
        mark_workflow_terminal_effects_pending(run)
        await project_workflow_run_status(db, run=run)
        await db.commit()
        response = _run_to_dict(run, can_control=True)
        await finalize_workflow_terminal_effects_best_effort(
            run,
            db,
            context="API cancellation replay",
        )
        return response
    if run.status == "completed" and not actionable_completed:
        raise HTTPException(400, "Run already completed")
    from packages.core.constants.task import CancelSource
    from packages.core.services.workflow_run_control import (
        active_subworkflow_descendant_ids,
        cancel_active_subworkflow_descendants,
        cancel_run as cancel_workflow_run,
    )

    descendant_run_ids = await active_subworkflow_descendant_ids(db, run)
    nested_recovery = await _deepest_retryable_subworkflow_run(db, run)
    if (
        nested_recovery.id != run.id
        and nested_recovery.status in {"pending", "running", "paused"}
    ):
        await _require_run_control(db, nested_recovery, user)
        descendant_run_ids = list(dict.fromkeys([
            *descendant_run_ids,
            nested_recovery.id,
            *await active_subworkflow_descendant_ids(db, nested_recovery),
        ]))
    try:
        cancel_workflow_run(
            run,
            actor_id=user.id,
            source=CancelSource.USER,
            allow_problem_terminal=(run.status == "failed" or actionable_completed),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cancelled_children = await cancel_active_subworkflow_descendants(
        db,
        run,
        actor_id=user.id,
        descendant_run_ids=descendant_run_ids,
    )
    from packages.core.services.workflow_run_execution_claim import (
        revoke_workflow_run_execution_claims,
    )

    await revoke_workflow_run_execution_claims(
        db,
        [run.id, *descendant_run_ids],
    )
    mark_workflow_terminal_effects_pending(run)
    for child in cancelled_children:
        mark_workflow_terminal_effects_pending(child)
    await project_workflow_run_status(db, run=run)
    await db.commit()
    response = _run_to_dict(run, can_control=True)
    await finalize_workflow_terminal_effects_best_effort(
        run,
        db,
        context="API cancellation",
    )
    return response


@router.post("/runs/{run_id}/pause")
async def pause_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    from packages.core.services.workflow_run_control import pause_run as pause_workflow_run
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    try:
        pause_workflow_run(run, actor_id=user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await project_workflow_run_status(db, run=run)
    await db.commit()
    return _run_to_dict(run, can_control=True)


@router.post("/runs/{run_id}/approvals/{request_id}/resolve")
async def resolve_run_runtime_approval(
    run_id: str,
    request_id: str,
    body: RuntimeApprovalDecisionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.workflow_runtime_approvals import resolve_workflow_approval

    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    try:
        resolution = await resolve_workflow_approval(
            db, run=run, request_id=request_id, user_id=user.id, choice=body.choice,
            submitted_review=body.review,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    metadata = resolution.runtime_metadata if resolution else None
    # Commit canonical expiry decisions as well as grants; only grants enqueue
    # the saved continuation, after this transaction is durable.
    await db.commit()
    if not metadata or metadata.get("approval_outcome") not in {"approved", "rejected"}:
        raise HTTPException(409, resolution.message if resolution else "Approval is unavailable")
    await db.refresh(run)
    return _run_to_dict(run, can_control=True)


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
    # row lock in its own session. The durable continuation is then handed to a
    # worker; an HTTP request must not execute an arbitrary workflow inline.
    await db.rollback()
    from packages.core.ai.workflow_runner import WorkflowRunner

    outcome = await WorkflowRunner.resume(
        run_id,
        body.variables if body else None,
        entity_id=entity_id,
        resumed_by=resumed_by,
        expected_step_id=body.expected_step_id if body else None,
        execute=False,
    )
    if outcome == "not_found":
        raise HTTPException(404, "Run not found")
    if outcome == "not_paused":
        raise HTTPException(409, "Run was already resumed")
    if outcome == "invalid_approval":
        raise HTTPException(400, "Approval resume requires a valid decision from the run owner")
    if outcome == "invalid_input":
        raise HTTPException(422, "The submitted workflow input no longer satisfies this wait.")
    if outcome == "wait_changed":
        raise HTTPException(409, "The workflow has moved to a different step. Review the current request.")
    if outcome == "definition_changed":
        raise HTTPException(409, "Workflow definition changed since this run started")
    if outcome == "continuation_required":
        raise HTTPException(
            409,
            "This workflow pause is controlled by an active child continuation",
        )
    await db.refresh(run)
    return _run_to_dict(run)


async def _restart_stalled_run(db: AsyncSession, stalled, body: "RetryRunRequest | None", user: User) -> dict:
    """Retry of a run its frozen Flow could not advance: start over on the current Flow."""
    from packages.core.services.workspace_flow_launcher import enqueue_binding_run

    try:
        run, launched = await svc.restart_stalled_run(
            db, stalled, user=user, variables=body.variables if body else None,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc)) from exc
    await db.commit()
    await enqueue_binding_run(db, run, launched)
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
    latest_visible_attempt = await svc.latest_workflow_run_family_attempt(
        db,
        existing_run,
    )
    # A second click (or a second tab) must not start a parallel attempt while
    # the first retry of this run is still going.
    if latest_visible_attempt.status in {"pending", "running"}:
        raise HTTPException(
            409,
            f"A retry of this run is already running as {latest_visible_attempt.id}. "
            "Wait for it to finish instead of starting another.",
        )
    retry_target = await _deepest_retryable_subworkflow_run(
        db,
        latest_visible_attempt,
    )
    if (
        retry_target.id != existing_run.id
        and retry_target.status in {"pending", "running", "paused", "completed"}
    ):
        raise HTTPException(409, "Nested workflow recovery is already in progress")
    from packages.core.services.workflow_run_trace import workflow_run_stalled

    if retry_target.id == existing_run.id and workflow_run_stalled(existing_run):
        if body and body.supplied_output:
            raise HTTPException(409, "A stalled run starts over; it can't accept a supplied output")
        return await _restart_stalled_run(db, existing_run, body, user)
    try:
        run = await svc.retry_workflow_run(
            db,
            run_id=retry_target.id,
            entity_id=user.entity_id,
            started_by=user.id,
            from_step_id=body.from_step_id if body else None,
            variables=body.variables if body else None,
            owner_run_id=existing_run.id,
            expected_owner_run_id=latest_visible_attempt.id,
            expected_owner_status=latest_visible_attempt.status,
            effect_review=body.effect_review.model_dump() if body and body.effect_review else None,
            definition=body.definition if body else "current",
            supplied_output=body.supplied_output.model_dump() if body and body.supplied_output else None,
        )
    except ValueError as exc:
        message = str(exc)
        normalized = message.lower()
        status_code = (
            404
            if "not found" in normalized
            else 409
            if (
                "changed since" in normalized
                or "newer workflow attempt" in normalized
                or "retry is blocked" in normalized
                or "can't be retried" in normalized
                or "workflow preflight" in normalized
                or "requires an active workspace" in normalized
                or "no longer part of the workflow" in normalized
            )
            else 400
        )
        raise HTTPException(status_code, message) from exc
    await db.commit()

    if body is None or body.execute:
        if WorkflowRunner.enqueue(run.id) is False:
            run.status = "failed"
            run.error = "Workflow could not be queued. Please retry this run."
            run.completed_at = datetime.now(timezone.utc)
            mark_workflow_terminal_effects_pending(run)
            from packages.core.services.workflow_chat_projection import (
                project_workflow_run_status,
            )
            from packages.core.services.workflow_run_trace import (
                update_workflow_history_summary,
            )

            update_workflow_history_summary(run)
            await project_workflow_run_status(db, run=run)
            await db.commit()
    return _run_to_dict(run)


@router.post("/runs/{run_id}/revise", status_code=201)
async def revise_run(
    run_id: str,
    body: ReviseRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Make the next version of a finished run's result: from the step its
    Workflow's ``production.revise`` names, with the person's notes and the unit
    versions they picked. The earlier version stays in the run's history."""
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.workflow_production import production_revision_inputs, workflow_production_config

    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await _require_run_control(db, run, user)
    if run.status != "completed":
        raise HTTPException(409, "Only a finished run can be revised")
    # A later attempt that was cancelled made nothing, so this is still the latest version.
    latest = await svc.latest_workflow_run_family_attempt(db, run)
    if latest.id != run.id and latest.status != "cancelled":
        raise HTTPException(409, "A newer version of this run already exists")
    steps = await db.scalar(select(WorkflowDefinition.steps).where(
        WorkflowDefinition.id == run.workflow_id, WorkflowDefinition.entity_id == run.entity_id,
    ))
    revise = (workflow_production_config(steps) or {}).get("revise")
    if not revise:
        raise HTTPException(400, "This Workflow does not declare how its result is revised")
    notes = body.notes.strip()
    versions = {str(unit)[:128]: str(job)[:64] for unit, job in body.unit_versions.items() if str(job).strip()}
    if not notes and not versions:
        raise HTTPException(400, "Say what should change, or pick new versions of its units")
    # The first of the declared steps this run reached; a later version reuses
    # what came before it on the path the run actually took.
    reached = run.step_results or {}
    from_step = next((step for step in revise["from_step"] if step in reached), None)
    if from_step is None:
        raise HTTPException(409, "This run did not reach the step its revisions start from")
    variables = production_revision_inputs(revise, run.variables or {}, notes=notes, unit_versions=versions)
    try:
        revised = await svc.retry_workflow_run(
            db, run_id=run.id, entity_id=user.entity_id, started_by=user.id,
            from_step_id=from_step, variables=variables,
            owner_run_id=run.id, expected_owner_run_id=latest.id, expected_owner_status=latest.status,
            definition="current", revision=True,
        )
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(409 if "newer" in message.lower() or "blocked" in message.lower() else 400, message) from exc
    await db.commit()
    if WorkflowRunner.enqueue(revised.id) is False:
        revised.status = "failed"
        revised.error = "Workflow could not be queued. Please retry this run."
        revised.completed_at = datetime.now(timezone.utc)
        mark_workflow_terminal_effects_pending(revised)
        from packages.core.services.workflow_chat_projection import project_workflow_run_status
        from packages.core.services.workflow_run_trace import update_workflow_history_summary

        update_workflow_history_summary(revised)
        await project_workflow_run_status(db, run=revised)
        await db.commit()
    return _run_to_dict(revised)


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


@router.get(
    "/{workflow_id}/run-inputs/{input_key}/choices",
    response_model=list[WorkflowRecordChoiceResponse],
)
async def list_workflow_record_choices(
    workflow_id: str,
    input_key: str,
    workspace_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Options of a ``record_select`` or ``workflow_project_select`` run input.

    A ``record_select`` reads its Workspace Ledger; a ``workflow_project_select``
    offers the Workspace's projects of its type (value = project id). The
    source comes from the saved Workflow, never from the caller. A Ledger that
    is not installed or has no matching rows yields no options.
    """
    from packages.core.services.workflow_project_select import load_project_choices, project_select
    from packages.core.services.workflow_record_source import (
        RecordSourceUnavailable,
        load_record_choices,
        record_select_source,
    )
    from packages.core.services.workspace_workflow_router import workflow_run_inputs

    wf = await _require_workflow(db, user, workflow_id, Capability.VIEW)
    await _require_binding_workspace_read(db, user, workspace_id)
    workspaces = await workflow_workspace_ids_batch(db, [wf.id], entity_id=user.entity_id)
    if wf.workspace_id != workspace_id and workspace_id not in workspaces.get(wf.id, set()):
        raise HTTPException(404, "Workflow not found in this Workspace")
    item = next((row for row in workflow_run_inputs(wf) if row["key"] == input_key), None)
    picker = project_select(item.get("schema")) if item else None
    if picker is not None:
        return await load_project_choices(db, picker, entity_id=user.entity_id, workspace_id=workspace_id)
    source = record_select_source(item.get("schema")) if item else None
    if source is None:
        raise HTTPException(404, "Run input has no record choices")
    try:
        return await load_record_choices(
            db, source, entity_id=user.entity_id, workspace_id=workspace_id,
        )
    except RecordSourceUnavailable as exc:
        raise HTTPException(
            503, {"code": "record_source_unavailable", "message": str(exc)},
        ) from None


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
    current = await _require_workflow(db, user, workflow_id, Capability.EDIT)
    changes = body.model_dump(exclude_none=True)
    require_valid_steps = bool(changes.pop("validate_steps", False))
    expected_version = changes.pop("expected_version", None)
    if "steps" in changes:
        await require_collaborator_workflow_steps(
            db, user_id=user.id, entity_id=user.entity_id, workspace_id=current.workspace_id,
            steps=changes["steps"], user=user,
        )
    try:
        wf = await svc.update_workflow(
            db,
            workflow_id,
            user.entity_id,
            require_valid_steps=require_valid_steps,
            expected_version=expected_version,
            **changes,
        )
    except StaleRevisionError as exc:
        raise CodedError(
            409,
            code="workflow.error.edited_elsewhere",
            message=(
                f"This workflow was saved by someone else after version {exc.expected} "
                f"(it is now at version {exc.actual}). Load it again and reapply your change."
            ),
            vars={"expected": exc.expected, "actual": exc.actual},
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
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
    try:
        deleted = await svc.delete_workflow(db, workflow_id, user.entity_id)
    except svc.WorkflowInUseError as exc:
        raise HTTPException(
            409,
            {
                "message": str(exc),
                "binding_ids": exc.binding_ids,
                "scheduled_job_ids": exc.scheduled_job_ids,
            },
        ) from exc
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
    except WorkflowRunInputError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e).lower() else 409, str(e))
    queue = body is not None and body.queue
    execute = not queue and (body is None or body.execute)
    cache_index = await _prime_run_cache(db, workflow_id, run.id) if execute else {}
    # Commit ends the request transaction: the runner needs its own database
    # admission slot, and the API may have only one per loop.
    await db.commit()

    # By default execute the run inline so it actually runs (no Celery worker in
    # dev). ``execute=false`` creates the run only (manual /step driving). The
    # runner manages its own session + commits; we refresh for final state.
    if execute:
        run_id = run.id
        try:
            await WorkflowRunner(cache_index=cache_index).run(run_id)
        except Exception:
            logger.exception("inline workflow run failed: %s", run_id)
        await db.refresh(run)
    elif queue:
        # A failed publish is not lost: dispatch recovery republishes the unclaimed run.
        WorkflowRunner.enqueue(run.id)
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
    except WorkflowRunInputError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e).lower() else 409, str(e))
    run_id = run.id
    entity_id = user.entity_id
    # Reuse unchanged steps from the previous run (ComfyUI incremental re-exec).
    cache_index = await _prime_run_cache(db, workflow_id, run_id)
    # Commit ends the request transaction before the runner takes its own slot.
    await db.commit()

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
