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
from packages.core.contracts.artifact_delivery import artifact_result_schema
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import TaskLog
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
)
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
        _accepted_terminal_output,
        _accepted_terminal_text_summary,
        _artifact_refs_from_result,
        _dedupe_task_artifact_refs,
        _step_result_summary,
    )
    from packages.core.contracts.artifact_delivery import task_artifact_refs_for_output

    verified_files = task_artifact_refs_for_output(plan, steps)

    review_verdict = (supervisor_review or {}).get("verdict")
    review_accepts_output = review_verdict == SupervisorVerdict.COMPLETED.value
    validated_task_output = (
        _accepted_terminal_output(
            task, plan, steps, supervisor_accepted=review_accepts_output,
        )
        if task is not None
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
                    verified_files.get(str(step.id), []) if verified_files is not None
                    else _artifact_refs_from_result(step.result, step_key=step.step_key, output_schema=artifact_result_schema(step)),
                    entity_id=plan.entity_id,
                )
                if refs:
                    entry["files"] = refs
                    all_files.extend(refs)
                if verified_files is None and step.result.get("document_id"):
                    entry["document_id"] = step.result["document_id"]
                if verified_files is None and step.result.get("fs_path"):
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
        actual_output["result"] = validated_task_output[1]
        actual_output["result_step_key"] = validated_task_output[0]
        actual_output["result_contract_source"] = validated_task_output[2]
    terminal_text_summary = _accepted_terminal_text_summary(
        task, steps, supervisor_accepted=bool(review_accepts_output),
    ) if task is not None else None
    if terminal_text_summary:
        actual_output["summary"] = terminal_text_summary
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

    from packages.core.services.file_refs import parse_file_reference
    from packages.core.services.generated_file_refs import canonical_document_id

    for item in _actual_artifact_refs(actual):
        if not isinstance(item, dict) or canonical_document_id(item.get("document_id")):
            continue
        if any(item.get(key) for key in ("fs_path", "path", "file_path", "local_path", "saved_to")):
            return True
        url = str(item.get("url") or item.get("file_url") or item.get("result_url") or "")
        file_ref = parse_file_reference(url)
        if file_ref is not None and file_ref.entity_id:
            return True
    return False


