"""Durable, reviewed repair of a pre-Plan Agent capability gap.

Requirements never grant tools. A repair is an ordinary Workspace operation
review, pinned to one stopped Task generation and one Agent deployment.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from sqlalchemy import select

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.constants.task import TaskStatus
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.workspace import Agent, AgentToolBinding, ToolDefinition
from packages.core.services.task_execution import (
    resolve_task_execution,
    task_execution_intent_generation,
    task_plan_subscriptions,
)

BLOCKER_KEY = "capability_blocker"
RECOVERY_KEY = "task_capability_recovery"


class CapabilityBlockerKind(str, Enum):
    UNAVAILABLE = "capability_unavailable"
    TOOL_MISSING = "tool_missing"
    CONNECTION_REQUIRED = "connection_required"
    APPROVAL_REQUIRED = "approval_required"
    POLICY_DENIED = "policy_denied"


def current_capability_blocker(task: Task) -> dict[str, Any] | None:
    raw_details = getattr(task, "details", None)
    details = raw_details if isinstance(raw_details, dict) else {}
    blocker = details.get(BLOCKER_KEY)
    if (
        getattr(task, "status", None) != TaskStatus.BLOCKED.value
        or not isinstance(blocker, dict)
        or blocker.get("generation") != task_execution_intent_generation(task)
    ):
        return None
    return blocker


async def capability_acquisition_available_to_requester(db, *, task, execution_mode: str) -> bool:
    """Admission for the installed live worker path, never a binding grant."""
    return False


async def _tool_policy(db, *, task, tool: str):
    """Use the runtime's canonical subject even before a concrete call exists."""
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.governance import check_step_policy

    classified = classify_runtime_tool(tool, {}, entity_id=task.entity_id,
        workspace_id=task.workspace_id, task_id=task.id)
    action = classified.action
    authorization = classified.authorization
    read_only = classified.effect is RuntimeToolEffect.READ_ONLY
    return await check_step_policy(
        db, entity_id=task.entity_id, workspace_id=task.workspace_id, task_id=task.id,
        kind=action.kind if action else "action",
        action_key=authorization.action_key if authorization else "unclassified.action",
        capability_id=authorization.capability_id if authorization else None,
        resource_id=action.resource_id if action else None,
        resource_kind=action.resource_kind if action else None,
        risk_level=action.risk_level if action else "low" if read_only else "medium",
        operation=action.operation if action else "read" if read_only else None,
        impact="read" if read_only else "reversible" if classified.effect is RuntimeToolEffect.CONTROL else None,
    )


