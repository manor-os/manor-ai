"""Read-only Task execution facts for Workspace Chat.

Task, coordinator, Plan and lease remain their own state owners. This module
does not dispatch, reconcile, approve, repair or mark anything complete. A
model-authored summary or a Step's early RUNNING label is never start proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import load_only

from packages.core.contracts.artifact_delivery import accepted_task_artifact_manifest
from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.constants.execution import WorkLeaseStatus
from packages.core.constants.task import TaskStatus


class ExecutionObservationState(StrEnum):
    NOT_STARTED = "not_started"
    ACCEPTED = "accepted"
    ASSIGNED = "assigned"
    PLANNING = "planning"
    AWAITING_COORDINATOR = "awaiting_coordinator"
    RUNNING = "running"
    # A Task agent loop parked on its own wait (a time or media jobs).
    WAITING = "waiting"
    WAITING_INPUT = "waiting_input"
    BLOCKED = "blocked"
    DELIVERED = "delivered"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ExecutionEvidenceScope:
    entity_id: str
    workspace_id: str
    user_id: str
    conversation_id: str
    origin_user_message_id: str | None


def _future(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > now


# Task agent loop phases that park the run on a wait without a worker.
_PARKED_PHASES = frozenset({"waiting_timer", "waiting_job"})
_MAX_WAIT_JOB_IDS = 50


def task_execution_wait(phase: str | None, wait: Any) -> dict[str, Any] | None:
    """What a parked Task agent loop is waiting for, from its task_loop_wait."""

    if phase not in _PARKED_PHASES or not isinstance(wait, dict) or wait.get("kind") != phase:
        return None
    if phase == "waiting_timer":
        wake_at = str(wait.get("wake_at") or "")[:64]
        if not wake_at:
            return None
        return {"kind": phase, "wake_at": wake_at, "label": f"Waiting until {wake_at}"}
    job_ids = [str(item)[:64] for item in wait.get("job_ids") or [] if item][:_MAX_WAIT_JOB_IDS]
    if not job_ids:
        return None
    observation = {
        "kind": phase, "job_ids": job_ids,
        "label": f"Waiting for {len(job_ids)} media job(s)",
    }
    if wait.get("deadline_at"):
        observation["deadline_at"] = str(wait["deadline_at"])[:64]
    return observation


def task_execution_state(
    task: Any, *, plan: Any = None, coordinator: Any = None,
    steps: tuple[Any, ...] = (), leases: tuple[Any, ...] = (), pending_input: bool = False,
    admitted: bool = False, now: datetime | None = None,
) -> ExecutionObservationState:
    """Derive a conservative observation from existing authoritative rows."""
    now = now or datetime.now(timezone.utc)
    status = str(task.status or "")
    if status in {"cancelled", "canceled"}:
        return ExecutionObservationState.CANCELLED
    if status == "failed":
        return ExecutionObservationState.FAILED
    if status == "completed":
        actual = getattr(task, "actual_output", None) or {}
        if (
            plan is not None and plan.status == "completed"
            and actual.get("plan_id") == plan.id
            and actual.get("supervisor_verdict") == "completed"
            and accepted_task_artifact_manifest(plan, list(steps)) is not None
        ):
            return ExecutionObservationState.DELIVERED
        return ExecutionObservationState.COMPLETED_UNVERIFIED
    if pending_input:
        return ExecutionObservationState.WAITING_INPUT
    if status in {"blocked", "waiting_on_customer", "on_hold"}:
        return ExecutionObservationState.BLOCKED
    live_leases = [
        lease for lease in leases
        if lease.status == WorkLeaseStatus.ACTIVE and _future(lease.lease_until, now)
    ]
    if any(
        # External workers may receive a pre-minted claim token at dispatch.
        # A timestamp or a fenced heartbeat, not that token, proves admission.
        lease.execution_claim_id
        and (lease.execution_claimed_at is not None or (lease.heartbeat_count or 0) > 0)
        for lease in live_leases
    ):
        return ExecutionObservationState.RUNNING
    if live_leases:
        return ExecutionObservationState.ASSIGNED
    # Claim admission and checkpoint persistence do not promote READY to
    # RUNNING. A resumed coordinator can therefore be doing real work while
    # retaining READY (or the previous child-wait phase).
    from packages.core.services.task_execution import task_execution_intent_generation

    parked = coordinator is not None and coordinator.phase in _PARKED_PHASES
    coordinator_active = coordinator is not None and coordinator.phase in {
        "ready", "running", "waiting_plan", "waiting_child", *_PARKED_PHASES,
    }
    current_lifecycle = bool(
        coordinator is not None
        and coordinator.lifecycle_generation == task_execution_intent_generation(task)
    )
    if (
        status == "in_progress" and coordinator_active
        and coordinator.claim_token and coordinator.claim_generation
        and coordinator.claim_generation == coordinator.wake_generation
        and _future(coordinator.claim_expires_at, now)
        and current_lifecycle
    ):
        # A parked loop holding a live claim was woken and is working.
        return ExecutionObservationState.RUNNING if parked else ExecutionObservationState.PLANNING
    if status == "in_progress" and parked and current_lifecycle:
        return ExecutionObservationState.WAITING
    if (
        status == "in_progress" and coordinator_active and plan is not None
        and coordinator.current_plan_id == plan.id
        and any(step.step_status == "done" for step in steps)
    ):
        # Completed Steps prove past execution, not a live owner or accepted
        # Task delivery. Never call this "no execution recorded" or "reviewing".
        return ExecutionObservationState.AWAITING_COORDINATOR
    if admitted:
        return ExecutionObservationState.ACCEPTED
    return ExecutionObservationState.NOT_STARTED


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _current_pending_input(request: Any, plan: Any, step: Any, lease: Any) -> bool:
    """An old pending row is not proof that the current execution is parked."""
    if (
        plan is None or plan.status in {"completed", "failed", "cancelled"}
        or step is None or step.plan_id != plan.id
        or request.origin_plan_id != plan.id or step.step_status != "waiting_human"
    ):
        return False
    if request.origin_kind == "step":
        return not step.current_lease_id
    context = request.context if isinstance(request.context, dict) else {}
    return bool(
        request.origin_kind == "lease" and lease is not None
        and step.current_lease_id == lease.id and lease.step_id == step.id
        and lease.plan_id == plan.id and lease.status == WorkLeaseStatus.NEEDS_HUMAN
        and _future(lease.lease_until, datetime.now(timezone.utc))
        and request.dedup_key == f"lease:{lease.id}:{context.get('pending_kind') or 'human_input'}"
    )


def _current_task_recovery(request: Any, task: Any, plan: Any, coordinator: Any) -> bool:
    """Recovery is Task-owned; bind the card to its current owner's pointer."""
    return bool(
        request.origin_kind == "task" and request.origin_task_id == task.id
        and request.action_key == "task.recover" and request.hitl_type == HitlType.ERROR
        and request.matched_rule == "task.needs_recovery"
        and task.status in {TaskStatus.WAITING_ON_CUSTOMER, TaskStatus.ON_HOLD, TaskStatus.BLOCKED}
        and plan is not None and request.origin_plan_id == plan.id
        and (plan.dispatcher_state or {}).get("task_recovery_request_id") == request.id
        and coordinator is not None and coordinator.current_plan_id == plan.id
        and coordinator.phase == "waiting_human"
    )


