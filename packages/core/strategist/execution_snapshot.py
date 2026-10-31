"""Read-only, Task-owned execution evidence shared by Strategist prompts.

Recorded failures describe an occurrence, not current tool authority. Nothing
here grants capabilities, repairs a Task, or reads an Agent checkpoint.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import TASK_AGENT_CHILD_TERMINAL_PHASES, TaskAgentChildRun
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.services.sensitive_data import sanitize_approval_credentials, sanitize_sensitive_payload
from packages.core.services.task_capability_recovery import current_capability_blocker
from packages.core.services.task_execution import (
    TASK_EXECUTION_OUTBOX_EVENT, task_execution_intent_dedupe_key, task_execution_intent_generation,
)
from packages.core.services.workspace_execution_evidence import (
    _current_pending_input, _current_task_recovery, task_execution_state,
)
from packages.core.services.workspace_work_reconciliation import is_waiting_on_human

_MAX_TASKS = 80
_BLOCKER_KINDS = {
    "capability_unavailable", "tool_missing", "connection_required", "approval_required", "policy_denied",
}


def _get(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _strings(value: Any) -> list[str]:
    return sorted({item for item in value if isinstance(item, str) and item}) if isinstance(value, (list, tuple)) else []


def _blocker_fact(task, run, service_capabilities) -> dict | None:
    blocker = current_capability_blocker(task)
    if not blocker:
        return None
    kind, stage = blocker.get("kind"), blocker.get("stage")
    if (not isinstance(kind, str) or kind not in _BLOCKER_KINDS
        or not isinstance(stage, str) or stage not in {"planning", "execution"}):
        return None
    if run is not None and (
        blocker.get("run_id") != run.id
        or blocker.get("selection_fingerprint") != run.selection_fingerprint
        or run.phase != "waiting_human"
        or (blocker.get("stage") == "planning" and run.current_plan_id)
    ):
        return None
    if run is None and blocker.get("run_id"):
        return None
    recorded = _strings(blocker.get("missing_capabilities"))
    if not recorded:
        return None
    raw_affected = blocker.get("affected_agents")
    affected = [row for row in raw_affected if isinstance(row, dict)] if isinstance(raw_affected, list) else []
    root_scopes = [(key, scope) for key, scope in service_capabilities.items() if run is not None
                   and _get(scope, "subscription_id") == run.root_subscription_id
                   and _get(scope, "agent_id") == run.root_agent_id]
    if not affected and len(root_scopes) == 1:
        affected = [{"service_key": root_scopes[0][0],
                     "subscription_id": run.root_subscription_id, "agent_id": run.root_agent_id}]
    selected_keys = {task.owner_service_key, *_strings(task.delegate_service_keys)} - {None, ""}
    # Historical team Tasks may have no explicit owner. Their coordinator's
    # pinned root still identifies a deployment; never infer one by Agent name.
    resolved = bool(affected) and (run is None or len(root_scopes) == 1)
    affected_keys = {row.get("service_key") for row in affected if isinstance(row.get("service_key"), str)}
    resolved = resolved and len(affected_keys) == len(affected)
    if selected_keys:
        resolved = resolved and affected_keys <= selected_keys
    else:
        resolved = resolved and run is not None and all(
            row.get("subscription_id") == run.root_subscription_id and row.get("agent_id") == run.root_agent_id
            for row in affected
        )
    available = set()
    for owner in affected:
        if not isinstance(owner.get("service_key"), str):
            continue
        scope = service_capabilities.get(owner.get("service_key"))
        ready = _get(scope, "available")
        if ready is None:
            ready = _get(scope, "scope_status") == "resolved" and not _get(scope, "errors")
        if (
            ready is not True or not owner.get("subscription_id") or not owner.get("agent_id")
            or _get(scope, "subscription_id") != owner["subscription_id"]
            or _get(scope, "agent_id") != owner["agent_id"]
            or (owner.get("service_key") == task.owner_service_key
                and task.owner_subscription_id and task.owner_subscription_id != owner["subscription_id"])
        ):
            resolved = False
        else:
            available.update(_strings(_get(scope, "capability_ids")))
    # Tool scope is not a live credential check, policy approval or provider
    # health check. Only missing-tool occurrences can be rechecked this way.
    resolved = resolved and kind in {"capability_unavailable", "tool_missing"}
    missing = sorted(set(recorded) - available) if resolved else None
    fact = {
        "source_task_id": task.id,
        "ref": f"capability:{task.id}:{_digest(blocker)}",
        "kind": blocker["kind"], "stage": blocker["stage"],
        "generation": blocker["generation"],
        "recorded_missing_capabilities": recorded,
        "current_missing_capabilities": missing,
        "scope_status": "unavailable" if missing is None else "still_missing" if missing else "configuration_changed",
        "affected_agents": [
            {key: owner[key] for key in ("service_key", "subscription_id", "agent_id")}
            for owner in affected
            if all(isinstance(owner.get(key), str) and 0 < len(owner[key]) <= 100
                   for key in ("service_key", "subscription_id", "agent_id"))
        ][:32],
    }
    if isinstance(blocker.get("operation_draft_id"), str):
        fact["operation_draft_id"] = blocker["operation_draft_id"]
    return fact


def _error_fact(task, log, plans_by_id) -> dict | None:
    # An earlier lifecycle's failure cannot explain this Task's current hold.
    if (log is None or task.status not in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.WAITING_ON_CUSTOMER, TaskStatus.ON_HOLD}
        or task.status_changed_at is None or _utc(log.created_at) < _utc(task.status_changed_at)):
        return None
    meta = log.meta if isinstance(log.meta, dict) else {}
    plan_id = meta.get("plan_id")
    if plan_id and (plan_id not in plans_by_id or plans_by_id[plan_id].task_id != task.id):
        return None
    message = sanitize_sensitive_payload(sanitize_approval_credentials(str(log.content or "")))
    fact = {"source_task_id": task.id, "ref": f"task_log:{log.id}", "log_id": log.id,
            "message": str(message)[:1000], "created_at": _iso(log.created_at)}
    if plan_id:
        fact["plan_id"] = plan_id
    # Coordinator failures persist the tool-error contract's machine code in
    # metadata.error; no error-message classification is needed.
    error = meta.get("error")
    code = meta.get("code") or meta.get("error_type")
    if not code:
        code = error.get("code") or error.get("type") if isinstance(error, dict) else error
    if isinstance(code, str):
        fact["code"] = str(sanitize_sensitive_payload(sanitize_approval_credentials(code)))[:100]
    if isinstance(meta.get("stop_reason"), str):
        fact["stop_reason"] = str(sanitize_sensitive_payload(sanitize_approval_credentials(meta["stop_reason"])))[:100]
    return fact


def _query(model, fields: str):
    # Column projection avoids loading checkpoint messages, tool parameters or
    # refreshing/mutating ORM identities already held by the review caller.
    return select(*(getattr(model, name).label(name) for name in fields.split()))


async def _read(db, query):
    return [SimpleNamespace(**row) for row in (await db.execute(query)).mappings()]


async def gather_execution_snapshot(db, *, workspace, tasks, plans, outcomes, service_capabilities) -> dict:
    """Read one bounded Workspace batch; database failures remain failures."""
    ids = {task.id for task in tasks}
    ids.update(plan.task_id for plan in plans if plan.task_id)
    ids.update(task.id for rows in (outcomes or {}).values() for task in rows)
    if len(ids) > _MAX_TASKS:
        raise ValueError("Strategist execution evidence exceeds its bounded Task scope")
    now = datetime.now(timezone.utc)
    with db.no_autoflush:
        rows = await _read(db, _query(Task,
            "id title status updated_at status_changed_at actual_output details entity_id workspace_id "
            "task_type agent_id agent_type owner_service_key owner_subscription_id delegate_service_keys"
        ).where(Task.id.in_(ids), Task.entity_id == workspace.entity_id, Task.workspace_id == workspace.id))
        task_ids = [task.id for task in rows]
        runs = await _read(db, _query(TaskCoordinatorRun,
            "id task_id current_plan_id phase state_version selection_fingerprint root_subscription_id root_agent_id "
            "claim_token claim_expires_at claim_generation wake_generation lifecycle_generation engine"
        ).where(TaskCoordinatorRun.task_id.in_(task_ids), TaskCoordinatorRun.entity_id == workspace.entity_id,
                TaskCoordinatorRun.workspace_id == workspace.id))
        runs_by_task = {run.task_id: run for run in runs}
        # Delegated child runs of agent-loop Tasks (open ones decide the state).
        child_rows = await _read(db, _query(TaskAgentChildRun,
            "id task_id phase claim_token claim_generation claim_expires_at wake_generation"
        ).where(TaskAgentChildRun.task_id.in_(task_ids), TaskAgentChildRun.entity_id == workspace.entity_id,
                TaskAgentChildRun.workspace_id == workspace.id))
        children_by_task: dict[str, list[Any]] = {}
        for child in child_rows:
            children_by_task.setdefault(child.task_id, []).append(child)
        plan_fields = "id task_id status dispatcher_state created_at"
        plan_scope = (ExecutionPlan.task_id.in_(task_ids), ExecutionPlan.entity_id == workspace.entity_id,
                      ExecutionPlan.workspace_id == workspace.id)
        latest_plans = await _read(db, _query(ExecutionPlan, plan_fields).where(*plan_scope)
            .distinct(ExecutionPlan.task_id).order_by(ExecutionPlan.task_id, ExecutionPlan.created_at.desc(), ExecutionPlan.id.desc()))
        latest_by_task = {plan.task_id: plan.id for plan in latest_plans}
        chosen = {}
        for task in rows:
            run = runs_by_task.get(task.id)
            output = task.actual_output if isinstance(task.actual_output, dict) else {}
            chosen[task.id] = run.current_plan_id if run is not None else output.get("plan_id") or latest_by_task.get(task.id)
        selected_plans = await _read(db, _query(ExecutionPlan, plan_fields).where(
            *plan_scope, ExecutionPlan.id.in_([value for value in chosen.values() if value])))
        plans_by_id = {plan.id: plan for plan in selected_plans}
        steps = await _read(db, _query(ExecutionStep,
            "id plan_id current_lease_id step_key step_status attempt_count result params"
        ).where(ExecutionStep.plan_id.in_(plans_by_id), ExecutionStep.entity_id == workspace.entity_id,
                ExecutionStep.workspace_id == workspace.id))
        steps_by_id = {step.id: step for step in steps}
        leases = await _read(db, _query(WorkLease,
            "id step_id plan_id status lease_until execution_claim_id execution_claimed_at heartbeat_count"
        ).where(WorkLease.id.in_([step.current_lease_id for step in steps if step.current_lease_id]),
                WorkLease.entity_id == workspace.entity_id, WorkLease.workspace_id == workspace.id))
        leases = [lease for lease in leases if lease.step_id in steps_by_id
                  and steps_by_id[lease.step_id].plan_id == lease.plan_id
                  and steps_by_id[lease.step_id].current_lease_id == lease.id]
        leases_by_id = {lease.id: lease for lease in leases}
        pending = await _read(db, _query(HitlRequest,
            "id action_key hitl_type matched_rule origin_task_id origin_plan_id origin_step_id origin_kind dedup_key context"
        ).where(HitlRequest.origin_task_id.in_(task_ids), HitlRequest.entity_id == workspace.entity_id,
                HitlRequest.workspace_id == workspace.id, HitlRequest.status == ApprovalStatus.PENDING))
        logs = await _read(db, _query(TaskLog, "id task_id content meta created_at")
            .where(TaskLog.task_id.in_(task_ids), TaskLog.log_type == TaskLogType.AI_EXECUTION_FAILED)
            .distinct(TaskLog.task_id).order_by(TaskLog.task_id, TaskLog.created_at.desc(), TaskLog.id.desc()))
        logs_by_task = {log.task_id: log for log in logs}
        intent_by_task = {task.id: task_execution_intent_dedupe_key(task.id, task_execution_intent_generation(task))
                          for task in rows if task.status == TaskStatus.IN_PROGRESS}
        intents = set((await db.scalars(select(RuntimeOutboxEvent.dedupe_key).where(
            RuntimeOutboxEvent.dedupe_key.in_(intent_by_task.values()),
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        ))).all())
    facts = []
    for task in sorted(rows, key=lambda row: row.id):
        run = runs_by_task.get(task.id)
        plan = plans_by_id.get(chosen.get(task.id))
        if plan is not None and plan.task_id != task.id:
            plan = None
        task_steps = tuple(step for step in steps if plan is not None and step.plan_id == plan.id)
        task_leases = tuple(lease for lease in leases if plan is not None and lease.plan_id == plan.id)
        pending_input = task.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.WAITING_ON_CUSTOMER, TaskStatus.ON_HOLD} and any(
            request.origin_task_id == task.id and (
                _current_task_recovery(request, task, plan, run) or _current_pending_input(
                    request, plan, steps_by_id.get(request.origin_step_id),
                    leases_by_id.get(getattr(steps_by_id.get(request.origin_step_id), "current_lease_id", None)),
                )
            ) for request in pending
        )
        task_children = children_by_task.get(task.id, [])
        state = task_execution_state(task, plan=plan, coordinator=run, steps=task_steps,
            leases=task_leases if task.status == TaskStatus.IN_PROGRESS else (), pending_input=pending_input,
            admitted=intent_by_task.get(task.id) in intents, now=now,
            children=tuple(child for child in task_children if child.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES))
        fact = {
            "task_id": task.id, "title": task.title, "task_status": task.status, "observed_state": state.value,
            "owner_service_key": task.owner_service_key, "owner_subscription_id": task.owner_subscription_id,
            "updated_at": _iso(task.updated_at), "plan_id": plan.id if plan else None,
            "plan_status": plan.status if plan else None,
            "coordinator": ({"id": run.id, "phase": run.phase, "state_version": run.state_version,
                "root_agent_id": run.root_agent_id, "root_subscription_id": run.root_subscription_id,
                "current_plan_id": run.current_plan_id, "claim_active": bool(
                    run.claim_token and run.claim_generation and run.claim_generation == run.wake_generation
                    and run.claim_expires_at and _utc(run.claim_expires_at) > now
                    and run.lifecycle_generation == task_execution_intent_generation(task)
                ), **({"children": {
                    "total": len(task_children),
                    "by_phase": dict(sorted(Counter(child.phase for child in task_children).items())),
                }} if task_children else {})} if run is not None else None),
            "step_counts": dict(sorted(Counter(step.step_status for step in task_steps).items())),
            "current_blocker": _blocker_fact(task, run, service_capabilities or {}),
            "latest_error": _error_fact(task, logs_by_task.get(task.id), plans_by_id),
            # Any open card this Task raised is already in front of a person;
            # agent work cannot answer it, only re-ask it.
            "waiting_on_human": is_waiting_on_human(task.status, has_open_human_request=any(
                request.origin_task_id == task.id for request in pending)),
        }
        fact["revision"] = _digest({
            **{key: value for key, value in fact.items() if key != "updated_at"},
            "generation": task_execution_intent_generation(task),
        })
        facts.append(fact)
    return {"status": "available", "observed_at": now.isoformat(),
            "revision": _digest([(fact["task_id"], fact["revision"]) for fact in facts]), "tasks": facts}
