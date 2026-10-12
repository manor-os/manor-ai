"""Fenced release of persisted V3 Plan steps by the Task coordinator.

The model chooses when to delegate, but it cannot choose a service, action, or
arguments here: those are the validated, materialized Plan Step rows.  A batch
remains pinned in the Task checkpoint until its exact tool result is durably
recorded by the parent AgentLoop.  Replayed calls therefore cannot run ahead
of child-result inspection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.constants.task import TaskStatus
from packages.core.dispatcher.effect_safety import retry_effect_block_detail
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.plans.wakeup import PlanCycleWake, stage_plan_cycle_wake
from packages.core.services.file_refs import is_open_address
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim


TASK_COORDINATOR_CONTROL_KEY = "task_coordinator_control"
TASK_COORDINATOR_STEP_INPUTS_KEY = "task_coordinator_step_inputs"
TASK_COORDINATOR_DEPENDENCY_ARTIFACTS_KEY = "task_coordinator_dependency_artifacts"
_ACTIVE_STEP_STATUSES = frozenset({
    ExecutionStepStatus.PENDING.value,
    ExecutionStepStatus.RUNNING.value,
    ExecutionStepStatus.WAITING_HUMAN.value,
})
_TERMINAL_STEP_STATUSES = frozenset({
    ExecutionStepStatus.DONE.value,
    ExecutionStepStatus.FAILED.value,
    ExecutionStepStatus.SKIPPED.value,
    ExecutionStepStatus.CANCELLED.value,
})


@dataclass(frozen=True)
class TaskCoordinatorStepRelease:
    status: str  # released | waiting | settled | blocked | all_done
    plan_id: str
    step_ids: tuple[str, ...] = ()
    state_version: int = 0
    tool_result: str | None = None
    reason: str | None = None
    plan_wake: PlanCycleWake | None = None


def task_coordinator_step_input_digest(
    task: Task, plan: ExecutionPlan, step: ExecutionStep, steps: list[ExecutionStep],
) -> str | None:
    """Pin the host's execution inputs, not a model-authored retry assertion.

    Resolve references exactly as execution does, so normal materialization
    does not look like an input edit. Runtime feedback/acquisition bookkeeping
    and the bounded retry allowance are not new business inputs. Missing
    references remain the executor's error; without a digest automatic repair
    is unavailable for that Step.
    """
    from packages.core.plans.executor import PlanExecutor, _step_expects_external_publish
    from packages.core.plans.refs import ReferenceError, resolve_refs

    params = dict(step.params or {})
    for key in (SUPERVISOR_FEEDBACK_PARAM_KEY, "_runtime_capability_acquisitions", "retry_policy"):
        params.pop(key, None)
    try:
        params = resolve_refs(
            params, PlanExecutor._collect_prior_results(steps),
            strict=_step_expects_external_publish(step, plan.plan_dag),
        )
    except ReferenceError:
        return None
    by_key = {row.step_key: row for row in steps}
    dependencies = []
    for key in step.depends_on or []:
        source = by_key.get(key)
        if source is None or source.step_status != ExecutionStepStatus.DONE.value:
            return None
        dependencies.append({
            "step_id": source.id, "result": source.result,
            "evidence_refs": source.evidence_refs,
        })
    payload = {
        "task": {key: getattr(task, key, None) for key in (
            "id", "entity_id", "workspace_id", "title", "description", "task_type",
            "creator_id", "owner_id", "agent_id", "agent_type",
            "input_contract", "expected_output", "owner_subscription_id",
            "owner_service_key", "delegate_service_keys", "required_skills",
        )},
        "details": {key: value for key, value in (task.details or {}).items() if not key.startswith("_")},
        "plan_id": plan.id, "plan_dag": plan.plan_dag,
        "step": {key: getattr(step, key, None) for key in (
            "id", "kind", "service_key", "provider", "action_key", "integration_id",
            "depends_on", "expected_input_schema", "expected_output_schema",
            "risk_level", "requires_approval",
        )},
        "params": params, "dependencies": dependencies,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def verified_task_coordinator_dependency_artifacts(
    db: AsyncSession, *, task: Task, plan: ExecutionPlan,
    steps: list[ExecutionStep], selected: list[ExecutionStep],
) -> dict[str, list[dict[str, Any]]]:
    """Recheck the artifacts being consumed, without minting producer receipts.

    Persisted result hashes do not prove that file bytes or ACLs are current.
    First consumption pins the verifier's snapshots; any retry must match
    those exact versions. The verifier's nonwaiting filesystem lock avoids
    waiting for a writer while the short V3 database boundary is held.
    """
    from packages.core.contracts.artifact_delivery import reused_artifact_snapshots
    from packages.core.services.artifact_delivery import (
        ArtifactDeliveryError, submitted_artifact_refs, verify_artifact_delivery_refs,
    )
    from packages.core.services.task_requester_identity import TaskRequesterIdentityError, resolve_task_execution_user_id

    pinned = dict((plan.dispatcher_state or {}).get(TASK_COORDINATOR_DEPENDENCY_ARTIFACTS_KEY) or {})
    dependencies = {key for step in selected for key in step.depends_on or []}
    total_refs = 0
    for source in steps:
        if source.step_key not in dependencies or source.step_status != ExecutionStepStatus.DONE.value:
            continue
        contract = (source.params or {}).get("_execution_contract") or {}
        artifact_delivery = contract.get("delivery") == "artifact" or any(
            isinstance(ref, dict) and ref.get("kind") == "artifact" for ref in source.evidence_refs or []
        )
        try:
            refs = submitted_artifact_refs(source.result, output_schema=source.expected_output_schema)
        except ArtifactDeliveryError as exc:
            if exc.code == "artifact_reference_missing" and not artifact_delivery:
                continue
            raise
        if not artifact_delivery:
            # An ordinary inline web-search URL is not a Knowledge artifact.
            refs = [ref for ref in refs if ref.get("document_id") or any(
                ref.get(key) for key in ("fs_path", "path", "file_path", "local_path", "saved_to", "output_path")
            ) or any(is_open_address(ref.get(key)) for key in ("url", "open_url", "file_url"))]
            if not refs:
                continue
        total_refs += len(refs)
        if total_refs > 32:
            raise ArtifactDeliveryError("artifact_reference_limit", "Too many dependency artifacts for one bounded Step release.")
        expected = pinned.get(source.id) or reused_artifact_snapshots(source) or None
        if expected is None and any(step.attempt_count and source.step_key in (step.depends_on or []) for step in selected):
            raise ArtifactDeliveryError("artifact_evidence_missing", "The failed Step has no pinned dependency artifact version; inspect the saved file before retrying.")
        try:
            user_id = await resolve_task_execution_user_id(db, task)
        except TaskRequesterIdentityError as exc:
            raise ArtifactDeliveryError("artifact_unavailable", "The Task requester no longer has access to its dependency artifacts.") from exc
        verified = await verify_artifact_delivery_refs(
            db, entity_id=task.entity_id, workspace_id=task.workspace_id,
            user_id=user_id, refs=refs, expected_snapshots=expected,
        )
        pinned[source.id] = verified.snapshots
    return pinned


async def _locked_scope(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: TaskCoordinatorClaim,
    expected_state_version: int,
) -> tuple[Task, ExecutionPlan, TaskCoordinatorRun, list[ExecutionStep]]:
    from packages.core.services.task_coordinator_fence import lock_task_coordinator_admission
    from packages.core.services.task_execution import task_execution_intent_generation

    # Workspace -> Task -> subscriptions precedes the Plan execution suffix.
    # Recheck live routing inside the mutation transaction, not just before
    # the coordinator's provider call.
    task, selection = await lock_task_coordinator_admission(db, task_id=task_id)
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or plan is None:
        raise ValueError("Task coordinator Task or Plan disappeared")
    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    if (
        not task.workspace_id
        or plan.task_id != task.id
        or plan.entity_id != task.entity_id
        or plan.workspace_id != task.workspace_id
        or state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION
        or not state.get(TASK_COORDINATOR_RUN_ID_KEY)
    ):
        raise ValueError("Task coordinator Plan lineage or V3 marker changed")
    steps = list((await db.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        .order_by(ExecutionStep.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    if not steps:
        raise ValueError("Task coordinator Plan has no materialized Steps")
    run = (await db.execute(
        select(TaskCoordinatorRun)
        .where(TaskCoordinatorRun.id == state[TASK_COORDINATOR_RUN_ID_KEY])
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    expiry = run.claim_expires_at if run is not None else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (
        run is None or claim.task_id != task_id
        or run.id != claim.run_id or run.task_id != task.id
        or run.entity_id != task.entity_id or run.workspace_id != task.workspace_id
        or run.lifecycle_generation != task_execution_intent_generation(task)
        or run.root_subscription_id != selection.root.subscription_id
        or run.root_agent_id != selection.root.agent_id
        or run.selection_fingerprint != selection.stable_fingerprint()
        or run.current_plan_id != plan.id
        or run.state_version != expected_state_version
        or run.claim_token != claim.claim_token
        or run.claim_generation != claim.wake_generation
        or run.wake_generation != claim.wake_generation
        or expiry is None or expiry <= datetime.now(timezone.utc)
        or run.phase in TaskCoordinatorPhase.terminal()
    ):
        raise ValueError("Task coordinator claim, version, or current Plan was lost")
    return task, plan, run, sorted(steps, key=lambda step: step.step_key)


def _waiting_control(run: TaskCoordinatorRun, plan_id: str) -> dict[str, Any] | None:
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    control = checkpoint.get(TASK_COORDINATOR_CONTROL_KEY)
    if control is None:
        return None
    if (
        not isinstance(control, dict)
        or control.get("version") != 1
        or control.get("plan_id") != plan_id
        or not isinstance(control.get("step_ids"), list)
        or not control["step_ids"]
        or not all(isinstance(value, str) and value for value in control["step_ids"])
        or not isinstance(control.get("tool_call_id"), str)
        or not control["tool_call_id"]
    ):
        raise ValueError("Task coordinator waiting batch checkpoint is invalid")
    return control


def _settled_tool_result(
    plan_id: str,
    step_ids: tuple[str, ...],
    by_id: dict[str, ExecutionStep],
) -> str:
    results = [
        {
            "step_id": step_id,
            "step_key": by_id[step_id].step_key,
            "status": by_id[step_id].step_status,
            "result": by_id[step_id].result,
            "error": by_id[step_id].error,
            "evidence_refs": by_id[step_id].evidence_refs,
        }
        for step_id in step_ids
    ]
    canonical = json.dumps(results, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    payload: dict[str, Any] = {
        "plan_id": plan_id, "task_coordinator_batch_digest": digest, "steps": results,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(json.dumps(payload, ensure_ascii=False)) <= 3900:
        return text

    # The shared AgentLoop compacts generic tool results beyond 4,000 chars.
    # Return a bounded, stable digest plus a useful preview ourselves so the
    # stored role=tool message remains fully checkable at ACK time. Full
    # output/evidence remains on the immutable terminal Step rows.
    summaries = []
    for item in results:
        result_json = json.dumps(item["result"], sort_keys=True, ensure_ascii=False)
        error_json = json.dumps(item["error"], sort_keys=True, ensure_ascii=False)
        evidence_json = json.dumps(item["evidence_refs"], sort_keys=True, ensure_ascii=False)
        summaries.append({
            "step_id": item["step_id"], "step_key": item["step_key"],
            "status": item["status"],
            "result_preview": result_json[:160],
            "error_preview": error_json[:80],
            "evidence_preview": evidence_json[:80],
        })
    payload = {
        "plan_id": plan_id, "task_coordinator_batch_digest": digest,
        "truncated": True, "steps": summaries,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(json.dumps(payload, ensure_ascii=False)) <= 3900:
        return text
    for item in summaries:
        item.pop("result_preview", None)
        item.pop("error_preview", None)
        item.pop("evidence_preview", None)
    return json.dumps(
        payload,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _sleep_deadline(params: dict[str, Any], now: datetime) -> datetime:
    """Freeze a relative sleep once, so repeated Plan cycles never restart it."""

    if "until" in params:
        target = params.get("until")
        if isinstance(target, str):
            target = datetime.fromisoformat(target.replace("Z", "+00:00"))
        if not isinstance(target, datetime):
            raise ValueError("sleep until must be an ISO timestamp")
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target.astimezone(timezone.utc)
    seconds = max(0.0, float(params.get("seconds") or 0))
    return now + timedelta(seconds=seconds)


async def release_ready_task_coordinator_steps(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: TaskCoordinatorClaim,
    expected_state_version: int,
    tool_call_id: str,
    max_parallel: int = 8,
) -> TaskCoordinatorStepRelease:
    """Release at most eight independent ready PAUSED Steps in one transaction.

    The caller commits this transaction before returning a suspension to the
    AgentLoop.  A crash before suspension checkpoint leaves a recoverable
    waiting control with the exact tool call and Step identities.
    """
    if not tool_call_id or not str(tool_call_id).strip():
        raise ValueError("Task coordinator release requires a runtime tool call id")
    if not 1 <= max_parallel <= 8:
        raise ValueError("Task coordinator parallel batch must be between 1 and 8")
    task, plan, run, steps = await _locked_scope(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
        expected_state_version=expected_state_version,
    )
    by_id = {step.id: step for step in steps}
    by_key = {step.step_key: step for step in steps}
    control = _waiting_control(run, plan.id)
    if control is not None:
        if control["tool_call_id"] != tool_call_id:
            raise ValueError("Task coordinator must inspect its prior child batch")
        step_ids = tuple(control["step_ids"])
        if len(step_ids) != len(set(step_ids)) or any(step_id not in by_id for step_id in step_ids):
            raise ValueError("Task coordinator waiting batch Step lineage changed")
        if any(by_id[step_id].step_status not in _TERMINAL_STEP_STATUSES for step_id in step_ids):
            return TaskCoordinatorStepRelease("waiting", plan.id, step_ids, run.state_version)
        # Reading an already-settled child and returning its durable receipt
        # cannot replay that effect. The gate belongs to a NEW child release
        # or a cross-Plan retry, not this exact result handoff.
        return TaskCoordinatorStepRelease(
            "settled", plan.id, step_ids, run.state_version,
            tool_result=_settled_tool_result(plan.id, step_ids, by_id),
        )

    if task.status != TaskStatus.IN_PROGRESS.value:
        return TaskCoordinatorStepRelease(
            "blocked", plan.id, state_version=run.state_version,
            reason=f"Task status {task.status!r} does not permit child release",
        )
    if plan.status not in {ExecutionPlanStatus.DRAFT.value, ExecutionPlanStatus.RUNNING.value}:
        return TaskCoordinatorStepRelease(
            "blocked", plan.id, state_version=run.state_version,
            reason=f"Plan status {plan.status!r} does not permit child release",
        )
    if reason := await retry_effect_block_detail(db, steps):
        return TaskCoordinatorStepRelease("blocked", plan.id, state_version=run.state_version, reason=reason)
    if any(step.step_status in {
        ExecutionStepStatus.FAILED.value,
        ExecutionStepStatus.SKIPPED.value,
        ExecutionStepStatus.CANCELLED.value,
    } for step in steps):
        return TaskCoordinatorStepRelease(
            "blocked", plan.id, state_version=run.state_version,
            reason="Plan contains a failed, skipped, or cancelled Step",
        )
    if any(step.step_status in _ACTIVE_STEP_STATUSES for step in steps):
        return TaskCoordinatorStepRelease(
            "blocked", plan.id, state_version=run.state_version,
            reason="Plan has an active Step outside the Task coordinator waiting batch",
        )
    ready: list[ExecutionStep] = []
    for step in steps:
        if step.step_status != ExecutionStepStatus.PAUSED.value:
            continue
        dependencies = step.depends_on if isinstance(step.depends_on, list) else None
        if dependencies is None or any(not isinstance(key, str) or key not in by_key for key in dependencies):
            raise ValueError(f"Task coordinator Step {step.step_key!r} has invalid dependencies")
        if all(by_key[key].step_status == ExecutionStepStatus.DONE.value for key in dependencies):
            ready.append(step)
    if not ready:
        if all(step.step_status == ExecutionStepStatus.DONE.value for step in steps):
            return TaskCoordinatorStepRelease("all_done", plan.id, state_version=run.state_version)
        return TaskCoordinatorStepRelease(
            "blocked", plan.id, state_version=run.state_version,
            reason="Plan has no ready Step; dependency graph cannot progress",
        )
    selected = ready[:max_parallel]
    step_ids = tuple(step.id for step in selected)
    from packages.core.services.artifact_delivery import ArtifactDeliveryError

    try:
        dependency_artifacts = await verified_task_coordinator_dependency_artifacts(
            db, task=task, plan=plan, steps=steps, selected=selected,
        )
    except ArtifactDeliveryError as exc:
        return TaskCoordinatorStepRelease("blocked", plan.id, state_version=run.state_version, reason=str(exc))
    dispatcher_state = dict(plan.dispatcher_state or {})
    input_digests = dict(dispatcher_state.get(TASK_COORDINATOR_STEP_INPUTS_KEY) or {})
    for step in selected:
        digest = task_coordinator_step_input_digest(task, plan, step, steps)
        if step.id in input_digests and input_digests[step.id] != digest:
            return TaskCoordinatorStepRelease(
                "blocked", plan.id, state_version=run.state_version,
                reason=f"Step {step.step_key!r} inputs changed after its first release; revise the Plan explicitly.",
            )
        if digest is not None:
            input_digests[step.id] = digest
    dispatcher_state[TASK_COORDINATOR_STEP_INPUTS_KEY] = input_digests
    dispatcher_state[TASK_COORDINATOR_DEPENDENCY_ARTIFACTS_KEY] = dependency_artifacts
    plan.dispatcher_state = dispatcher_state
    now = datetime.now(timezone.utc)
    for step in selected:
        if step.kind == "sleep":
            params = dict(step.params or {})
            params[COORDINATOR_CHILD_WAKE_AT_PARAM_KEY] = (
                _sleep_deadline(params, now).isoformat()
            )
            step.params = params
        step.step_status = ExecutionStepStatus.PENDING.value
    checkpoint = dict(run.checkpoint or {})
    checkpoint[TASK_COORDINATOR_CONTROL_KEY] = {
        "version": 1, "plan_id": plan.id,
        "step_ids": list(step_ids), "tool_call_id": tool_call_id,
    }
    run.checkpoint = checkpoint
    run.phase = TaskCoordinatorPhase.WAITING_CHILD.value
    run.state_version += 1
    wake = await stage_plan_cycle_wake(db, plan.id, reason="task_coordinator_released_steps")
    if wake is None:
        raise ValueError("Task coordinator could not stage Plan cycle wake")
    return TaskCoordinatorStepRelease(
        "released", plan.id, step_ids, run.state_version, plan_wake=wake,
    )


async def ack_settled_task_coordinator_batch(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: TaskCoordinatorClaim,
    expected_state_version: int,
) -> int:
    """Clear a batch only after strict checkpoint persisted its exact tool result.

    This does not accept a caller-supplied receipt.  It reads the durable
    checkpoint and verifies the canonical result message for the pinned tool
    call against current terminal Step rows.
    """
    task, plan, run, steps = await _locked_scope(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
        expected_state_version=expected_state_version,
    )
    if (
        task.status != TaskStatus.IN_PROGRESS.value
        or plan.status not in {ExecutionPlanStatus.DRAFT.value, ExecutionPlanStatus.RUNNING.value}
    ):
        raise ValueError("Task coordinator Task or Plan is not runnable")
    control = _waiting_control(run, plan.id)
    if control is None:
        raise ValueError("Task coordinator has no child batch to acknowledge")
    by_id = {step.id: step for step in steps}
    step_ids = tuple(control["step_ids"])
    if (
        len(step_ids) != len(set(step_ids))
        or any(step_id not in by_id or by_id[step_id].step_status not in _TERMINAL_STEP_STATUSES for step_id in step_ids)
    ):
        raise ValueError("Task coordinator child batch is not settled")
    result = _settled_tool_result(plan.id, step_ids, by_id)
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    messages = checkpoint.get("messages") if isinstance(checkpoint.get("messages"), list) else []
    def _matches_result(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, str):
            return False
        try:
            # AgentLoop re-serializes resumed tool JSON with default spacing.
            # Compare the complete typed payload, including its Step digest,
            # without letting insignificant JSON whitespace break the ACK.
            normalized = json.dumps(
                json.loads(content), sort_keys=True,
                separators=(",", ":"), ensure_ascii=False,
            )
        except (TypeError, ValueError):
            return False
        return normalized == result

    if not any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("tool_call_id") == control["tool_call_id"]
        and _matches_result(message)
        for message in messages
    ):
        raise ValueError("Task coordinator has not checkpointed the exact child tool result")
    updated = dict(checkpoint)
    updated.pop(TASK_COORDINATOR_CONTROL_KEY)
    run.checkpoint = updated
    run.phase = TaskCoordinatorPhase.RUNNING.value
    run.state_version += 1
    return run.state_version


async def legacy_blocked_child_result_is_recoverable(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: TaskCoordinatorClaim,
    expected_state_version: int,
) -> bool:
    """Identify the old replay-gate response, never treat it as an ACK receipt.

    Older workers saved a `blocked` tool result for the pinned call even after
    its child became terminal. Only that exact read-only observation may be
    removed from an in-memory resume transcript so the same call can return
    the canonical Step digest. The ordinary ACK must still verify that digest.
    """
    task, plan, run, steps = await _locked_scope(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
        expected_state_version=expected_state_version,
    )
    if (
        task.status != TaskStatus.IN_PROGRESS.value
        or plan.status not in {ExecutionPlanStatus.DRAFT.value, ExecutionPlanStatus.RUNNING.value}
        or run.phase != TaskCoordinatorPhase.WAITING_CHILD.value
    ):
        return False
    control = _waiting_control(run, plan.id)
    if control is None:
        return False
    by_id = {step.id: step for step in steps}
    step_ids = control["step_ids"]
    if (
        len(step_ids) != len(set(step_ids))
        or any(step_id not in by_id for step_id in step_ids)
        or not any(by_id[step_id].step_status == ExecutionStepStatus.FAILED.value for step_id in step_ids)
        or any(by_id[step_id].step_status not in _TERMINAL_STEP_STATUSES for step_id in step_ids)
    ):
        return False
    if (await db.execute(
        select(WorkLease.id).where(
            WorkLease.step_id.in_(step_ids),
            WorkLease.status == WorkLeaseStatus.ACTIVE.value,
        ).limit(1)
    )).scalar_one_or_none() is not None:
        return False
    reason = await retry_effect_block_detail(db, [by_id[step_id] for step_id in step_ids])
    if not reason:
        return False
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    messages = checkpoint.get("messages") if isinstance(checkpoint.get("messages"), list) else []
    call_id = control["tool_call_id"]
    if not any(
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and any(
            isinstance(call, dict)
            and call.get("id") == call_id
            and isinstance(call.get("function"), dict)
            and call["function"].get("name") == "delegate_ready_steps"
            and call["function"].get("arguments") == "{}"
            for call in message.get("tool_calls") or []
        )
        for message in messages
    ):
        return False
    matching = [
        message for message in messages
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("content"), str):
        return False
    try:
        payload = json.loads(matching[0]["content"])
    except (TypeError, ValueError):
        return False
    return payload == {"status": "blocked", "reason": reason}
