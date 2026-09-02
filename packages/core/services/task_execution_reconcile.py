"""Best-effort reconciliation between task rows and their latest plan.

The executor owns the canonical write path. This module is a defensive
read-time repair layer for older rows or race windows where a completed plan
was recorded but the task's business status/output did not catch up.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.supervisor import (
    SUPERVISOR_VERDICT_LOG_TYPE,
    SupervisorVerdict,
)
from packages.core.constants.task import (
    HITL_LOG_TYPES,
    HITL_REQUEST_LOG_TYPES,
    TaskLogType,
)
from packages.core.constants.execution import ExecutionPlanStatus
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import TaskLog
from packages.core.services.task_state_machine import apply_task_status_transition

logger = logging.getLogger(__name__)


_PLAN_COMPLETION_RECONCILABLE_TASK_STATUSES = {
    "pending",
    "in_progress",
    "waiting_on_customer",
    "completed",
    # A retry can complete a new plan after the previous plan finalized the
    # task as failed; dependency reads must be able to repair that stale row.
    "failed",
}


def _actual_output_from_steps(
    plan: ExecutionPlan,
    steps: list[ExecutionStep],
    *,
    task: Any | None = None,
    supervisor_review: dict[str, str] | None = None,
) -> dict[str, Any]:
    # Keep artifact extraction identical to the executor so task output,
    # dependency handoff, and UI display agree on file references.
    from packages.core.plans.executor import (
        _artifact_refs_from_result,
        _dedupe_task_artifact_refs,
        _step_result_summary,
        _validated_terminal_task_output,
    )

    review_verdict = (supervisor_review or {}).get("verdict")
    review_accepts_output = review_verdict == SupervisorVerdict.COMPLETED.value
    validated_task_output = (
        _validated_terminal_task_output(task, steps)
        if task is not None and review_accepts_output
        else None
    )
    step_summaries: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    for step in steps:
        entry: dict[str, Any] = {
            "key": step.step_key,
            "kind": step.kind,
            "status": step.step_status,
        }
        if step.result:
            entry["result_summary"] = _step_result_summary(step.result)
            if isinstance(step.result, dict):
                refs = _dedupe_task_artifact_refs(
                    _artifact_refs_from_result(step.result, step_key=step.step_key),
                    entity_id=plan.entity_id,
                )
                if refs:
                    entry["files"] = refs
                    all_files.extend(refs)
                if step.result.get("document_id"):
                    entry["document_id"] = step.result["document_id"]
                if step.result.get("fs_path"):
                    entry["fs_path"] = step.result["fs_path"]
        if validated_task_output and step.step_key == validated_task_output[0]:
            entry["data"] = validated_task_output[1]
        if step.error:
            entry["error"] = {
                "type": step.error.get("type", "unknown"),
                "message": str(step.error.get("message", ""))[:300],
            }
        step_summaries.append(entry)

    actual_output = {
        "plan_id": plan.id,
        "plan_status": plan.status,
        "steps": step_summaries,
        "files": _dedupe_task_artifact_refs(
            all_files,
            entity_id=plan.entity_id,
        ) if all_files else None,
        "reconciled_from_plan": True,
    }
    if validated_task_output is not None:
        from packages.core.contracts.task_output import TASK_OUTPUT_CONTRACT_SOURCE

        actual_output["result"] = validated_task_output[1]
        actual_output["result_step_key"] = validated_task_output[0]
        actual_output["result_contract_source"] = TASK_OUTPUT_CONTRACT_SOURCE
    if supervisor_review:
        if supervisor_review.get("verdict"):
            actual_output["supervisor_verdict"] = supervisor_review["verdict"]
        if supervisor_review.get("evidence"):
            actual_output["supervisor_evidence"] = supervisor_review["evidence"]
    return actual_output


def _has_duplicate_file_refs(actual: dict[str, Any]) -> bool:
    from packages.core.services.generated_file_refs import generated_file_ref_aliases

    seen: set[str] = set()
    for item in actual.get("files") or []:
        if not isinstance(item, dict):
            continue
        aliases = generated_file_ref_aliases(item)
        if aliases & seen:
            return True
        seen.update(aliases)
    return False


def _actual_artifact_refs(actual: dict[str, Any]) -> list[dict[str, Any]]:
    refs = [dict(item) for item in actual.get("files") or [] if isinstance(item, dict)]
    if refs:
        return refs
    for step in actual.get("steps") or []:
        if not isinstance(step, dict):
            continue
        refs.extend(dict(item) for item in step.get("files") or [] if isinstance(item, dict))
    return refs


def _has_unprojected_local_artifacts(actual: dict[str, Any]) -> bool:
    """A local file is not a delivered Artifact until it has a Document id."""

    from packages.core.services.generated_file_refs import canonical_document_id

    for item in _actual_artifact_refs(actual):
        if not isinstance(item, dict) or canonical_document_id(item.get("document_id")):
            continue
        if any(item.get(key) for key in ("fs_path", "path", "file_path", "local_path", "saved_to")):
            return True
        url = str(item.get("url") or item.get("file_url") or item.get("result_url") or "")
        if "/api/v1/fs/" in url:
            return True
    return False


async def _project_actual_output_artifacts(task: Any, actual: dict[str, Any]) -> dict[str, Any]:
    refs = _actual_artifact_refs(actual)
    if not refs:
        return actual

    from packages.core.services.artifact_knowledge import project_artifact_refs_to_knowledge

    projection = await project_artifact_refs_to_knowledge(
        entity_id=task.entity_id,
        refs=refs,
        workspace_id=getattr(task, "workspace_id", None),
        task_id=task.id,
        agent_id=getattr(task, "agent_id", None),
        tool_name="task_execution_reconcile",
    )
    projected = dict(actual)
    projected["files"] = projection.refs
    if projection.failures:
        projected["artifact_knowledge_issues"] = projection.failures
    else:
        projected.pop("artifact_knowledge_issues", None)
    return projected


async def _has_open_supervisor_input_request(db: AsyncSession, *, task_id: str, plan_id: str) -> bool:
    """Return True when a completed plan deliberately left the task waiting.

    The plan row can be ``completed`` even when the supervisor determined that
    the business task still needs operator input, for example when a user-visible
    artifact was expected but no file/document reference was saved. In that
    case read-time reconciliation must not "helpfully" flip the task back to
    completed just because the execution plan ended.
    """
    rows = list((await db.execute(
        select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.log_type.in_([m.value for m in HITL_LOG_TYPES]),
        ).order_by(TaskLog.created_at)
    )).scalars().all())

    open_request = False
    for log in rows:
        meta = log.meta or {}
        meta_plan_id = str(meta.get("plan_id") or "")
        if meta_plan_id and meta_plan_id != plan_id:
            continue
        if log.log_type == TaskLogType.AI_HITL_RESUMED:
            open_request = False
            continue
        if log.log_type in HITL_REQUEST_LOG_TYPES and (
            meta.get("verdict") == "needs_human"
            or bool(meta.get("artifact_required"))
        ):
            open_request = True
    return open_request


async def _supervisor_review_for_plan(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    actual_output: dict[str, Any],
) -> dict[str, str]:
    """Return the persisted Supervisor decision for this exact Plan, if any."""
    rows = list((await db.execute(
        select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.log_type == SUPERVISOR_VERDICT_LOG_TYPE,
        ).order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
    )).scalars().all())
    for log in rows:
        metadata = log.meta or {}
        if str(metadata.get("plan_id") or "") != plan_id:
            continue
        verdict = str(metadata.get("verdict") or "")
        if verdict:
            return {
                "verdict": verdict,
                "evidence": str(metadata.get("evidence") or ""),
            }
    if actual_output.get("plan_id") == plan_id and actual_output.get("supervisor_verdict"):
        return {
            "verdict": str(actual_output["supervisor_verdict"]),
            "evidence": str(actual_output.get("supervisor_evidence") or ""),
        }
    return {}


async def reconcile_task_from_latest_completed_plan(db: AsyncSession, task: Any) -> bool:
    """Repair stale task output/status from the latest completed plan.

    Returns True when the ORM task object was mutated. This intentionally only
    uses completed plans. A failed plan may legitimately leave the task in
    waiting_on_customer if the supervisor requested human input.
    """
    task_id = getattr(task, "id", None)
    entity_id = getattr(task, "entity_id", None)
    if not task_id or not entity_id:
        return False

    task_status = str(getattr(task, "status", "") or "")
    if task_status not in _PLAN_COMPLETION_RECONCILABLE_TASK_STATUSES:
        return False

    plan = (await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.task_id == task_id,
        ).order_by(
            ExecutionPlan.created_at.desc(),
            ExecutionPlan.id.desc(),
        ).limit(1)
    )).scalar_one_or_none()
    if not plan or plan.status != ExecutionPlanStatus.COMPLETED.value:
        return False

    actual = getattr(task, "actual_output", None) if isinstance(getattr(task, "actual_output", None), dict) else {}
    supervisor_review = await _supervisor_review_for_plan(
        db,
        task_id=task_id,
        plan_id=plan.id,
        actual_output=actual,
    )
    supervisor_verdict = supervisor_review.get("verdict")
    if (
        supervisor_verdict
        and supervisor_verdict != SupervisorVerdict.COMPLETED.value
    ):
        # A completed Plan is only the mechanical execution state.  The
        # Supervisor owns Task acceptance and a read-time repair must never
        # reverse its explicit needs_replan/needs_human/failed decision.
        return False
    if (
        task_status == "waiting_on_customer"
        and not supervisor_verdict
        and (
            await _has_open_supervisor_input_request(
                db,
                task_id=task_id,
                plan_id=plan.id,
            )
        )
    ):
        return False

    from packages.core.contracts.task_output import (
        TASK_OUTPUT_CONTRACT_SOURCE,
        task_expected_output_json_schema,
    )

    requires_structured_result = (
        task_expected_output_json_schema(getattr(task, "expected_output", None))
        is not None
    )
    if (
        requires_structured_result
        and supervisor_verdict != SupervisorVerdict.COMPLETED.value
    ):
        # A completed Plan proves mechanical execution only. Structured Task
        # output becomes formal only after an explicit persisted acceptance.
        return False
    if (
        actual.get("plan_id") == plan.id
        and actual.get("plan_status") == "completed"
        and task_status == "completed"
        and not _has_duplicate_file_refs(actual)
        and not requires_structured_result
    ):
        if not _has_unprojected_local_artifacts(actual):
            return False
        projected = await _project_actual_output_artifacts(task, actual)
        if projected == actual:
            return False
        task.actual_output = projected
        return True

    steps = list((await db.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        .order_by(ExecutionStep.created_at)
    )).scalars().all())
    rebuilt_output = _actual_output_from_steps(
        plan,
        steps,
        task=task,
        supervisor_review=supervisor_review,
    )
    if requires_structured_result and "result" not in rebuilt_output:
        # A mechanically completed Plan is insufficient when the Task owns a
        # hard result schema. If the persisted terminal Step can no longer
        # prove the exact accepted deliverable, leave the Task untouched
        # instead of manufacturing a completed result during read repair.
        return False

    if (
        requires_structured_result
        and task_status == "completed"
        and actual.get("plan_id") == plan.id
        and actual.get("plan_status") == "completed"
        and not _has_duplicate_file_refs(actual)
        and "result" in actual
        and actual["result"] == rebuilt_output["result"]
        and actual.get("result_step_key") == rebuilt_output["result_step_key"]
        and actual.get("result_contract_source") == TASK_OUTPUT_CONTRACT_SOURCE
    ):
        if not _has_unprojected_local_artifacts(actual):
            return False
        projected = await _project_actual_output_artifacts(task, actual)
        if projected == actual:
            return False
        task.actual_output = projected
        return True

    task.actual_output = await _project_actual_output_artifacts(
        task,
        rebuilt_output,
    )

    if task_status != "completed":
        try:
            await apply_task_status_transition(task, "completed", db=db)
        except Exception:
            logger.debug(
                "Could not reconcile task %s status %s from completed plan %s",
                task_id,
                task_status,
                plan.id,
                exc_info=True,
            )
    return True