async def read_workspace_task_evidence(
    scope: ExecutionEvidenceScope, references: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Read a bounded batch with current actor/Workspace/Conversation checks.

    Each invocation owns and closes its read transaction before another model
    round. There is no process-global receipt cache or cross-Workspace state.
    """
    from packages.core.database import async_session
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.models.user import User
    from packages.core.models.worker import WorkLease
    from packages.core.models.workspace import Workspace
    from packages.core.models.workspace_event import WorkspaceEvent
    from packages.core.ledger.event_types import EXECUTION_REQUESTED
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.task_execution import (
        TASK_EXECUTION_OUTBOX_EVENT, task_execution_intent_dedupe_key,
        task_execution_intent_generation,
    )
    from packages.core.services.workspace_access import user_can_read_workspace_id
    from packages.core.services.workspace_autonomy import workspace_allows_manual_work

    ids = sorted({ref["id"] for ref in references})
    if not ids or len(ids) > 8:
        return []
    async with async_session() as db:
        async def readable_workspace() -> bool:
            # Re-select scalar fields, not identity-map cached lifecycle state.
            row = (await db.execute(select(Workspace.status, Workspace.deleted_at).where(
                Workspace.id == scope.workspace_id, Workspace.entity_id == scope.entity_id,
            ))).one_or_none()
            actor = await db.scalar(select(User.id).where(
                User.id == scope.user_id, User.status == "active", User.deleted_at.is_(None),
            ))
            if not workspace_allows_manual_work(row) or actor is None:
                return False
            role = await resolve_effective_user_role_name(db, entity_id=scope.entity_id, user_id=scope.user_id)
            return await user_can_read_workspace_id(
                db, workspace_id=scope.workspace_id, entity_id=scope.entity_id,
                user_id=scope.user_id, role=role,
            )
        if not await readable_workspace():
            return []
        conversation = await db.scalar(select(Conversation.id).where(
            Conversation.id == scope.conversation_id,
            Conversation.entity_id == scope.entity_id,
            Conversation.workspace_id == scope.workspace_id,
            Conversation.status == "active",
        ))
        if conversation is None:
            return []
        origin_valid = bool(scope.origin_user_message_id and await db.scalar(
            select(Message.id).where(
                Message.id == scope.origin_user_message_id,
                Message.conversation_id == scope.conversation_id,
                Message.role == "user",
            )
        ))
        tasks = (await db.scalars(select(Task).options(load_only(
            Task.id, Task.title, Task.status, Task.status_changed_at, Task.actual_output,
            Task.entity_id, Task.workspace_id, Task.task_type, Task.agent_id,
            Task.agent_type, Task.owner_service_key, Task.owner_subscription_id,
            Task.delegate_service_keys,
        )).where(
            Task.id.in_(ids), Task.entity_id == scope.entity_id,
            Task.workspace_id == scope.workspace_id,
        ))).all()
        task_ids = [task.id for task in tasks]
        origins = (await db.scalars(select(WorkspaceEvent).options(load_only(
            WorkspaceEvent.source_id, WorkspaceEvent.payload,
        )).where(
            WorkspaceEvent.entity_id == scope.entity_id,
            WorkspaceEvent.workspace_id == scope.workspace_id,
            WorkspaceEvent.source_kind == "task",
            WorkspaceEvent.source_id.in_(task_ids),
            WorkspaceEvent.event_type == EXECUTION_REQUESTED,
            WorkspaceEvent.idempotency_key.in_([f"chat-task-created:{task_id}" for task_id in task_ids]),
            WorkspaceEvent.actor_kind == "user",
            WorkspaceEvent.actor_id == scope.user_id,
        ))).all()
        origin_by_task = {event.source_id: event.payload for event in origins}
        runs = (await db.scalars(select(TaskCoordinatorRun).options(load_only(
            TaskCoordinatorRun.task_id, TaskCoordinatorRun.current_plan_id,
            TaskCoordinatorRun.phase, TaskCoordinatorRun.claim_token,
            TaskCoordinatorRun.claim_expires_at, TaskCoordinatorRun.claim_generation,
            TaskCoordinatorRun.wake_generation, TaskCoordinatorRun.lifecycle_generation,
        )).where(
            TaskCoordinatorRun.task_id.in_(task_ids),
            TaskCoordinatorRun.entity_id == scope.entity_id,
            TaskCoordinatorRun.workspace_id == scope.workspace_id,
        ))).all()
        run_by_task = {run.task_id: run for run in runs}
        # Only a parked run's wait key is read, never its whole transcript.
        parked_runs = [run.task_id for run in runs if run.phase in _PARKED_PHASES]
        waits_by_task = dict((await db.execute(select(
            TaskCoordinatorRun.task_id, TaskCoordinatorRun.checkpoint["task_loop_wait"],
        ).where(
            TaskCoordinatorRun.task_id.in_(parked_runs),
            TaskCoordinatorRun.entity_id == scope.entity_id,
            TaskCoordinatorRun.workspace_id == scope.workspace_id,
        ))).all()) if parked_runs else {}
        plan_ids = {
            run.current_plan_id for run in runs if run.current_plan_id
        } | {
            (task.actual_output or {}).get("plan_id") for task in tasks
            if isinstance(task.actual_output, dict)
        }
        plan_ids.discard(None)
        plans = (await db.scalars(select(ExecutionPlan).options(load_only(
            ExecutionPlan.id, ExecutionPlan.task_id, ExecutionPlan.status,
            ExecutionPlan.dispatcher_state,
        )).where(
            ExecutionPlan.id.in_(plan_ids), ExecutionPlan.task_id.in_(task_ids),
            ExecutionPlan.entity_id == scope.entity_id,
            ExecutionPlan.workspace_id == scope.workspace_id,
        ))).all()
        plans_by_id = {plan.id: plan for plan in plans}
        steps = (await db.scalars(select(ExecutionStep).options(load_only(
            ExecutionStep.id, ExecutionStep.plan_id, ExecutionStep.current_lease_id,
            ExecutionStep.step_key, ExecutionStep.step_status, ExecutionStep.attempt_count,
            ExecutionStep.result, ExecutionStep.params,
        )).where(
            ExecutionStep.plan_id.in_(plans_by_id),
            ExecutionStep.entity_id == scope.entity_id,
            ExecutionStep.workspace_id == scope.workspace_id,
        ))).all()
        step_by_id = {step.id: step for step in steps}
        steps_by_plan: dict[str, list[Any]] = {}
        for step in steps:
            steps_by_plan.setdefault(step.plan_id, []).append(step)
        lease_ids = [step.current_lease_id for step in steps if step.current_lease_id]
        leases = (await db.scalars(select(WorkLease).options(load_only(
            WorkLease.id, WorkLease.step_id, WorkLease.plan_id, WorkLease.status,
            WorkLease.lease_until, WorkLease.execution_claim_id,
            WorkLease.execution_claimed_at, WorkLease.heartbeat_count,
        )).where(
            WorkLease.id.in_(lease_ids), WorkLease.entity_id == scope.entity_id,
            WorkLease.workspace_id == scope.workspace_id,
        ))).all()
        leases_by_plan: dict[str, list[Any]] = {}
        for lease in leases:
            step = step_by_id.get(lease.step_id)
            if step is not None and step.plan_id == lease.plan_id and step.current_lease_id == lease.id:
                leases_by_plan.setdefault(lease.plan_id, []).append(lease)
        leases_by_id = {lease.id: lease for lease in leases}
        pending = (await db.scalars(select(HitlRequest).options(load_only(
            HitlRequest.id, HitlRequest.action_key, HitlRequest.hitl_type, HitlRequest.matched_rule,
            HitlRequest.origin_task_id, HitlRequest.origin_plan_id, HitlRequest.origin_step_id,
            HitlRequest.origin_kind, HitlRequest.dedup_key, HitlRequest.context,
        )).where(
            HitlRequest.origin_task_id.in_(task_ids),
            HitlRequest.entity_id == scope.entity_id,
            HitlRequest.workspace_id == scope.workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING,
        ))).all()
        dedupe_by_task = {
            task.id: task_execution_intent_dedupe_key(task.id, task_execution_intent_generation(task))
            for task in tasks if task.status == TaskStatus.IN_PROGRESS
        }
        intents = set((await db.scalars(select(RuntimeOutboxEvent.dedupe_key).where(
            RuntimeOutboxEvent.dedupe_key.in_(dedupe_by_task.values()),
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        ))).all())
        facts = []
        for task in tasks:
            # Mutable Task.details are useful context, not provenance proof.
            origin = origin_by_task.get(task.id) or {}
            same_turn = origin_valid and origin == {
                "kind": "workspace_chat_create", "entity_id": scope.entity_id,
                "workspace_id": scope.workspace_id, "user_id": scope.user_id,
                "conversation_id": scope.conversation_id,
                "origin_user_message_id": scope.origin_user_message_id,
            }
            run = run_by_task.get(task.id)
            plan_id = (
                run.current_plan_id if run is not None
                else (task.actual_output or {}).get("plan_id")
            )
            plan = plans_by_id.get(plan_id)
            task_leases = tuple(leases_by_plan.get(plan_id, []))
            state = task_execution_state(
                task, plan=plan, coordinator=run, leases=task_leases,
                steps=tuple(steps_by_plan.get(plan_id, [])),
                pending_input=any(
                    request.origin_task_id == task.id
                    and (
                        _current_task_recovery(request, task, plan, run)
                        or _current_pending_input(
                            request, plan, step_by_id.get(request.origin_step_id),
                            leases_by_id.get(getattr(step_by_id.get(request.origin_step_id), "current_lease_id", None)),
                        )
                    ) for request in pending
                ),
                admitted=dedupe_by_task.get(task.id) in intents,
            )
            fact = {
                "kind": "task", "id": task.id, "title": task.title,
                "state": state.value, "url": f"/tasks/{task.id}",
                "origin": "current_turn" if same_turn else "existing_record",
            }
            if state is ExecutionObservationState.WAITING:
                waiting = task_execution_wait(run.phase, waits_by_task.get(task.id))
                if waiting is not None:
                    fact["waiting"] = waiting
            # Heartbeat refreshes do not create different facts every second.
            # Expiry and current ownership are rechecked at every validation.
            fact["revision"] = _digest({
                **fact, "entity_id": scope.entity_id,
                "workspace_id": scope.workspace_id,
                "conversation_id": scope.conversation_id,
                "origin_user_message_id": scope.origin_user_message_id,
                "task_generation": task.status_changed_at,
                "plan_id": plan_id, "plan_status": getattr(plan, "status", None),
                "acceptance": (getattr(plan, "dispatcher_state", None) or {}).get("task_content_acceptance"),
                "coordinator": (
                    (run.id, run.phase, run.claim_token, run.claim_generation, run.wake_generation, run.lifecycle_generation)
                    if run is not None else None
                ),
                "leases": sorted((lease.id, lease.execution_claim_id, str(lease.execution_claimed_at), lease.status) for lease in task_leases),
            })
            facts.append(fact)
        # Recheck current access before returning the snapshot; no lease or
        # lifecycle lock is held across provider work.
        if not await readable_workspace():
            return []
        return facts


async def read_current_turn_task_references(scope: ExecutionEvidenceScope) -> list[dict[str, str]]:
    """Tasks this Workspace Chat turn created, from the host's own ledger event.

    Each Workspace Chat Task creation records one ``chat-task-created`` event
    caused by the originating user message (workspace_task_actions). The model
    never names these: a plain-text reply cannot add or drop a Task here.
    """
    if not scope.origin_user_message_id:
        return []
    from packages.core.database import async_session
    from packages.core.ledger.event_types import EXECUTION_REQUESTED
    from packages.core.models.workspace_event import WorkspaceEvent

    async with async_session() as db:
        rows = (await db.execute(select(WorkspaceEvent.source_id, WorkspaceEvent.payload).where(
            WorkspaceEvent.entity_id == scope.entity_id,
            WorkspaceEvent.workspace_id == scope.workspace_id,
            WorkspaceEvent.event_type == EXECUTION_REQUESTED,
            WorkspaceEvent.source_kind == "task",
            WorkspaceEvent.causation_id == scope.origin_user_message_id,
            WorkspaceEvent.actor_kind == "user",
            WorkspaceEvent.actor_id == scope.user_id,
            WorkspaceEvent.idempotency_key.like("chat-task-created:%"),
        ).order_by(WorkspaceEvent.occurred_at, WorkspaceEvent.id).limit(8))).all()
    references = []
    for task_id, payload in rows:
        if not isinstance(payload, dict) or payload.get("conversation_id") != scope.conversation_id:
            continue
        references.append({"kind": "task", "id": str(task_id), "scope": "current_turn"})
    return references