async def _project_actual_output_artifacts(
    task: Any, actual: dict[str, Any], *, db: AsyncSession,
) -> dict[str, Any]:
    refs = _actual_artifact_refs(actual)
    if not refs:
        return actual

    from packages.core.services.artifact_knowledge import project_artifact_refs_to_knowledge

    from packages.core.contracts.artifact_delivery import readonly_artifact_delivery, reused_artifact_snapshots
    from packages.core.services.artifact_delivery import (
        ArtifactDeliveryError,
        submitted_artifact_refs,
        verify_artifact_delivery_refs,
    )
    from packages.core.services.generated_file_refs import generated_file_ref_aliases
    from packages.core.services.task_requester_identity import resolve_task_execution_user_id

    # Read repair must not turn consumption of another Task's file into a
    # producer projection, which would overwrite that Document's origin.
    steps = list((await db.execute(select(ExecutionStep).join(
        ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id,
    ).where(
        ExecutionPlan.id == actual.get("plan_id"),
        ExecutionPlan.task_id == task.id,
        ExecutionPlan.entity_id == task.entity_id,
    ))).scalars().all())
    reused_refs: list[dict[str, Any]] = []
    reused_aliases: set[str] = set()
    try:
        for step in steps:
            if not readonly_artifact_delivery(step.params):
                continue
            snapshots = reused_artifact_snapshots(step)
            if not snapshots:
                raise ArtifactDeliveryError("artifact_evidence_missing", "The read-only Step has no verified artifact delivery receipt.")
            user_id = await resolve_task_execution_user_id(db, task)
            verified = await verify_artifact_delivery_refs(
                db, entity_id=task.entity_id, workspace_id=task.workspace_id,
                user_id=user_id, refs=submitted_artifact_refs(step.result, output_schema=step.expected_output_schema),
                expected_snapshots=snapshots,
            )
            reused_refs.extend(verified.projection.refs)
            for ref in verified.projection.refs:
                reused_aliases.update(generated_file_ref_aliases(ref))
    except ArtifactDeliveryError as exc:
        return {**actual, "artifact_knowledge_issues": [{"reason": exc.code}]}

    refs = [ref for ref in refs if not (
        generated_file_ref_aliases(ref) & reused_aliases
    )]
    if refs and reused_refs and all(readonly_artifact_delivery(step.params) for step in steps):
        # A stale/display-only extra reference is not authority to turn a
        # reuse-only Plan into a producer during a GET/read-repair path.
        return {**actual, "artifact_knowledge_issues": [{"reason": "artifact_evidence_mismatch"}]}
    if not refs:
        projected = {**actual, "files": reused_refs}
        projected.pop("artifact_knowledge_issues", None)
        return projected

    projection = await project_artifact_refs_to_knowledge(
        entity_id=task.entity_id,
        refs=refs,
        workspace_id=getattr(task, "workspace_id", None),
        task_id=task.id,
        agent_id=getattr(task, "agent_id", None),
        tool_name="task_execution_reconcile",
    )
    projected = dict(actual)
    projected["files"] = [*projection.refs, *reused_refs]
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
    plan_state = plan.dispatcher_state or {}
    if plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION:
        acceptance = plan_state.get("task_content_acceptance")
        if (
            not isinstance(acceptance, dict)
            or acceptance.get("verdict") != "accepted"
            or not isinstance(acceptance.get("snapshot_digest"), str)
            or not acceptance["snapshot_digest"]
            or not isinstance(acceptance.get("checks"), list)
            or not acceptance["checks"]
            or supervisor_verdict != SupervisorVerdict.COMPLETED.value
        ):
            # V3 writes acceptance and Task completion in one transaction.
            # A read repair must not turn a merely mechanical Plan completion
            # (or a corrupt/incomplete terminal write) into accepted delivery.
            return False
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
        output_contract_for_schema,
        task_expected_output_declares_schema,
    )
    from packages.core.plans.executor import _is_coordinator_child

    steps = list((await db.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        .order_by(ExecutionStep.created_at)
    )).scalars().all())
    is_v3 = plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION
    if is_v3:
        from packages.core.contracts.artifact_delivery import accepted_task_artifact_manifest

        if accepted_task_artifact_manifest(plan, steps) is None:
            # A stale/missing receipt cannot be repaired by rediscovering
            # payload links or rebinding a Document's producer on a GET.
            return False

    requires_structured_result = (
        task_expected_output_declares_schema(getattr(task, "expected_output", None))
        or any(
            not _is_coordinator_child(step)
            and output_contract_for_schema(step.expected_output_schema).is_bare_payload
            for step in steps
        )
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
        and not is_v3
    ):
        projected = actual
        if _has_unprojected_local_artifacts(actual):
            projected = await _project_actual_output_artifacts(task, actual, db=db)
        if (
            not projected.get("summary")
            and supervisor_verdict == SupervisorVerdict.COMPLETED.value
        ):
            from packages.core.plans.executor import _accepted_terminal_text_summary

            summary = _accepted_terminal_text_summary(
                task, steps, supervisor_accepted=True,
            )
            if summary:
                projected = {**projected, "summary": summary}
        if projected == actual:
            return False
        task.actual_output = projected
        return True

    rebuilt_output = _actual_output_from_steps(
        plan,
        steps,
        task=task,
        supervisor_review=supervisor_review,
    )
    if requires_structured_result and "result" not in rebuilt_output:
        # A mechanically completed Plan is insufficient with an explicit
        # Task/Plan result contract. If the persisted terminal Step cannot
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
        and actual.get("result_contract_source") == rebuilt_output["result_contract_source"]
        and not is_v3
    ):
        if not _has_unprojected_local_artifacts(actual):
            return False
        projected = await _project_actual_output_artifacts(task, actual, db=db)
        if projected == actual:
            return False
        task.actual_output = projected
        return True

    if is_v3:
        if actual == rebuilt_output and task_status == "completed":
            return False
        task.actual_output = rebuilt_output
    else:
        task.actual_output = await _project_actual_output_artifacts(
            task,
            rebuilt_output,
            db=db,
        )

    if task.actual_output.get("artifact_knowledge_issues"):
        return True

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