async def record_capability_blocker(db, *, task, run, missing_capabilities, affected_agents=()):
    """Called under Workspace -> Task -> Run locks, after the BLOCKED write.

Only unambiguous native capabilities with absent bindings can produce a
configuration proposal. Unknown/provider/policy failures remain honest gaps.
No model, provider call, binding write or execution dispatch occurs here.
"""
    from packages.core.services.workspace_operation_service import (
        create_operation_draft, draft_to_dict,
    )
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError, resolve_task_execution_user_id,
    )

    existing = current_capability_blocker(task)
    missing = sorted(set(missing_capabilities))
    generation = task_execution_intent_generation(task)
    fingerprint = hashlib.sha256(json.dumps(
        [generation, run.selection_fingerprint, missing], separators=(",", ":"),
    ).encode()).hexdigest()
    if existing and existing.get("fingerprint") == fingerprint:
        return existing, None

    blocker = {
        "kind": CapabilityBlockerKind.UNAVAILABLE.value,
        "stage": "planning",
        "affected_agents": list(affected_agents),
        "missing_capabilities": missing,
        "generation": generation,
        "fingerprint": fingerprint,
        "run_id": run.id,
        "selection_fingerprint": run.selection_fingerprint,
    }
    # Later awaited reads can autoflush this value. Keep the local builder
    # separate so subsequent receipt additions remain a detectable JSONB
    # assignment, rather than mutating SQLAlchemy's committed value in place.
    task.details = {**(task.details or {}), BLOCKER_KEY: dict(blocker)}
    if run.current_plan_id or not missing:
        return blocker, None
    if await db.scalar(select(ExecutionPlan.id).where(ExecutionPlan.task_id == task.id).limit(1)):
        # Plan-backed recovery belongs to its existing checkpoint/Step path.
        return blocker, None
    execution = await resolve_task_execution(db, task)
    if execution.coordinator is None or execution.coordinator.stable_fingerprint() != run.selection_fingerprint:
        return blocker, None
    root = execution.coordinator.root
    agent = await db.get(Agent, root.agent_id)
    if agent is None:
        return blocker, None
    if not blocker["affected_agents"]:
        blocker = {**blocker, "affected_agents": [{
            "service_key": root.service_key, "subscription_id": root.subscription_id,
            "agent_id": root.agent_id, "agent_name": agent.name,
        }]}
        task.details = {**task.details, BLOCKER_KEY: dict(blocker)}
    capabilities = [CORE_CAPABILITIES.get(value) for value in missing]
    if any(cap is None or not cap.tool_names for cap in capabilities):
        return blocker, None
    decisions = [await _tool_policy(db, task=task, tool=tool)
                 for cap in capabilities for tool in cap.tool_names]
    if any(not decision.allowed and not decision.pause_for_hitl for decision in decisions):
        blocker = {**blocker, "kind": CapabilityBlockerKind.POLICY_DENIED.value}
        task.details = {**task.details, BLOCKER_KEY: dict(blocker)}
        return blocker, None

    # If a binding already exists, adding it again is not a repair. Do not
    # guess whether readiness, an account, or runtime policy excluded it.
    bound_tools = set((await db.execute(
        select(ToolDefinition.name).join(
            AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id,
        ).where(AgentToolBinding.agent_id == root.agent_id)
    )).scalars().all())
    if any(set(cap.tool_names) & bound_tools for cap in capabilities):
        return blocker, None
    try:
        user_id = await resolve_task_execution_user_id(db, task)
    except TaskRequesterIdentityError:
        # A revoked requester cannot authorize a configuration proposal. Keep
        # the original durable blocker rather than failing its error handler.
        return blocker, None
    if not user_id:
        return blocker, None
    from packages.core.services.agent_effective_capabilities import AgentEffectiveCapabilityFactory

    root_subscriptions = [sub for sub in await task_plan_subscriptions(db, task)
                          if sub.id == root.subscription_id]
    snapshots = await AgentEffectiveCapabilityFactory.create(
        db, entity_id=task.entity_id, user_id=user_id, workspace_id=task.workspace_id,
        subscriptions=root_subscriptions, agents_by_id={agent.id: agent},
        task_id=task.id, conversation_id=task.conversation_id,
    )
    snapshot = snapshots.get(root.service_key)
    if snapshot is None or not snapshot.available:
        return blocker, None
    bindings = [{
        "owner_scope": "agent", "agent_id": root.agent_id,
        "agent_subscription_id": root.subscription_id,
        "capability_type": "capability", "capability_id": cap.id,
        "binding_key": f"capability:{cap.id}:subscription:{root.subscription_id}",
    } for cap in capabilities]
    draft = await create_operation_draft(
        db, task.workspace_id, task.entity_id, user_id=user_id,
        source_event_id=f"task-capability:{task.id}:{fingerprint[:32]}",
        initial_patches=[{"op": "capability_binding.upsert", "payload": {"bindings": bindings}}],
    )
    if draft is None:
        return blocker, None
    if (draft.validation or {}).get("errors"):
        # Keep the invalid proposal auditable but never publish a dead-end
        # approval card or advertise an unverified connection classification.
        from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
        draft.status = WorkspaceOperationDraftStatus.DISCARDED.value
        return blocker, None
    if not (draft.diff or {}).get("changed_keys"):
        from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
        draft.status = WorkspaceOperationDraftStatus.DISCARDED.value
        return blocker, None
    recovery = {
        "task_id": task.id, "task_title": task.title,
        "generation": generation, "fingerprint": fingerprint,
        "selection_fingerprint": run.selection_fingerprint, "run_id": run.id,
        "agent_name": agent.name,
        "tool_names": sorted({name for cap in capabilities for name in cap.tool_names}),
    }
    # This is draft-local runtime evidence, not portable operating_model data.
    # The existing review fingerprint covers all current_state, including it.
    draft.current_state = {**draft.current_state, RECOVERY_KEY: recovery}
    blocker = {
        **blocker, "kind": CapabilityBlockerKind.TOOL_MISSING.value,
        "operation_draft_id": draft.id,
    }
    task.details = {**task.details, BLOCKER_KEY: dict(blocker)}
    from packages.core.ai.runtime.workspace_operation_actions import _operation_review_payload
    from packages.core.services.hitl_requests import workspace_operation_pending_action_from_data
    from packages.core.workspace_chat.service import post_message

    payload = _operation_review_payload(
        draft_to_dict(draft),
        prompt=f"Configure {agent.name} and continue this task?",
        content={"task_id": task.id, "capabilities": [cap.name for cap in capabilities],
                 "tools": sorted({name for cap in capabilities for name in cap.tool_names})},
    )
    message = await post_message(
        db, entity_id=task.entity_id, workspace_id=task.workspace_id,
        body=f"{agent.name} needs additional tools before this task can start. Review the configuration change to continue the same task.",
        author_kind="system", message_kind="system",
        refs=[{"type": "task", "id": task.id}],
        pending_action=workspace_operation_pending_action_from_data(payload),
        meta={"hitl_requests": [payload["hitl"]]}, publish_event=False,
    )
    return blocker, message