# Keep aligned with PlanExecutor.MAX_REPLANS. Importing the executor here
# would pull the whole planning runtime into a read-repair module.
_ORPHAN_REPLAN_BUDGET = 2
_ORPHAN_RECOVERY_DETAIL_KEY = "_orphan_plan_recovery"


async def recover_orphaned_in_progress_task(db: AsyncSession, task: Any) -> str | None:
    """Fail or re-dispatch an in_progress Task that no longer has a live Plan.

    Returns ``failed``, ``redispatched``, or ``None`` when no repair is needed.
    """
    from packages.core.constants.execution import PLAN_TERMINAL_STATUSES
    from packages.core.constants.task import TaskRuntimeKind, TaskStatus
    from packages.core.constants.task_actors import TaskActor
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task import Task
    from packages.core.models.task_coordinator import (
        TaskCoordinatorPhase,
        TaskCoordinatorRun,
    )
    from packages.core.services.task_coordinator_wakeup import task_coordinator_run_reopenable
    from packages.core.services.task_execution import (
        TASK_EXECUTION_OUTBOX_EVENT,
        queue_task_execution_after_commit,
        resolve_task_execution,
        stage_task_execution_intent,
        task_execution_intent_dedupe_key,
        task_execution_intent_generation,
    )
    from packages.core.services.task_service import add_task_log

    task_id = getattr(task, "id", None)
    entity_id = getattr(task, "entity_id", None)
    workspace_id = getattr(task, "workspace_id", None)
    if not task_id or not entity_id:
        return None
    if str(getattr(task, "status", "") or "") != TaskStatus.IN_PROGRESS.value:
        return None

    if workspace_id:
        from packages.core.services.workspace_access import (
            lock_workspace_access_boundary,
        )

        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=workspace_id,
            entity_id=entity_id,
        )
        if workspace is None:
            return None

    # Serialize the marker with its durable dispatch. Without the Task lock,
    # two Strategist reconciliation passes can both observe the missing Plan
    # and publish independent recovery attempts.
    locked_task = (await db.execute(
        select(Task)
        .where(Task.id == task_id, Task.entity_id == entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        locked_task is None
        or locked_task.status != TaskStatus.IN_PROGRESS.value
        or locked_task.entity_id != entity_id
        or locked_task.workspace_id != workspace_id
    ):
        return None
    task = locked_task

    plans = list((await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.task_id == task_id,
        )
    )).scalars().all())
    terminal = {status.value for status in PLAN_TERMINAL_STATUSES}
    if any(str(plan.status or "") not in terminal for plan in plans):
        return None

    dispatch = await resolve_task_execution(db, task)
    if dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        # A V3 coordinator legitimately owns intervals with no materialized
        # Plan while planning, waiting on a child, or awaiting another wake.
        # Its unique Run is the liveness record; do not manufacture a second
        # start merely because the Plan table is temporarily empty.
        generation = task_execution_intent_generation(task)
        run = (await db.execute(
            select(TaskCoordinatorRun)
            .where(TaskCoordinatorRun.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if run is not None:
            same_generation = run.lifecycle_generation == generation
            if (
                same_generation
                and run.phase not in TaskCoordinatorPhase.terminal()
            ):
                return None
            if not same_generation and not task_coordinator_run_reopenable(run):
                # Only a run the new lifecycle may reopen (a stopped run, or
                # a paused agent loop it rebinds in place) is redispatched.
                return None
            if same_generation:
                return await _fail_orphaned_in_progress_task(
                    db,
                    task,
                    reason="coordinator_terminal_without_task_settlement",
                )

        # A previous reconciliation may already have committed the same
        # generation marker and durable outbox row. Reuse it instead of
        # treating the short pre-consumption window as lost planning.
        pending_intent = (await db.execute(
            select(RuntimeOutboxEvent.id).where(
                RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == task_id,
                RuntimeOutboxEvent.dedupe_key
                == task_execution_intent_dedupe_key(task_id, generation),
                RuntimeOutboxEvent.delivered_at.is_(None),
            )
        )).scalar_one_or_none()
        if pending_intent is not None:
            return None
    elif dispatch.runtime_kind is not TaskRuntimeKind.LEGACY_AGENT:
        # Manual, approval and interactive Tasks do not own a background Plan
        # runtime. Leaving them untouched is safer than routing around the
        # factory with a fabricated legacy dispatch.
        return None

    details = dict(getattr(task, "details", None) or {})
    marker = details.get(_ORPHAN_RECOVERY_DETAIL_KEY)
    marker = marker if isinstance(marker, dict) else {}
    terminal_count = len(plans)
    if terminal_count >= _ORPHAN_REPLAN_BUDGET:
        return await _fail_orphaned_in_progress_task(
            db,
            task,
            reason="replan_budget_exhausted",
        )
    if marker.get("action") == "redispatched":
        return await _fail_orphaned_in_progress_task(
            db,
            task,
            reason="planning_lost",
        )

    details[_ORPHAN_RECOVERY_DETAIL_KEY] = {
        "action": "redispatched",
        "reason": "no_live_plan",
        "runtime_kind": dispatch.runtime_kind.value,
    }
    task.details = details
    if dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        await stage_task_execution_intent(db, task, dispatch)
    elif dispatch.runtime_kind is TaskRuntimeKind.LEGACY_AGENT:
        queue_task_execution_after_commit(db, task_id, dispatch)
    else:
        raise AssertionError("orphan recovery admitted a non-dispatchable runtime")
    await add_task_log(
        db,
        task_id,
        TaskLogType.STATUS_CHANGE,
        "Requeued planning after the Task lost its live Plan.",
        actor=TaskActor.SYSTEM,
        metadata={"mode": "orphaned_in_progress_recovery"},
    )
    await db.flush()
    return "redispatched"


async def _fail_orphaned_in_progress_task(
    db: AsyncSession,
    task: Any,
    *,
    reason: str,
) -> str:
    from packages.core.constants.task import TaskStatus
    from packages.core.constants.task_actors import TaskActor
    from packages.core.services.task_service import add_task_log

    details = dict(getattr(task, "details", None) or {})
    details[_ORPHAN_RECOVERY_DETAIL_KEY] = {
        "action": "failed",
        "reason": reason,
    }
    task.details = details
    await apply_task_status_transition(
        task,
        TaskStatus.FAILED.value,
        db=db,
        actor_kind=TaskActor.SYSTEM.value,
    )
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_EXECUTION_FAILED,
        "Failed an in-progress Task that had no live Plan left to execute.",
        actor=TaskActor.SYSTEM,
        metadata={"mode": "orphaned_in_progress_recovery", "reason": reason},
    )
    try:
        from packages.core.services.workspace_operation_service import (
            check_work_batch_completion,
        )

        await check_work_batch_completion(
            db,
            task,
            trigger_source="orphaned_in_progress_recovery",
        )
    except Exception:
        logger.warning(
            "task %s: failed to evaluate workspace work batch completion after orphan recovery",
            getattr(task, "id", None),
            exc_info=True,
        )
    await db.flush()
    return "failed"

_TOOL_ERROR_HUMAN_WAIT_RECOVERY_DETAIL_KEY = "_tool_error_human_wait_recovery"


def _hitl_request_haystack(request: Any) -> str:
    import json

    payload = getattr(request, "payload", None) or {}
    context = getattr(request, "context", None) or {}
    return " ".join(
        [
            str(getattr(request, "reason", None) or ""),
            json.dumps(payload, default=str, ensure_ascii=False),
            json.dumps(context, default=str, ensure_ascii=False),
            str(getattr(request, "matched_rule", None) or ""),
        ]
    )


async def recover_misclassified_tool_error_human_wait(db: AsyncSession, task: Any) -> str | None:
    """Fail a false human-wait parked by a tool error recovery HITL.

    Returns ``failed`` when the Task is moved off ``waiting_on_customer``,
    otherwise ``None``. Real Need Input and credential waits are left alone.
    """
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.constants.task import TaskStatus
    from packages.core.constants.task_actors import TaskActor
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.plans.executor import tool_error_failure_is_not_human_required
    from packages.core.services.task_service import add_task_log

    task_id = getattr(task, "id", None)
    if not task_id:
        return None
    if str(getattr(task, "status", "") or "") != TaskStatus.WAITING_ON_CUSTOMER.value:
        return None

    requests = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.origin_task_id == task_id,
            HitlRequest.status == ApprovalStatus.PENDING.value,
            HitlRequest.action_key == "task.recover",
        )
    )).scalars().all())
    if not requests:
        return None
    haystack = " ".join(_hitl_request_haystack(request) for request in requests)
    if not tool_error_failure_is_not_human_required(haystack):
        return None

    details = dict(getattr(task, "details", None) or {})
    details[_TOOL_ERROR_HUMAN_WAIT_RECOVERY_DETAIL_KEY] = {
        "action": "failed",
        "reason": "tool_error_not_human_required",
    }
    task.details = details
    await apply_task_status_transition(
        task,
        TaskStatus.FAILED.value,
        db=db,
        actor_kind=TaskActor.SYSTEM.value,
    )
    try:
        from packages.core.governance.approvals import resolve_origin_requests
        from packages.core.governance.service import resolve_stale_hitl_cards

        await resolve_origin_requests(
            db,
            task_id=task_id,
            reason="tool_error_not_human_required",
        )
        await resolve_stale_hitl_cards(
            db,
            task_id=task_id,
            reason="tool_error_not_human_required",
            pending_action_kinds=[
                PendingActionKind.TASK_RECOVERY.value,
                PendingActionKind.GOVERNANCE_APPROVAL.value,
            ],
        )
    except Exception:
        logger.warning(
            "task %s: failed to expire misclassified tool-error HITL",
            task_id,
            exc_info=True,
        )
    await add_task_log(
        db,
        task_id,
        TaskLogType.AI_EXECUTION_FAILED,
        "Failed a customer-wait Task whose recovery HITL was a tool error, not a human input.",
        actor=TaskActor.SYSTEM,
        metadata={"mode": "misclassified_tool_error_human_wait"},
    )
    try:
        from packages.core.services.workspace_operation_service import (
            check_work_batch_completion,
        )

        await check_work_batch_completion(
            db,
            task,
            trigger_source="misclassified_tool_error_human_wait",
        )
    except Exception:
        logger.warning(
            "task %s: failed to evaluate workspace work batch completion after tool-error HITL recovery",
            task_id,
            exc_info=True,
        )
    await db.flush()
    return "failed"


async def recover_misclassified_tool_error_human_waits(
    db: AsyncSession,
    *,
    limit: int = 50,
) -> int:
    """Sweep pending task.recover waits that are actually tool errors."""
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.constants.task import TaskStatus
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Task

    rows = list((await db.execute(
        select(Task)
        .join(HitlRequest, HitlRequest.origin_task_id == Task.id)
        .where(
            Task.status == TaskStatus.WAITING_ON_CUSTOMER.value,
            HitlRequest.status == ApprovalStatus.PENDING.value,
            HitlRequest.action_key == "task.recover",
        )
        .limit(limit)
    )).scalars().unique().all())
    recovered = 0
    for task in rows:
        action = await recover_misclassified_tool_error_human_wait(db, task)
        if action == "failed":
            recovered += 1
    return recovered