async def validate_capability_retry(db, *, task, execution) -> None:
    """Cheap fresh preflight before retry can spend on another planning call."""
    from packages.core.services.agent_effective_capabilities import AgentEffectiveCapabilityFactory
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError, resolve_task_execution_user_id,
    )
    from packages.core.services.task_retry_service import TaskRetryError

    blocker = current_capability_blocker(task)
    if blocker is None:
        return
    if execution.coordinator is None or execution.coordinator.stable_fingerprint() != blocker["selection_fingerprint"]:
        raise TaskRetryError(409, "The Agent deployment changed. Review the task's service configuration before continuing.")
    subscriptions = await task_plan_subscriptions(db, task)
    agents = list((await db.execute(
        select(Agent).where(Agent.id.in_([sub.agent_id for sub in subscriptions]))
    )).scalars().all())
    try:
        requester_id = await resolve_task_execution_user_id(db, task)
    except TaskRequesterIdentityError as exc:
        raise TaskRetryError(409, "The task requester no longer has access. Review task access before continuing.") from exc
    snapshots = await AgentEffectiveCapabilityFactory.create(
        db, entity_id=task.entity_id, user_id=requester_id,
        workspace_id=task.workspace_id, subscriptions=subscriptions,
        agents_by_id={agent.id: agent for agent in agents}, task_id=task.id,
        conversation_id=task.conversation_id,
    )
    available = {
        capability_id for snapshot in snapshots.values() for capability_id in snapshot.capability_ids
    }
    from packages.core.ai.runtime.task_requirements import task_runtime_capabilities_from_context

    try:
        required = task_runtime_capabilities_from_context(
            (task.details or {}).get("runtime_context"), strict=True,
        )
    except ValueError as exc:
        raise TaskRetryError(409, "Review the task's invalid capability requirements before continuing.") from exc
    missing = (set(required) | set(blocker.get("missing_capabilities") or [])) - available
    # A configuration review is not authority to override a newer hard deny.
    # Read the DB policy; apply already owns its governance mutation fence.
    effective_tools = {name for snapshot in snapshots.values() for name in snapshot.tool_names}
    for capability_id in set(required) | set(blocker.get("missing_capabilities") or []):
        capability = CORE_CAPABILITIES.get(capability_id)
        tools = set(capability.tool_names) & effective_tools if capability else set()
        decisions = [await _tool_policy(db, task=task, tool=name) for name in tools]
        if decisions and all(not item.allowed and not item.pause_for_hitl for item in decisions):
            raise TaskRetryError(409, "Current workspace rules prohibit a required capability. Review the rules before continuing.")
    if missing and not await _can_retry_with_worker_acquisition(
        db, task=task, execution=execution, blocker=blocker, snapshots=snapshots,
    ):
        raise TaskRetryError(409, "This Agent still needs configuration. Resolve the capability request before continuing this task.")


async def _can_retry_with_worker_acquisition(db, *, task, execution, blocker, snapshots) -> bool:
    """An explicit retry may rediscover setup inside the original Task's workers."""
    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.models.task_plan_submission import TaskPlanSubmissionKind, TaskPlanSubmissionState
    from packages.core.models.workspace import Workspace
    from packages.core.services.task_plan_submission import latest_task_plan_submission
    from packages.core.workspaces import default_execution_mode

    if execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN:
        return False
    run = (await db.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.id == blocker.get("run_id"), TaskCoordinatorRun.task_id == task.id,
        TaskCoordinatorRun.entity_id == task.entity_id, TaskCoordinatorRun.workspace_id == task.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    selection = execution.coordinator
    if (
        run is None or run.phase != "waiting_human" or run.claim_token or run.current_plan_id
        or run.lifecycle_generation != blocker["generation"]
        or run.selection_fingerprint != selection.stable_fingerprint()
        or (run.root_subscription_id, run.root_agent_id)
        != (selection.root.subscription_id, selection.root.agent_id)
        or not any(
            scope.available and any(
                (binding.service_key, binding.subscription_id, binding.agent_id)
                == (scope.service_key, scope.subscription_id, scope.agent_id)
                for binding in selection.bindings
            ) for scope in snapshots.values()
        )
        or await db.scalar(select(ExecutionPlan.id).where(ExecutionPlan.task_id == task.id).limit(1))
    ):
        return False
    prior_submission = await latest_task_plan_submission(db, task_id=task.id)
    if (
        prior_submission is not None
        and prior_submission.request_kind == TaskPlanSubmissionKind.MANUAL.value
        and prior_submission.state == TaskPlanSubmissionState.PENDING.value
        and prior_submission.plan_id is None
    ):
        # Retry carries this exact receipt into the new generation. Its frozen
        # manual DAG still takes strict admission; it cannot acquire new Steps.
        return False
    # Planner receipts are not carried across retry generations, so their old
    # execution_mode cannot override the new generated Plan's Workspace mode.
    workspace = await db.get(Workspace, task.workspace_id, populate_existing=True)
    return bool(workspace and await capability_acquisition_available_to_requester(
        db, task=task, execution_mode=default_execution_mode(workspace),
    ))


async def recovery_task_for_operation(db, draft):
    """Validate a server-created receipt before changing configuration."""
    from packages.core.services.workspace_operation_service import OperationConflictError

    recovery = (draft.current_state or {}).get(RECOVERY_KEY)
    if not isinstance(recovery, dict):
        return None
    task = (await db.execute(select(Task).where(
        Task.id == recovery.get("task_id"), Task.entity_id == draft.entity_id,
        Task.workspace_id == draft.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    blocker = current_capability_blocker(task) if task is not None else None
    if not blocker or blocker.get("operation_draft_id") != draft.id or any(
        blocker.get(key) != recovery.get(key)
        for key in ("generation", "fingerprint", "selection_fingerprint", "run_id")
    ):
        raise OperationConflictError("This task configuration request is no longer current. Refresh the task before applying.")
    run = (await db.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.id == recovery["run_id"], TaskCoordinatorRun.task_id == task.id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    has_plan = (await db.execute(select(ExecutionPlan.id).where(
        ExecutionPlan.task_id == task.id,
    ).limit(1))).scalar_one_or_none()
    execution = await resolve_task_execution(db, task)
    if (
        run is None or run.current_plan_id or run.claim_token or has_plan
        or run.phase != "waiting_human" or execution.coordinator is None
        or execution.coordinator.stable_fingerprint() != recovery["selection_fingerprint"]
    ):
        raise OperationConflictError("The task execution changed. Its old configuration request cannot resume it.")
    return task
