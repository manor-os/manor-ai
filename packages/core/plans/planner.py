"""LLM-driven Planner.

Inputs: a Task (already created with owner_service_key + delegates) +
its workspace context (active subscriptions, available providers /
actions, relevant memories).

Output: a Pydantic ``Plan`` validated against the canonical schema.

Flow:
  1. Resolve workspace context — subscriptions, allowed actions per
     subscription, recent relevant memory.
  2. Build the system + user prompt.
  3. Bounded model/tool loop returning a structured Plan candidate.
  4. Normalize the candidate, bind the Task's exact output contract, and
     validate its schema, references, and service/action allowlists.
  5. Return a rejected candidate's diagnostics in the same conversation so
     the Planner can correct it before any acceptance or persistence.
  6. Persist via ``plans.service.create_plan_from_dag``.

If the LLM is unreachable / no API key, falls back to a tiny
rule-based stub so Demo A v0 still demonstrates end-to-end execution
during dev / CI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    RuntimeResolvedBillingScope,
    approval_args_hash,
    approval_stable_target_hash,
    approval_stable_target_payload,
    runtime_apply_action_binding_schemas_to_steps,
    runtime_capability_id_for_action_key,
    runtime_execute_planner_chat_turn,
    runtime_execute_planner_tool_call,
    runtime_current_billing_context,
    runtime_planner_assistant_message,
    runtime_planner_action_binding_for,
    runtime_planner_llm_billing_context,
    runtime_planner_capability_catalog,
    runtime_planner_system_prompt,
    runtime_planner_task_prompt,
    runtime_planner_tool_schemas,
    runtime_planner_tool_message,
    runtime_planner_user_message,
)
from packages.core.ai.llm_client import (
    CreditCheckUnavailableError,
    CreditExhaustedError,
)
from packages.core.ai.runtime.task_requirements import (
    task_runtime_capabilities_from_context,
)
from packages.core.ai.runtime.planning import runtime_planner_validation_feedback
from packages.core.services.agent_effective_capabilities import (
    AgentEffectiveCapabilityFactory,
    EffectiveAgentCapabilities,
    _filter_service_provider_actions_to_runtime_scope as _filter_service_provider_actions_to_runtime_scope,
)
from packages.core.constants.task import TaskRuntimeKind, TaskStatus
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.dispatchability import plan_dispatch_preflight
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.refs import extract_step_refs
from packages.core.plans.service import (
    PlanContractError,
    bind_task_output_contract,
    create_plan_from_dag,
    plan_contract_gaps,
    raise_for_active_task_plan,
)
from packages.core.services.integration_account_service import (
    IntegrationAccountSelectionMode,
)
from packages.core.services.official_remote_mcp import MCPActionEffect
from packages.core.services.task_execution_origin import (
    record_manual_task_execution,
    task_has_manual_execution,
)
from packages.core.services.workspace_autonomy import workspace_allows_manual_work
from packages.core.services.workflow_run_execution_claim import (
    commit_fenced_execution_boundary,
    task_plan_execution_claim,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim


PLANNER_VERSION = "v0.1-demo-a"

#: Opt-in for the rule-based planner stub on a first-turn provider failure.
#: Unset (production): the outage surfaces as ``PlannerError`` and the Celery
#: task retries with backoff instead of running a placeholder plan.
PLANNER_FALLBACK_STUB_ENV = "MANOR_PLANNER_FALLBACK_STUB"
_PLANNER_FALLBACK_STUB_TRUTHY = frozenset({"1", "true", "yes"})


def _planner_fallback_stub_enabled() -> bool:
    """Whether ``MANOR_PLANNER_FALLBACK_STUB`` opts into the dev/CI stub."""
    value = str(os.environ.get(PLANNER_FALLBACK_STUB_ENV, "") or "").strip().lower()
    return value in _PLANNER_FALLBACK_STUB_TRUTHY


class PlannerError(Exception):
    """Planner couldn't produce a valid plan after retries."""


class TaskPlanAdmissionError(PlannerError):
    """The Task lifecycle does not allow background planning."""

    def __init__(self, task_id: str, status: str | None, reason: str) -> None:
        self.task_id = task_id
        self.status = status
        self.reason = reason
        super().__init__(reason)


class TaskPlanOwnedByCoordinatorError(TaskPlanAdmissionError):
    """A queued V2 Plan start lost ownership to the durable Task loop."""


class CapabilityError(Exception):
    """Planner produced a plan referencing capabilities not in the
    workspace's allowlists. Should never happen if the prompt was
    followed; we re-validate as a safety net."""


class TaskCapabilityGapError(CapabilityError):
    """Selected Task services cannot meet its persisted runtime requirements."""

    def __init__(
        self,
        task_id: str,
        missing_capabilities: tuple[str, ...],
        affected_agents: tuple[dict[str, str | None], ...] = (),
    ) -> None:
        self.task_id = task_id
        self.missing_capabilities = missing_capabilities
        self.affected_agents = affected_agents
        super().__init__(
            "Task required capabilities unavailable to its selected Agent services: "
            + ", ".join(missing_capabilities)
            + ". Bind the necessary tool or integration to a selected Agent "
            "and retry; Task requirements do not grant tools."
        )


class TaskPlanClaimHeldError(RuntimeError):
    """Another live caller already owns billable planning for this Task."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} is already being planned")


class WorkspaceAutonomyPausedError(PlannerError):
    """Automatic planning is deferred until Workspace automatic running resumes."""


def task_plan_admission_error(task: Task) -> str | None:
    """Return lifecycle-only admission errors before factory resolution."""
    status = getattr(task, "status", None)
    if status != TaskStatus.IN_PROGRESS.value:
        return (
            f"task status {status!r} cannot start a new Plan; "
            f"expected {TaskStatus.IN_PROGRESS.value!r}"
        )
    return None


async def _require_coordinated_plan_execution(
    db: AsyncSession,
    task: Task,
    *,
    expected_selection=None,
    lock_subscriptions: bool = False,
):
    """Resolve the factory and require coordinator for Workspace Plan work."""

    from packages.core.services.task_execution import resolve_task_execution

    execution = await resolve_task_execution(
        db,
        task,
        lock_subscriptions=lock_subscriptions,
    )
    if not task.workspace_id:
        # Entity-level legacy Tasks retain their established Plan/Agent
        # compatibility path. Coordinator folding is a Workspace invariant.
        return execution
    if expected_selection is not None:
        expected_selection.require_current(execution.coordinator)
    if (
        execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
        or execution.coordinator is None
    ):
        raise TaskPlanAdmissionError(
            task.id,
            str(task.status) if task.status is not None else None,
            (
                f"task runtime {execution.runtime_kind.value!r} cannot start "
                "a new Plan"
            ),
        )
    return execution


@dataclass(frozen=True)
class _TaskPlanningScope:
    entity_id: str
    workspace_id: str | None


async def _lock_task_planning_origin(
    db: AsyncSession,
    task_id: str,
    *,
    manual_user_id: str | None = None,
    task_coordinator_claim: TaskCoordinatorClaim | None = None,
) -> tuple[Task, _TaskPlanningScope]:
    """Lock Workspace -> Task and validate one planning admission snapshot."""
    task_scope = (await db.execute(
        select(Task.id, Task.entity_id, Task.workspace_id).where(Task.id == task_id)
    )).one_or_none()
    if task_scope is None:
        raise PlannerError(f"task {task_id} not found")

    scope = _TaskPlanningScope(
        entity_id=str(task_scope.entity_id),
        workspace_id=(
            str(task_scope.workspace_id) if task_scope.workspace_id else None
        ),
    )
    workspace = None
    if scope.workspace_id:
        from packages.core.services.workspace_access import (
            lock_workspace_access_boundary,
        )

        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=scope.workspace_id,
            entity_id=scope.entity_id,
        )
        if not workspace_allows_manual_work(workspace):
            raise PlannerError("Workspace is unavailable for planning")

    task = (await db.execute(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or (
        str(task.entity_id) != scope.entity_id
        or (str(task.workspace_id) if task.workspace_id else None)
        != scope.workspace_id
    ):
        raise PlannerError("Task scope changed during planning admission")
    if admission_error := task_plan_admission_error(task):
        raise TaskPlanAdmissionError(
            task_id,
            str(task.status) if task.status is not None else None,
            admission_error,
        )
    if (
        workspace is not None and workspace.status == "paused"
        and not manual_user_id and not task_has_manual_execution(task)
    ):
        raise WorkspaceAutonomyPausedError("Workspace automatic running is paused")
    if scope.workspace_id and task_coordinator_claim is None:
        # All NEW Workspace Plans belong to V3, even before its Run exists.
        # Existing V2 Plans resume their saved Steps; they do not re-enter this
        # new-plan provider path through a legacy API or queue message.
        raise TaskPlanOwnedByCoordinatorError(
            task_id, str(task.status),
            "Workspace Task planning requires its Task coordinator; submit a planning request instead",
        )
    await raise_for_active_task_plan(
        db,
        task_id=str(task.id),
        entity_id=str(task.entity_id),
    )
    return task, scope


async def _admit_task_without_holding_locks(
    db: AsyncSession,
    task_id: str,
    *,
    manual_user_id: str | None = None,
    task_coordinator_claim: TaskCoordinatorClaim | None = None,
) -> _TaskPlanningScope:
    """Validate admission under row locks, then release them before LLM I/O.

    PostgreSQL releases row locks acquired after a savepoint when that
    savepoint is rolled back. Lightweight test doubles do not implement nested
    transactions, so they keep the old single-session behavior.
    """
    begin_nested = getattr(db, "begin_nested", None)
    if not callable(begin_nested):
        _task, scope = await _lock_task_planning_origin(
            db, task_id, manual_user_id=manual_user_id,
            task_coordinator_claim=task_coordinator_claim,
        )
        return scope

    savepoint = await begin_nested()
    try:
        _task, scope = await _lock_task_planning_origin(
            db, task_id, manual_user_id=manual_user_id,
            task_coordinator_claim=task_coordinator_claim,
        )
        return scope
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


def _planning_task_values(task: Task) -> dict[str, Any]:
    """Copy the canonical persisted Task input set used by planning guards."""
    return {
        column.key: deepcopy(getattr(task, column.key, None))
        for column in Task.__table__.columns
    }


def _planning_task_snapshot(task: Task, *, details: dict | None = None) -> Any:
    """Detach the provider prompt from the live ORM row.

    Contract-gap guidance must be visible to a second Planner attempt, but it
    must not dirty or autoflush the Task while generation runs without locks.
    """
    values = _planning_task_values(task)
    if details is not None:
        values["details"] = deepcopy(details)
    return SimpleNamespace(**values)


def _planning_task_fingerprint(task: Task) -> str:
    """Fingerprint every persisted Task input that can shape a Plan.

    Planning intentionally releases lifecycle locks during provider I/O.  A
    full Task-column fingerprint lets the final locked section reject a result
    generated from stale title, contracts, ownership, or governance details
    instead of trying to guess which fields matter to every current and future
    prompt/context builder.
    """
    payload = json.dumps(
        _planning_task_values(task),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _assert_workspace_setup_allows_planning(
    db: AsyncSession,
    context,
    task: Task | None = None,
) -> None:
    context_workspace = getattr(context, "workspace", None)
    if context_workspace is None:
        return

    from packages.core.services.workspace_readiness import (
        evaluate_current_workspace_blocking_setup,
        task_is_authorized_workspace_setup,
    )

    setup_status = await evaluate_current_workspace_blocking_setup(
        db,
        workspace_id=str(context_workspace.id),
        entity_id=(
            str(context_workspace.entity_id)
            if getattr(context_workspace, "entity_id", None)
            else None
        ),
    )
    if setup_status is not None and setup_status.blocks_work:
        if (
            task is not None
            and task.workspace_id == context_workspace.id
            and task.entity_id == context_workspace.entity_id
            and await task_is_authorized_workspace_setup(db, task)
        ):
            return
        raise PlannerError(
            "Workspace setup is incomplete; automated setup must run through "
            "the Blueprint's authorized setup job before Tasks may be planned"
        )


async def _raise_if_not_dispatchable(db: AsyncSession, *, plan: Plan, task: Any) -> None:
    """Reject a plan the dispatcher could never lease (Section P2)."""
    errors = await plan_dispatch_preflight(db, plan=plan, task=task)
    if errors:
        raise CapabilityError("plan is not dispatchable: " + "; ".join(errors))


async def admit_manual_task_plan(
    db: AsyncSession,
    *,
    task_id: str,
    submission_id: str,
    task_coordinator_claim: TaskCoordinatorClaim,
) -> ExecutionPlan:
    """Persist a frozen V3 manual DAG through the Planner's final authority gates.

    No provider call occurs. Workspace -> Task -> subscription -> Run locks stay
    held until the caller commits the Plan and consumed submission together.
    """
    from packages.core.services.task_execution import task_execution_intent_generation
    from packages.core.services.task_plan_submission import pending_task_plan_submission
    from packages.core.models.task_plan_submission import (
        TaskPlanSubmission,
        TaskPlanSubmissionKind,
    )

    requested = await db.get(TaskPlanSubmission, submission_id)
    if (
        requested is None
        or requested.task_id != task_id
        or requested.request_kind != TaskPlanSubmissionKind.MANUAL.value
    ):
        raise TaskPlanAdmissionError(task_id, None, "Manual Plan submission is unavailable")

    task, _scope = await _lock_task_planning_origin(
        db,
        task_id,
        manual_user_id=requested.requested_by_user_id,
        task_coordinator_claim=task_coordinator_claim,
    )
    submission = await pending_task_plan_submission(db, task=task)
    if (
        submission is None
        or submission.id != submission_id
        or submission.lifecycle_generation != task_execution_intent_generation(task)
    ):
        raise TaskPlanAdmissionError(
            task_id, str(task.status), "Manual Plan submission is no longer current",
        )
    candidate = Plan.model_validate(submission.plan_dag)
    execution = await _require_coordinated_plan_execution(
        db, task, lock_subscriptions=True,
    )
    context = await _gather_context(db, task)
    await _assert_workspace_setup_allows_planning(db, context, task)
    _require_task_capabilities(task, context)
    candidate = _apply_replan_approval_constraints(task, candidate)
    candidate = bind_task_output_contract(candidate, task.expected_output)
    _enforce_planner_acceptance_contract(candidate, task)
    _enforce_allowlists(candidate, context)
    await _raise_if_not_dispatchable(db, plan=candidate, task=task)
    required_step_errors = _required_plan_step_errors(task, candidate)
    if required_step_errors:
        raise PlannerError(
            "required plan steps were not satisfied: " + "; ".join(required_step_errors)
        )
    return await create_plan_from_dag(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        agent_subscription_id=execution.coordinator.root.subscription_id,
        plan=candidate,
        execution_mode=submission.execution_mode,
        approval_required=submission.approval_required,
        enforce_contract=True,
        expected_selection=execution.coordinator,
        task_coordinator_claim=task_coordinator_claim,
    )


# ── Public entry point ────────────────────────────────────────────────

async def plan_task(
    db: AsyncSession,
    task_id: str,
    *,
    execution_mode: Optional[str] = None,
    manual_user_id: str | None = None,
    before_provider: (
        Callable[[], Awaitable[RuntimeResolvedBillingScope | None]] | None
    ) = None,
    task_coordinator_claim: TaskCoordinatorClaim | None = None,
) -> ExecutionPlan:
    """Generate + persist a Plan for ``task_id``. Raises if the task
    doesn't exist or the Planner can't produce a valid plan.

    ``execution_mode`` defaults to whatever the task's workspace asks
    for — sandbox workspaces get ``sandbox`` automatically, regular
    workspaces get ``live``. Callers can still force a specific mode
    (eg. UI "Run as dry-run preview" button)."""
    requested_execution_mode = execution_mode
    task_scope = await _admit_task_without_holding_locks(
        db, task_id, manual_user_id=manual_user_id,
        task_coordinator_claim=task_coordinator_claim,
    )

    # Reload after rolling back the admission savepoint: ORM state loaded in a
    # rolled-back savepoint may be expired, and no live row should be handed to
    # provider code while another transaction is free to update it.
    task = (await db.execute(
        select(Task)
        .where(Task.id == task_id, Task.entity_id == task_scope.entity_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or (
        (str(task.workspace_id) if task.workspace_id else None)
        != task_scope.workspace_id
    ):
        raise PlannerError("Task scope changed before planning")
    if admission_error := task_plan_admission_error(task):
        raise TaskPlanAdmissionError(
            task_id,
            str(task.status) if task.status is not None else None,
            admission_error,
        )
    planning_task_fingerprint = _planning_task_fingerprint(task)
    context = await _gather_context(db, task)
    await _assert_workspace_setup_allows_planning(db, context, task)
    context_selection = None
    if task.workspace_id:
        from packages.core.services.task_execution import (
            CoordinatorRuntimeSelection,
        )

        context_selection = CoordinatorRuntimeSelection.from_subscriptions(
            context.subscriptions
        )
    # Freeze the exact root/service deployment identity that will shape this
    # provider turn. Subscription locks are short-lived and released by the
    # commit immediately before provider I/O.
    planning_execution = await _require_coordinated_plan_execution(
        db,
        task,
        lock_subscriptions=True,
    )
    planning_selection = planning_execution.coordinator
    if task.workspace_id:
        if planning_selection is not None:
            planning_selection.require_current(context_selection)
        context.coordinator = planning_selection
    planning_task = _planning_task_snapshot(task)

    if execution_mode is None:
        from packages.core.workspaces import default_execution_mode
        execution_mode = (
            default_execution_mode(context.workspace)
            if context.workspace else "live"
        )
    await _prepare_task_capability_requirements(
        db, task=task, ctx=context, execution_mode=execution_mode,
        claim=task_coordinator_claim,
    )

    billing_scope = await before_provider() if before_provider is not None else None
    if billing_scope is None:
        current_billing = runtime_current_billing_context()
        if current_billing is not None and str(
            getattr(current_billing, "entity_id", "")
        ) == str(planning_task.entity_id):
            billing_scope = RuntimeResolvedBillingScope(
                entity_id=str(planning_task.entity_id),
                workspace_id=(
                    str(planning_task.workspace_id)
                    if planning_task.workspace_id
                    else None
                ),
                user_id=getattr(current_billing, "user_id", None),
                byok=bool(getattr(current_billing, "byok", False)),
            )
    # Commit read state plus any billing preflight before provider I/O. Manor
    # sessions use expire_on_commit=False, so the immutable prompt snapshot and
    # loaded context remain usable without reopening this transaction.
    await db.commit()

    async with runtime_planner_llm_billing_context(
        entity_id=planning_task.entity_id,
        workspace_id=planning_task.workspace_id,
        billing_scope=billing_scope,
    ):
        plan = await _generate_plan(planning_task, context)
    plan = _apply_replan_approval_constraints(planning_task, plan)
    plan = bind_task_output_contract(
        plan,
        getattr(planning_task, "expected_output", None),
    )
    _enforce_allowlists(plan, context)
    await _raise_if_not_dispatchable(db, plan=plan, task=planning_task)

    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=getattr(planning_task, "expected_output", None),
        require_explicit_agent_outputs=True,
        require_explicit_execution_contract=True,
    )
    required_step_errors = _required_plan_step_errors(planning_task, plan)
    generated_replan_updates: dict[str, Any] | None = None
    if gaps or required_step_errors:
        # Re-plan once, feeding the gaps back to the Planner via task.details
        # (the prompt dumps Details JSON, so _replan_context reaches the LLM).
        # MERGE into any existing _replan_context — a runtime replan
        # (executor._maybe_replan) may have populated prior_plan_id /
        # succeeded_steps / failed_steps that lineage (_replan_parent_plan_id)
        # and the minimal-replan guidance depend on; don't clobber them.
        existing_ctx = (planning_task.details or {}).get("_replan_context")
        replan_ctx = dict(existing_ctx) if isinstance(existing_ctx, dict) else {}
        generated_replan_updates = {}
        if "reason" not in replan_ctx:
            generated_replan_updates["reason"] = (
                "contract_gaps" if gaps else "required_plan_steps"
            )
        if gaps:
            generated_replan_updates["contract_gaps"] = "; ".join(
                f"{g.step_key}: {g.detail}" for g in gaps
            )
        if required_step_errors:
            generated_replan_updates["required_plan_step_errors"] = (
                required_step_errors
            )
        replan_ctx.update(generated_replan_updates)
        planning_task = _planning_task_snapshot(
            task,
            details={
                **(planning_task.details or {}),
                "_replan_context": replan_ctx,
            },
        )
        # The first candidate's dispatchability checks opened a read
        # transaction. Release that connection before a second provider turn;
        # only detached Task/context snapshots cross provider I/O.
        await db.commit()
        async with runtime_planner_llm_billing_context(
            entity_id=planning_task.entity_id,
            workspace_id=planning_task.workspace_id,
            billing_scope=billing_scope,
        ):
            plan = await _generate_plan(planning_task, context)
        plan = _apply_replan_approval_constraints(planning_task, plan)
        plan = bind_task_output_contract(
            plan,
            getattr(planning_task, "expected_output", None),
        )
        _enforce_allowlists(plan, context)
        await _raise_if_not_dispatchable(db, plan=plan, task=planning_task)
        gaps = plan_contract_gaps(
            plan.topo_order(),
            task_expected_output=getattr(planning_task, "expected_output", None),
            require_explicit_agent_outputs=True,
            require_explicit_execution_contract=True,
        )
        required_step_errors = _required_plan_step_errors(planning_task, plan)
        if gaps or required_step_errors:
            if required_step_errors and not gaps:
                raise PlannerError(
                    "required plan steps were not satisfied: "
                    + "; ".join(required_step_errors)
                )
            raise PlanContractError(gaps)

    # Provider work runs without lifecycle locks. Reacquire Workspace -> Task,
    # then rebuild every mutable authorization/contract input immediately
    # before persistence. No provider I/O occurs in this final critical section.
    task, final_scope = await _lock_task_planning_origin(
        db, task_id, manual_user_id=manual_user_id,
        task_coordinator_claim=task_coordinator_claim,
    )
    if final_scope != task_scope:
        raise PlannerError("Task scope changed while planning")
    if _planning_task_fingerprint(task) != planning_task_fingerprint:
        raise PlannerError("Task planning inputs changed while planning; retry planning")
    if generated_replan_updates is not None:
        latest_details = task.details if isinstance(task.details, dict) else {}
        latest_context = latest_details.get("_replan_context")
        merged_context = (
            dict(latest_context) if isinstance(latest_context, dict) else {}
        )
        # Only merge the contract diagnostics generated by this Planner turn.
        # Never copy the stale prompt snapshot's approval or Plan-lineage
        # fields back over the freshly locked Task.
        merged_context.update(generated_replan_updates)
        task.details = {
            **latest_details,
            "_replan_context": merged_context,
        }

    await _require_coordinated_plan_execution(
        db,
        task,
        expected_selection=planning_selection,
        lock_subscriptions=True,
    )
    final_context = await _gather_context(db, task)
    if task.workspace_id:
        final_context.coordinator = planning_selection
    await _assert_workspace_setup_allows_planning(db, final_context, task)
    if requested_execution_mode is None:
        from packages.core.workspaces import default_execution_mode
        execution_mode = (
            default_execution_mode(final_context.workspace)
            if final_context.workspace else "live"
        )
    await _prepare_task_capability_requirements(
        db, task=task, ctx=final_context, execution_mode=execution_mode,
        claim=task_coordinator_claim,
    )
    plan = _apply_replan_approval_constraints(task, plan)
    plan = bind_task_output_contract(plan, getattr(task, "expected_output", None))
    _enforce_allowlists(plan, final_context)
    await _raise_if_not_dispatchable(db, plan=plan, task=task)
    final_gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=getattr(task, "expected_output", None),
        require_explicit_agent_outputs=True,
        require_explicit_execution_contract=True,
    )
    final_required_step_errors = _required_plan_step_errors(task, plan)
    if final_gaps or final_required_step_errors:
        if final_required_step_errors and not final_gaps:
            raise PlannerError(
                "required plan steps changed while planning: "
                + "; ".join(final_required_step_errors)
            )
        raise PlanContractError(final_gaps)

    plan_agent_subscription_id = task.owner_subscription_id
    if task.workspace_id:
        plan_agent_subscription_id = (
            planning_selection.root.subscription_id
        )

    if manual_user_id:
        record_manual_task_execution(task, user_id=manual_user_id, action="plan_task")
        await db.flush()

    return await create_plan_from_dag(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        agent_subscription_id=plan_agent_subscription_id,
        plan=plan,
        planner_version=PLANNER_VERSION,
        parent_plan_id=_replan_parent_plan_id(task),
        execution_mode=execution_mode,
        enforce_contract=True,
        expected_selection=planning_selection,
        task_coordinator_claim=task_coordinator_claim,
    )


async def plan_task_and_commit(
    db: AsyncSession,
    task_id: str,
    *,
    execution_mode: Optional[str] = None,
    manual_user_id: str | None = None,
    before_provider: (
        Callable[[], Awaitable[RuntimeResolvedBillingScope | None]] | None
    ) = None,
    before_commit: Callable[[ExecutionPlan], Awaitable[None]] | None = None,
) -> ExecutionPlan:
    """Single-flight one billable planning turn through its durable commit.

    The renewable claim prevents API and worker entrypoints from running the
    provider concurrently. The final claim-token fence and Plan commit share
    the caller's transaction, so an expired owner cannot persist after a
    successor acquires the Task planning lease.
    """

    async with task_plan_execution_claim(task_id) as claim:
        if not claim:
            raise TaskPlanClaimHeldError(task_id)
        plan = await plan_task(
            db,
            task_id,
            execution_mode=execution_mode,
            manual_user_id=manual_user_id,
            before_provider=before_provider,
        )
        if before_commit is not None:
            await before_commit(plan)
        await commit_fenced_execution_boundary(
            db.commit,
            execution_claim=claim,
            session=db,
            after_commit=claim.mark_terminal_committed,
        )
        return plan


def _replan_parent_plan_id(task: Task) -> str | None:
    """Preserve the failed-plan lineage when Planner is called for a replan."""
    details = task.details if isinstance(task.details, dict) else {}
    context = details.get("_replan_context")
    if not isinstance(context, dict):
        return None
    prior_plan_id = context.get("prior_plan_id")
    if isinstance(prior_plan_id, str) and prior_plan_id.strip():
        return prior_plan_id.strip()
    return None


def _apply_replan_approval_constraints(task: Task, plan: Plan) -> Plan:
    """Keep a human decision attached to the same replacement operation.

    Planner-authored ``requires_approval`` is intentionally normalized away:
    policy, not an LLM, owns approval rules.  A constraint written after a
    human denied an operation or requested changes is different — it is the
    user's decision boundary. If the replacement plan keeps the same step key,
    or keeps the same concrete subject, integration, and stable target, it must
    return for approval/review rather than running because revised copy changed
    the full parameter payload and the planner omitted the flag.

    A genuinely different alternative (different key and subject) is not
    force-gated here; normal Workspace policy still applies to it.
    """

    details = task.details if isinstance(task.details, dict) else {}
    context = details.get("_replan_context")
    if not isinstance(context, dict):
        return plan
    raw_constraints = context.get("approval_constraints")
    if not isinstance(raw_constraints, list) or not raw_constraints:
        return plan

    constraints = [row for row in raw_constraints if isinstance(row, dict)]
    if not constraints:
        return plan

    changed = False
    steps: list[PlanStep] = []
    for step in plan.steps:
        matched = False
        for constraint in constraints:
            same_key = bool(
                constraint.get("step_key")
                and str(constraint["step_key"]) == step.key
            )
            constraint_action = constraint.get("action_key")
            constraint_capability = constraint.get("capability_id")
            if constraint_action:
                same_subject = constraint_action == step.action_key
            elif constraint_capability:
                same_subject = constraint_capability == step.capability_id
            else:
                same_subject = constraint.get("kind") == step.kind
            same_provider = not constraint.get("provider") or (
                constraint.get("provider") == step.provider
            )
            same_integration = not constraint.get("integration_id") or (
                constraint.get("integration_id") == step.integration_id
            )
            target_fingerprint = constraint.get("target_fingerprint")
            if target_fingerprint:
                same_target = (
                    target_fingerprint
                    == approval_stable_target_hash(step.params or {})
                )
                if not same_target:
                    # Replacement Plans are inspected before their refs are
                    # resolved. If a routing/target field is dynamic, the
                    # planner cannot prove that it differs from the operation
                    # the user denied or sent back for revision. Keep it gated
                    # until execution has concrete arguments. Refs confined to
                    # mutable review content (prompt/body/caption/etc.) were
                    # removed by approval_stable_target_payload and do not
                    # trigger this fail-closed path.
                    same_target = bool(extract_step_refs(
                        approval_stable_target_payload(step.params or {})
                    ))
            else:
                # Compatibility for replan contexts written before the stable
                # target fingerprint was introduced.
                same_target = bool(
                    constraint.get("params_fingerprint")
                    and constraint.get("params_fingerprint")
                    == approval_args_hash(step.params or {})
                )
            if same_key or (
                same_subject and same_provider and same_integration and same_target
            ):
                matched = True
                break
        if matched and not step.requires_approval:
            step = step.model_copy(update={"requires_approval": True})
            changed = True
        steps.append(step)

    if not changed:
        return plan
    return Plan(steps=steps, metadata=plan.metadata)


# ── Context gathering ─────────────────────────────────────────────────

class _Context:
    """All the workspace state the Planner is allowed to draw on."""

    def __init__(
        self,
        workspace: Optional[Workspace],
        subscriptions: list[AgentSubscription],
        agents_by_id: dict[str, Agent],
        allowed_service_keys: set[str],
        provider_actions: dict[str, list[str]],
        provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
        service_provider_actions: dict[str, dict[str, list[str]]] | None = None,
        service_provider_action_specs: (
            dict[str, dict[str, dict[str, dict[str, Any]]]] | None
        ) = None,
        document_groups: list[dict] | None = None,
        staff: list[dict] | None = None,
        agent_tool_names: dict[str, list[str]] | None = None,
        agent_skill_names: dict[str, list[dict]] | None = None,
        service_tool_names: dict[str, list[str]] | None = None,
        service_mcp_tool_names: dict[str, list[str]] | None = None,
        coordinator: Any | None = None,
        service_capabilities: dict[str, EffectiveAgentCapabilities] | None = None,
    ):
        self.workspace = workspace
        self.subscriptions = subscriptions
        self.agents_by_id = agents_by_id
        self.allowed_service_keys = allowed_service_keys
        self.provider_actions = provider_actions
        self.provider_action_specs = provider_action_specs or {}
        self.service_provider_actions = service_provider_actions or {}
        self.service_provider_action_specs = service_provider_action_specs or {}
        self.document_groups = document_groups or []
        self.staff = staff or []
        self.agent_tool_names = agent_tool_names or {}
        self.service_tool_names = service_tool_names
        self.service_mcp_tool_names = service_mcp_tool_names or {}
        self.coordinator = coordinator
        self.agent_skill_names = agent_skill_names or {}
        self.service_capabilities = service_capabilities
        self.capability_acquisition: dict[str, Any] = {}
        self.capability_acquisition_targets: dict[str, dict[str, str]] = {}


def _capability_assignment_targets(ctx: _Context) -> dict[str, dict[str, str]]:
    """Exact selected worker deployments, never a configuration catalog."""
    targets = {}
    for service_key in ctx.allowed_service_keys:
        subscriptions = [sub for sub in ctx.subscriptions if sub.service_key == service_key]
        scope = (ctx.service_capabilities or {}).get(service_key)
        if len(subscriptions) != 1 or scope is None or not scope.available:
            continue
        sub = subscriptions[0]
        agent = ctx.agents_by_id.get(sub.agent_id)
        if (
            sub.status != "active" or agent is None or agent.status != "active"
            or agent.deleted_at is not None
            or (scope.subscription_id, scope.agent_id) != (sub.id, sub.agent_id)
        ):
            continue
        targets[service_key] = {
            "service_key": service_key, "subscription_id": sub.id,
            "agent_id": sub.agent_id,
        }
    return targets


async def _can_defer_task_capability_gap(
    db: AsyncSession, *, task: Task, ctx: _Context,
    execution_mode: str, claim: TaskCoordinatorClaim | None,
) -> bool:
    # OSS and execution origins without the installed worker acquisition path
    # retain the strict pre-Plan requirement boundary.
    return False


async def _prepare_task_capability_requirements(
    db: AsyncSession, *, task: Task, ctx: _Context,
    execution_mode: str, claim: TaskCoordinatorClaim | None,
) -> None:
    """Describe a recoverable gap without granting tools or inventing Steps."""
    if hasattr(ctx, "capability_acquisition"):
        ctx.capability_acquisition = {}
        ctx.capability_acquisition_targets = {}
    gap = None
    try:
        _require_task_capabilities(task, ctx)
    except TaskCapabilityGapError as exc:
        if exc.__cause__ is not None:
            raise
        gap = exc
    if await _can_defer_task_capability_gap(
        db, task=task, ctx=ctx, execution_mode=execution_mode, claim=claim,
    ):
        ctx.capability_acquisition_targets = _capability_assignment_targets(ctx)
    elif gap is not None:
        raise gap


def _enforce_capability_assignments(plan: Plan, ctx: _Context) -> None:
    assigned: set[str] = set()
    targets = getattr(ctx, "capability_acquisition_targets", {})
    for step in plan.steps:
        if "required_capabilities" not in step.params:
            continue
        values = step.params["required_capabilities"]
        if (
            not isinstance(values, list) or not 1 <= len(values) <= 24
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise CapabilityError(f"step {step.key}: required_capabilities must be a list of 1 to 24 capability IDs")
        try:
            required = task_runtime_capabilities_from_context(
                {"required_capabilities": values}, strict=True,
            )
        except ValueError as exc:
            raise CapabilityError(f"step {step.key}: {exc}") from exc
        if (
            step.kind != "subagent" or step.params.get("_coordinator") is True
            or step.service_key not in targets
            or targets.get(step.service_key) != _capability_assignment_targets(ctx).get(step.service_key)
        ):
            raise CapabilityError(f"step {step.key}: capability requirements need one current ordinary subagent deployment")
        assigned.update(required)
    observation = getattr(ctx, "capability_acquisition", {})
    missing = set(observation.get("missing_capabilities") or []) - assigned
    if missing:
        raise CapabilityError(
            "Missing Task capabilities must be assigned through subagent params.required_capabilities: "
            + ", ".join(sorted(missing))
        )


def _require_task_capabilities(task: Task, ctx: _Context) -> None:
    """Check requirements against selected services, never as a tool grant.

    The Planner's union catalog is useful for discovery but not authority for
    an individual service. Use only each selected subscription's effective
    scheduled-run tool surface and its own action/Skill bindings.
    """
    details = getattr(task, "details", None)
    runtime_context = details.get("runtime_context") if isinstance(details, dict) else None
    try:
        required = task_runtime_capabilities_from_context(
            runtime_context, strict=True,
        )
    except ValueError as exc:
        raise TaskCapabilityGapError(str(task.id), (f"invalid requirement: {exc}",)) from exc
    if not required:
        return

    available: set[str] = set()
    service_tools = ctx.service_tool_names or {}
    from packages.core.services.agent_permission_service import parse_mcp_tool_name

    for sub in ctx.subscriptions:
        service_key = str(getattr(sub, "service_key", "") or "")
        if service_key not in ctx.allowed_service_keys:
            continue
        if ctx.service_capabilities is not None:
            scope = ctx.service_capabilities.get(service_key)
            if scope is not None:
                available.update(scope.capability_ids)
            continue
        tools = service_tools.get(service_key, ())
        skills = (
            ctx.agent_skill_names.get(str(getattr(sub, "agent_id", "") or ""), ())
            if "invoke_skill" in tools else ()
        )
        provider_actions = {
            provider: list(actions)
            for provider, actions in ctx.service_provider_actions.get(service_key, {}).items()
        }
        # Deployment bindings are part of the Agent's effective scope too.
        # The provider-action catalog alone only reads reusable Agent bindings
        # and must not misclassify a valid Workspace deployment as a gap.
        for name in ctx.service_mcp_tool_names.get(service_key, ()):
            if parsed := parse_mcp_tool_name(name):
                provider, action = parsed
                provider_actions.setdefault(provider, []).append(action)
        available.update(
            entry.capability_id
            for entry in runtime_planner_capability_catalog(
                provider_actions=provider_actions,
                provider_action_specs=ctx.service_provider_action_specs.get(service_key, {}),
                platform_tools=tools,
                skills=skills,
            )
        )

    missing = tuple(capability_id for capability_id in required if capability_id not in available)
    if missing:
        raise TaskCapabilityGapError(
            str(task.id), missing,
            affected_agents=tuple({
                "service_key": sub.service_key,
                "subscription_id": sub.id,
                "agent_id": sub.agent_id,
                "agent_name": str(getattr(ctx.agents_by_id.get(sub.agent_id), "name", "") or ""),
            } for sub in ctx.subscriptions if sub.service_key in ctx.allowed_service_keys),
        )


async def _gather_context(db: AsyncSession, task: Task) -> _Context:
    workspace: Optional[Workspace] = None
    if task.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == task.workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()

    from packages.core.services.task_execution import task_plan_subscriptions

    subs = await task_plan_subscriptions(db, task)
    allowed_service_keys = {s.service_key for s in subs if s.service_key}
    seen_services: set[str] = set()
    for sub in subs:
        if sub.service_key in seen_services:
            raise CapabilityError(f"service_key={sub.service_key!r} has ambiguous active subscriptions")
        seen_services.add(sub.service_key)

    agent_ids = [s.agent_id for s in subs if s.agent_id]
    agents_by_id: dict[str, Agent] = {}
    if agent_ids:
        agent_rows = list((await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                or_(Agent.entity_id == task.entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all())
        agents_by_id = {a.id: a for a in agent_rows}

    # Provider/action map — derived from agent_mcp_bindings + mcp_servers.
    # Keep both a per-service scope for routing correctness and a union for
    # backward-compatible planner prompt summaries.
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError,
        resolve_task_execution_user_id,
    )

    try:
        actor_user_id = await resolve_task_execution_user_id(db, task)
    except TaskRequesterIdentityError:
        logger.info(
            "Planner task %s has no executable user; account MCP catalogs are unavailable",
            task.id,
        )
        actor_user_id = None

    # Workspace-scoped documents + staff — so Planner knows what's available
    doc_groups: list[dict] = []
    staff_list: list[dict] = []
    if task.workspace_id:
        from packages.core.models.document import DocumentGroup
        from packages.core.models.workspace import WorkspaceStaff
        doc_rows = list((await db.execute(
            select(DocumentGroup).where(DocumentGroup.workspace_id == task.workspace_id)
        )).scalars().all())
        doc_groups = [{"id": d.id, "name": d.name} for d in doc_rows]

        staff_rows = list((await db.execute(
            select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == task.workspace_id)
        )).scalars().all())
        staff_list = [{"staff_id": s.staff_id, "role": s.role} for s in staff_rows]

    service_capabilities = await AgentEffectiveCapabilityFactory.create(
        db, entity_id=task.entity_id, user_id=actor_user_id,
        workspace_id=task.workspace_id, subscriptions=subs,
        agents_by_id=agents_by_id, task_id=task.id,
        conversation_id=task.conversation_id,
    )
    service_tool_names = {
        key: list(scope.tool_names) for key, scope in service_capabilities.items()
    }
    service_mcp_tool_names = {
        key: list(scope.mcp_tool_names) for key, scope in service_capabilities.items()
    }
    service_provider_actions = {
        key: scope.provider_actions for key, scope in service_capabilities.items()
        if scope.provider_actions
    }
    service_provider_action_specs = {
        key: scope.provider_action_specs for key, scope in service_capabilities.items()
        if scope.provider_actions
    }
    agent_tool_names: dict[str, list[str]] = {}
    agent_skill_names: dict[str, list[dict]] = {}
    for scope in service_capabilities.values():
        if scope.agent_id:
            agent_tool_names[scope.agent_id] = sorted(
                set(agent_tool_names.get(scope.agent_id, ())) | set(scope.tool_names)
            )
            if scope.skills:
                agent_skill_names[scope.agent_id] = list(scope.skills)
    provider_actions = _union_provider_actions(service_provider_actions)
    provider_action_specs = _union_provider_action_specs(service_provider_action_specs)

    return _Context(
        workspace=workspace,
        subscriptions=subs,
        agents_by_id=agents_by_id,
        allowed_service_keys=allowed_service_keys,
        provider_actions=provider_actions,
        provider_action_specs=provider_action_specs,
        service_provider_actions=service_provider_actions,
        service_provider_action_specs=service_provider_action_specs,
        document_groups=doc_groups,
        staff=staff_list,
        agent_tool_names=agent_tool_names,
        service_tool_names=service_tool_names,
        service_mcp_tool_names=service_mcp_tool_names,
        agent_skill_names=agent_skill_names,
        service_capabilities=service_capabilities,
    )


def _union_provider_actions(
    service_provider_actions: dict[str, dict[str, list[str]]],
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for provider_actions in service_provider_actions.values():
        for provider, actions in provider_actions.items():
            bucket = out.setdefault(provider, [])
            for action in actions:
                if action not in bucket:
                    bucket.append(action)
    return out


def _union_provider_action_specs(
    service_provider_action_specs: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_specs in service_provider_action_specs.values():
        for provider, action_specs in provider_specs.items():
            bucket = out.setdefault(provider, {})
            for action, spec in action_specs.items():
                bucket.setdefault(action, spec)
    return out


# ── LLM call + validation ─────────────────────────────────────────────

async def _generate_plan(task: Task, ctx: _Context) -> Plan:
    """Multi-turn agent loop — the Planner selects Agent delegation contracts
    before committing to a plan. Up to MAX_PLANNER_TURNS turns.

    Available tools:
      get_agent_schema(service_key) — introduction and delegation contract
      get_task_context(section, field) — retrieve only needed inputs/resources
      list_tools/get_tool_schema — exact bindings for typed action steps only
      submit_plan(plan_json)    — finalize and validate the plan DAG

    score_goals is a fixed persist-only plan. Do not spend LLM turns on it.
    """
    if _is_goal_scoring_task(task):
        logger.info("Planner: skipping LLM for score_goals; using deterministic persist plan")
        return _build_validated_goal_scoring_plan(task, ctx)

    MAX_PLANNER_TURNS = 8

    system_prompt = runtime_planner_system_prompt(
        subscriptions=ctx.subscriptions,
        agents_by_id=ctx.agents_by_id,
        allowed_service_keys=ctx.allowed_service_keys,
        provider_actions=ctx.provider_actions,
        provider_action_specs=ctx.provider_action_specs,
        document_groups=ctx.document_groups,
        staff=ctx.staff,
        agent_tool_names=ctx.agent_tool_names,
        service_tool_names=ctx.service_tool_names,
        agent_skill_names=ctx.agent_skill_names,
    )
    user_prompt = runtime_planner_task_prompt(task)
    if getattr(ctx, "capability_acquisition", None):
        user_prompt += "\n\nCurrent capability readiness:\n" + json.dumps(
            ctx.capability_acquisition, ensure_ascii=False,
        )
    tools = runtime_planner_tool_schemas()

    messages: list[dict[str, Any]] = [runtime_planner_user_message(user_prompt)]

    submitted_plan: Optional[Plan] = None

    def parse_candidate(text: str) -> Plan:
        plan = Plan.model_validate_json(_strip_code_fence(text))
        plan = _normalize_plan_for_task(task, plan)
        plan = _apply_replan_approval_constraints(task, plan)
        plan = bind_task_output_contract(plan, getattr(task, "expected_output", None))
        _enforce_allowlists(plan, ctx)
        gaps = plan_contract_gaps(
            plan.topo_order(),
            task_expected_output=getattr(task, "expected_output", None),
            require_explicit_agent_outputs=True,
            require_explicit_execution_contract=True,
        )
        if gaps:
            raise PlanContractError(gaps)
        required_step_errors = _required_plan_step_errors(task, plan)
        if required_step_errors:
            raise PlannerError("required plan steps were not satisfied: " + "; ".join(required_step_errors))
        return plan

    for turn in range(MAX_PLANNER_TURNS):
        try:
            response = await runtime_execute_planner_chat_turn(
                messages=messages,
                tools=tools,
                system_prompt=system_prompt,
                entity_id=getattr(task, "entity_id", None),
                workspace_id=getattr(task, "workspace_id", None),
            )
        except (CreditExhaustedError, CreditCheckUnavailableError):
            raise
        except Exception as exc:
            logger.warning("Planner LLM call failed on turn %d: %s", turn, exc)
            if turn == 0:
                if _planner_fallback_stub_enabled():
                    logger.warning(
                        "Planner: LLM unavailable, using fallback stub (%s is set)",
                        PLANNER_FALLBACK_STUB_ENV,
                    )
                    return _fallback_plan(task, ctx)
                # A provider outage is transient: let the Celery task retry
                # with backoff rather than persisting a placeholder plan.
                raise PlannerError(f"Planner LLM unavailable: {exc}") from exc
            break

        # Handle tool calls
        if response.tool_calls:
            messages.append(runtime_planner_assistant_message(response))
            for tc in response.tool_calls:
                tool_name = tc.get("name") or (tc.get("function") or {}).get("name", "")
                from packages.core.ai.runtime.tool_error_contract import model_tool_error_result, TOOL_ERROR_MAX_CHARS
                from packages.core.ai.runtime.tool_input_validation import runtime_tool_input_validation_result

                raw_args = tc["arguments"] if "arguments" in tc else (tc.get("function") or {}).get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {"_raw": raw_args}
                else:
                    args = raw_args
                # Validate the argument envelope without guessing missing data.
                # Nested Plan validation remains with parse_candidate, which
                # also reads historical JSON-string Plan payloads.
                input_error = runtime_tool_input_validation_result(
                    tool_name=tool_name, arguments=args, tool_schema={"type": "object"},
                )

                result = input_error if input_error is not None else runtime_execute_planner_tool_call(
                    tool_name,
                    args,
                    context=ctx,
                    task=task,
                    parse_plan=parse_candidate,
                )

                # Check if submit_plan returned a valid plan
                if tool_name == "submit_plan" and isinstance(result, dict) and result.get("_plan"):
                    submitted_plan = result["_plan"]
                    result_text = result.get("message", "Plan accepted.")
                else:
                    result_text = model_tool_error_result(tool_name, result, max_chars=TOOL_ERROR_MAX_CHARS)
                    if result_text is None:
                        result_text = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)

                messages.append(runtime_planner_tool_message(
                    result_text,
                    tool_call_id=tc.get("id"),
                ))

            if submitted_plan:
                return submitted_plan
            continue

        # No tool calls — try to parse as direct plan JSON (fallback for simple tasks)
        if response.content:
            try:
                return parse_candidate(response.content)
            except (ValidationError, ValueError, PlanContractError, PlannerError, CapabilityError) as exc:
                from packages.core.ai.runtime.tool_error_contract import model_tool_error_result, TOOL_ERROR_MAX_CHARS

                feedback = model_tool_error_result("submit_plan", runtime_planner_validation_feedback(
                    exc, _strip_code_fence(response.content),
                ), max_chars=TOOL_ERROR_MAX_CHARS)
            # Keep the rejected candidate and its exact diagnostic together so
            # the next model turn can repair it within this bounded loop.
            messages.append(runtime_planner_assistant_message(response))
            messages.append(runtime_planner_user_message(
                feedback + "\nPlease correct the plan and submit it with submit_plan."
            ))
            continue

        break

    raise PlannerError(f"Planner failed to produce a valid plan after {MAX_PLANNER_TURNS} turns")


_TEXT_REPORT_DELIVERABLE_TERMS = (
    "structured text report",
    "text report",
    "plain text report",
    "internal memo",
    "plain text",
    "text-only",
    "text only",
    "文字报告",
    "纯文本",
)
_EXPLICIT_SAVED_ARTIFACT_TERMS = (
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md",
    "mp4", "video file", "video artifact",
    "saved file", "file link", "file path", "report file", "text file",
    "document file", "markdown file", "as a file", "download", "attachment",
    "save as", "export as",
    "报告文件", "文件链接", "文件路径", "附件", "下载", "保存为", "导出为",
)


def _normalize_plan_for_task(task: Task, plan: Plan) -> Plan:
    """Normalize planner overreach before materializing executable steps."""
    plan = _normalize_required_skill_steps(task, plan)
    plan = _normalize_goal_scoring_plan(task, plan)
    plan = _normalize_internal_agent_high_risk_steps(plan)
    plan = _normalize_planner_hard_approval_steps(plan)
    if _task_requests_text_report_only(task):
        depended_on = {dep for step in plan.steps for dep in step.depends_on}
        removable_keys = {
            step.key
            for step in plan.steps
            if step.key not in depended_on
            and _is_unrequested_text_report_file_write_step(step)
        }
        if removable_keys and len(removable_keys) < len(plan.steps):
            metadata = plan.metadata.model_dump()
            contract = metadata.get("acceptance_contract")
            if isinstance(contract, dict):
                for criterion in contract.get("criteria") or []:
                    if isinstance(criterion, dict):
                        criterion["evidence_step_keys"] = [
                            key for key in criterion.get("evidence_step_keys") or []
                            if key not in removable_keys
                        ]
                if any(
                    not criterion.get("evidence_step_keys")
                    for criterion in contract.get("criteria") or []
                    if isinstance(criterion, dict)
                ):
                    metadata["acceptance_contract"] = None
            plan = Plan.model_validate({
                "steps": [
                    step.model_dump()
                    for step in plan.steps
                    if step.key not in removable_keys
                ],
                "metadata": metadata,
            })
            try:
                plan.metadata.normalized_removed_steps = sorted(removable_keys)
                plan.metadata.normalization_reason = "unrequested_text_report_file_write"
            except Exception:
                pass

    plan = _normalize_planner_acceptance_contract(plan, task)
    _enforce_planner_acceptance_contract(plan, task)
    return plan


def _is_goal_scoring_task(task: Task) -> bool:
    """True for Strategist-injected Goal measurement persistence."""
    details = getattr(task, "details", None) or {}
    if not isinstance(details, dict):
        return False
    return str(details.get("strategist_task_key") or "").strip() == "score_goals"


def _build_validated_goal_scoring_plan(task: Task, ctx: _Context) -> Plan:
    """Build the persist-only score_goals plan without an LLM candidate."""
    owner_service_key = str(getattr(task, "owner_service_key", "") or "").strip()
    prompt = str(getattr(task, "description", "") or "").strip()
    if not owner_service_key or not prompt:
        raise PlannerError("score_goals requires owner_service_key and description")
    seed = Plan(
        steps=[
            PlanStep(
                key="goal_measurements",
                kind="subagent",
                service_key=owner_service_key,
                output_shape="TextResult",
                execution_contract={"delivery": "inline", "effects": "authorized"},
                params={"prompt": prompt},
                description="Persist each Goal current_value via update_goal_value.",
                risk_level="low",
                requires_approval=False,
            )
        ]
    )
    plan = _normalize_goal_scoring_plan(task, seed)
    plan = _apply_replan_approval_constraints(task, plan)
    plan = bind_task_output_contract(plan, getattr(task, "expected_output", None))
    _enforce_allowlists(plan, ctx)
    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=getattr(task, "expected_output", None),
        require_explicit_agent_outputs=True,
        require_explicit_execution_contract=True,
    )
    if gaps:
        raise PlanContractError(gaps)
    required_step_errors = _required_plan_step_errors(task, plan)
    if required_step_errors:
        raise PlannerError(
            "required plan steps were not satisfied: " + "; ".join(required_step_errors)
        )
    return plan


def _normalize_goal_scoring_plan(task: Task, plan: Plan) -> Plan:
    """Keep score_goals as one persist step; inventory/skill plans never write Goal rows."""
    if not _is_goal_scoring_task(task):
        return plan
    owner_service_key = str(getattr(task, "owner_service_key", "") or "").strip()
    prompt = str(getattr(task, "description", "") or "").strip()
    if not owner_service_key or not prompt:
        return plan

    current = list(plan.steps)
    current_prompt = str((current[0].params or {}).get("prompt") or "").strip() if current else ""
    already_ok = (
        len(current) == 1
        and current[0].kind in {"llm", "subagent"}
        and str(current[0].service_key or "").strip() == owner_service_key
        and not (current[0].params or {}).get("skill_refs")
        and current_prompt == prompt
        and current[0].output_shape == "TextResult"
        and current[0].execution_contract is not None
        and current[0].execution_contract.model_dump(mode="json") == {
            "delivery": "inline", "effects": "authorized",
        }
    )
    if already_ok:
        return plan

    step = PlanStep(
        key="goal_measurements",
        kind="subagent",
        service_key=owner_service_key,
        output_shape="TextResult",
        execution_contract={"delivery": "inline", "effects": "authorized"},
        params={"prompt": prompt},
        description="Persist each Goal current_value via update_goal_value.",
        risk_level="low",
        requires_approval=False,
    )
    metadata = plan.metadata.model_dump()
    metadata["acceptance_contract"] = None
    metadata["normalization_reason"] = "goal_scoring_persist_only"
    metadata["normalized_removed_steps"] = [item.key for item in current]
    return Plan(steps=[step], metadata=metadata)


def _normalize_required_skill_steps(task: Task, plan: Plan) -> Plan:
    """Bind required owner-service Skills to executable subagent steps."""
    if _is_goal_scoring_task(task):
        return plan
    required_skills = [
        str(value).strip()
        for value in (getattr(task, "required_skills", None) or [])
        if str(value or "").strip()
    ]
    owner_service_key = str(
        getattr(task, "owner_service_key", "") or ""
    ).strip()
    if not required_skills or not owner_service_key:
        return plan

    file_producer_keys = {
        step.key for step in plan.steps
        if step.service_key == owner_service_key
        and step.kind in {"llm", "subagent"}
        and "files" in (step.expects or [])
    }
    file_only = bool(file_producer_keys) and any(
        item.get("kind") == "file" for item in _task_acceptance_deliverables(task)
    )
    changed: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if (
            step.service_key == owner_service_key
            and step.kind in {"llm", "subagent"}
        ):
            params = deepcopy(step.params)
            if file_only and step.key not in file_producer_keys:
                remaining = [
                    ref for ref in (params.get("skill_refs") or [])
                    if ref not in required_skills
                ]
                if remaining:
                    params["skill_refs"] = remaining
                else:
                    params.pop("skill_refs", None)
                steps.append(step.model_copy(update={"params": params}))
                continue
            params["skill_refs"] = list(dict.fromkeys([
                *list(params.get("skill_refs") or []),
                *required_skills,
            ]))
            steps.append(step.model_copy(update={
                "kind": "subagent",
                "params": params,
            }))
            changed.append(step.key)
        else:
            steps.append(step)
    if not changed:
        return plan
    normalized = Plan(steps=steps, metadata=plan.metadata)
    normalized.metadata.normalized_required_skill_steps = changed
    return normalized


def _task_acceptance_deliverables(task: Task) -> list[dict[str, Any]]:
    expected_output = getattr(task, "expected_output", None)
    if not isinstance(expected_output, dict):
        return []
    return [
        item for item in (expected_output.get("deliverables") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _acceptance_evidence_step_key(
    deliverable_name: str,
    steps: list[PlanStep],
) -> str:
    """Choose the narrowest step whose authored intent names a deliverable."""
    needle = deliverable_name.strip().lower().replace("-", "_").replace(" ", "_")
    for step in reversed(steps):
        searchable = " ".join((
            step.key,
            str(step.description or ""),
            str(step.params.get("prompt") or ""),
        )).lower().replace("-", "_").replace(" ", "_")
        if needle and needle in searchable:
            return step.key
    return steps[-1].key


def _fallback_acceptance_contract(task: Task, plan: Plan) -> dict[str, Any] | None:
    deliverables = _task_acceptance_deliverables(task)
    if not deliverables:
        return None
    criteria = []
    used_keys: set[str] = set()
    for index, deliverable in enumerate(deliverables, 1):
        name = str(deliverable["name"]).strip()
        base_key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or f"deliverable_{index}"
        key = f"{base_key}_accepted"
        if key in used_keys:
            key = f"{key}_{index}"
        used_keys.add(key)
        criteria.append({
            "key": key,
            "deliverable_name": name,
            "description": str(
                deliverable.get("acceptance")
                or f"The {name} deliverable is produced."
            ).strip(),
            "evidence_step_keys": [
                _acceptance_evidence_step_key(name, plan.steps)
            ],
        })
    return {
        "expected_result": str(
            getattr(task, "description", None)
            or getattr(task, "title", None)
            or "The task deliverables are produced."
        ).strip(),
        "criteria": criteria,
    }


def _collapse_duplicate_acceptance_criteria(
    criteria: list[Any],
) -> list[Any]:
    """Keep the first criterion for each named deliverable."""
    seen: set[str] = set()
    collapsed: list[Any] = []
    for item in criteria:
        if not isinstance(item, dict):
            collapsed.append(item)
            continue
        name = str(item.get("deliverable_name") or "").strip()
        if name:
            if name in seen:
                continue
            seen.add(name)
        collapsed.append(item)
    return collapsed


def _normalize_planner_acceptance_contract(task_plan: Plan, task: Task) -> Plan:
    contract = task_plan.metadata.acceptance_contract
    contract_payload = (
        contract.model_dump() if contract is not None
        else _fallback_acceptance_contract(task, task_plan)
    )
    if contract_payload is None:
        return task_plan
    raw_criteria = contract_payload.get("criteria")
    if isinstance(raw_criteria, list):
        contract_payload["criteria"] = _collapse_duplicate_acceptance_criteria(
            raw_criteria
        )
    expected_output = getattr(task, "expected_output", None)
    contract_payload["task_expected_output"] = (
        deepcopy(expected_output) if isinstance(expected_output, dict) else None
    )
    payload = task_plan.model_dump()
    payload.setdefault("metadata", {})["acceptance_contract"] = contract_payload
    return Plan.model_validate(payload)


def _enforce_planner_acceptance_contract(plan: Plan, task: Task) -> None:
    """Require each named Task deliverable to have one acceptance criterion."""
    deliverable_names = [
        str(item["name"]).strip() for item in _task_acceptance_deliverables(task)
    ]
    if not deliverable_names:
        return
    contract = plan.metadata.acceptance_contract
    if contract is None:
        raise PlannerError("plan is missing the required acceptance contract")
    criterion_names = [criterion.deliverable_name for criterion in contract.criteria]
    missing = [name for name in deliverable_names if criterion_names.count(name) == 0]
    duplicates = [name for name in deliverable_names if criterion_names.count(name) > 1]
    if missing:
        raise PlannerError(
            "acceptance contract is missing required criteria for: "
            + ", ".join(missing)
        )
    if duplicates:
        raise PlannerError(
            "acceptance contract has duplicate criteria for: "
            + ", ".join(duplicates)
        )


def _required_plan_step_errors(task: Task, plan: Plan) -> list[str]:
    """Return violations of an explicit task-level ExecutionPlan contract."""
    from packages.core.plans.refs import extract_step_refs
    from packages.core.plans.task_constraints import (
        binding_constraints_forbid_artifact_writes,
        plan_step_requires_artifact_write,
    )

    details = task.details if isinstance(task.details, dict) else {}
    errors: list[str] = []
    if binding_constraints_forbid_artifact_writes(details):
        errors.extend(
            f"step {step.key!r} requires a saved artifact but USER CONSTRAINTS prohibit file/artifact writes"
            for step in plan.steps
            if plan_step_requires_artifact_write(step)
        )
    required_steps = details.get("required_plan_steps")
    if not isinstance(required_steps, list):
        return errors

    by_key = {step.key: step for step in plan.steps}
    for raw in required_steps:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        step = by_key.get(key)
        if step is None:
            errors.append(f"missing required step {key!r}")
            continue
        for field in ("kind", "service_key"):
            expected = str(raw.get(field) or "").strip()
            if expected and str(getattr(step, field, None) or "").strip() != expected:
                errors.append(
                    f"required step {key!r} must have {field}={expected!r}"
                )
        expected_output_shape = str(raw.get("output_shape") or "").strip()
        if expected_output_shape and str(getattr(step, "output_shape", None) or "").strip() != expected_output_shape:
            errors.append(
                f"required step {key!r} must have output_shape={expected_output_shape!r}"
            )
        expected_expects = raw.get("expects")
        if isinstance(expected_expects, list):
            actual_expects = [str(value).strip() for value in (getattr(step, "expects", None) or []) if str(value).strip()]
            if actual_expects != [str(value).strip() for value in expected_expects if str(value).strip()]:
                errors.append(
                    f"required step {key!r} must declare expects={expected_expects!r}"
                )
        expected_dependencies = raw.get("depends_on")
        if isinstance(expected_dependencies, list):
            missing_dependencies = [
                str(value) for value in expected_dependencies
                if str(value) and str(value) not in step.depends_on
            ]
            if missing_dependencies:
                errors.append(
                    f"required step {key!r} must depend on {missing_dependencies!r}"
                )
        prompt = str((step.params or {}).get("prompt") or "")
        for value in raw.get("prompt_substrings") or []:
            required_text = str(value).strip()
            if required_text and required_text.lower() not in prompt.lower():
                errors.append(
                    f"required step {key!r} prompt must mention {required_text!r}"
                )
        required_refs = raw.get("required_refs")
        if isinstance(required_refs, list):
            referenced_steps = {step_key for step_key, _field in extract_step_refs(step.params)}
            for required_ref in required_refs:
                producer_key = str(required_ref or "").strip()
                if producer_key and producer_key not in referenced_steps:
                    errors.append(
                        f"required step {key!r} must reference required step {producer_key!r}"
                    )
        required_ref_fields = raw.get("required_ref_fields")
        if isinstance(required_ref_fields, dict):
            refs_by_step: dict[str, set[str | None]] = {}
            for ref_step, ref_field in extract_step_refs(step.params):
                refs_by_step.setdefault(ref_step, set()).add(ref_field)
            for producer_key, fields in required_ref_fields.items():
                expected_fields = [str(value).strip() for value in (fields or []) if str(value).strip()]
                actual_fields = refs_by_step.get(str(producer_key), set())
                if not any(field in actual_fields for field in expected_fields):
                    field_label = expected_fields[0] if len(expected_fields) == 1 else str(expected_fields)
                    errors.append(
                        f"required step {key!r} must reference {producer_key}.{field_label}"
                    )
    return errors


def _normalize_internal_agent_high_risk_steps(plan: Plan) -> Plan:
    """Keep external-risk classification on concrete runtime actions."""
    changed: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if (
            step.kind in {"llm", "subagent"}
            and step.risk_level == "high"
            and not step.action_key
            and not step.capability_id
        ):
            steps.append(step.model_copy(update={"risk_level": "low"}))
            changed.append(step.key)
        else:
            steps.append(step)
    if not changed:
        return plan
    normalized = Plan(steps=steps, metadata=plan.metadata)
    try:
        existing = list(getattr(normalized.metadata, "normalized_removed_internal_high_risk", []) or [])
        normalized.metadata.normalized_removed_internal_high_risk = existing + changed
        normalized.metadata.normalization_reason = "planner_internal_agent_risk_policy_owned"
    except Exception:
        pass
    return normalized


def _normalize_planner_hard_approval_steps(plan: Plan) -> Plan:
    """Keep approval authority in workspace policy, not planner guesses.

    Plans produced here come from the LLM planner. The planner may decide what
    work should happen, but it must not create approval rules by setting
    ``requires_approval``. Runtime approvals, always-allow, and denies are
    evaluated later by workspace/task governance policy in the dispatcher.
    """
    changed: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.requires_approval:
            steps.append(step.model_copy(update={"requires_approval": False}))
            changed.append(step.key)
        else:
            steps.append(step)
    if not changed:
        return plan

    normalized = Plan(steps=steps, metadata=plan.metadata)
    try:
        existing = list(getattr(normalized.metadata, "normalized_removed_step_approvals", []) or [])
        normalized.metadata.normalized_removed_step_approvals = existing + changed
        normalized.metadata.normalization_reason = "planner_hard_approval_policy_owned"
    except Exception:
        pass
    return normalized


def _task_requests_text_report_only(task: Task) -> bool:
    title = str(getattr(task, "title", "") or "")
    description = str(getattr(task, "description", "") or "")
    expected_output = getattr(task, "expected_output", None)
    details = getattr(task, "details", None)
    text = "\n".join([
        title,
        description,
        json.dumps(expected_output, ensure_ascii=False, default=str) if expected_output else "",
        json.dumps(details, ensure_ascii=False, default=str) if details else "",
    ]).lower()
    return (
        any(term in text for term in _TEXT_REPORT_DELIVERABLE_TERMS)
        and not any(term in text for term in _EXPLICIT_SAVED_ARTIFACT_TERMS)
    )


def _is_unrequested_text_report_file_write_step(step: PlanStep) -> bool:
    prompt = json.dumps(step.params or {}, ensure_ascii=False, default=str).lower()
    return (
        step.capability_id == "file.write"
        or step.requires_approval and "generate_file" in prompt
        or "file_url" in prompt
        or "fs_path" in prompt
    ) and (
        "generate_file" in prompt
        or "save the file" in prompt
        or "file_url" in prompt
        or "fs_path" in prompt
        or "保存" in prompt
    )


def _parse_plan(text: str) -> Optional[Plan]:
    """Try to extract + validate a Plan from raw LLM text. Tolerates
    fenced code blocks (```json … ```) and stray prose."""
    if not text:
        return None
    candidate = _strip_code_fence(text).strip()
    try:
        return Plan.model_validate_json(candidate)
    except ValidationError as exc:
        logger.debug("Planner Plan validation failed: %s", exc)
        return None
    except ValueError:
        # Not JSON at all.
        return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # ```json or ```\n
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ── Allowlist enforcement ─────────────────────────────────────────────

def _ctx_provider_actions_for_step(ctx: _Context, service_key: str | None) -> dict[str, list[str]]:
    if ctx.service_provider_actions:
        return dict(ctx.service_provider_actions.get(str(service_key or ""), {}) or {})
    return ctx.provider_actions


def _ctx_provider_action_specs_for_step(
    ctx: _Context,
    service_key: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    if ctx.service_provider_action_specs:
        return dict(ctx.service_provider_action_specs.get(str(service_key or ""), {}) or {})
    return ctx.provider_action_specs


def _enforce_allowlists(plan: Plan, ctx: _Context) -> None:
    _enforce_capability_assignments(plan, ctx)
    for s in plan.steps:
        if s.service_key and s.service_key not in ctx.allowed_service_keys:
            raise CapabilityError(
                f"step {s.key}: service_key={s.service_key!r} is not in "
                f"the workspace's allowlist {sorted(ctx.allowed_service_keys)}"
            )
        if s.kind == "action":
            provider_actions = _ctx_provider_actions_for_step(ctx, s.service_key)
            provider_action_specs = _ctx_provider_action_specs_for_step(ctx, s.service_key)
            binding = runtime_planner_action_binding_for(
                provider=str(s.provider or ""),
                action_key=str(s.action_key or ""),
                provider_actions=provider_actions,
                provider_action_specs=provider_action_specs,
            )
            available = provider_actions.get(s.provider or "")
            if available is None:
                raise CapabilityError(
                    f"step {s.key}: provider={s.provider!r} not available "
                    f"for service_key={s.service_key!r}"
                )
            if s.action_key not in available:
                raise CapabilityError(
                    f"step {s.key}: action {s.action_key!r} not in "
                    f"allowed actions for service_key={s.service_key!r} "
                    f"provider {s.provider}: {available}"
                )
            try:
                account_selection_mode = IntegrationAccountSelectionMode.resolve(
                    selector=s.integration_id,
                    requested=(s.params or {}).get(
                        "integration_account_selection"
                    ),
                )
            except ValueError as exc:
                raise CapabilityError(f"step {s.key}: {exc}") from exc
            all_accounts = (
                account_selection_mode is IntegrationAccountSelectionMode.ALL
            )
            if all_accounts and (
                binding is None
                or binding.effect is not MCPActionEffect.READ
            ):
                raise CapabilityError(
                    f"step {s.key}: all-account execution is restricted to "
                    "actions declared read-only"
                )
            if all_accounts and binding is not None and not binding.supports_all_accounts:
                raise CapabilityError(
                    f"step {s.key}: all-account execution requires compatible account contracts"
                )
            if all_accounts and binding is not None and binding.requires_explicit_account:
                raise CapabilityError(
                    f"step {s.key}: {s.provider}.{s.action_key} requires an exact "
                    "integration_id because its account registry is incomplete"
                )
            if binding is not None and binding.account_ids and not all_accounts:
                if s.integration_id and s.integration_id not in binding.account_ids:
                    raise CapabilityError(
                        f"step {s.key}: integration_id={s.integration_id!r} does not "
                        f"expose {s.provider}.{s.action_key}; choose one of "
                        f"{list(binding.account_ids)}"
                    )
                if binding.requires_explicit_account and not s.integration_id:
                    raise CapabilityError(
                        f"step {s.key}: {s.provider}.{s.action_key} requires an exact "
                        "integration_id from its account_ids binding"
                    )
                if not s.integration_id:
                    # Pin the first actor-ordered supported account into the
                    # persisted step; the worker must not re-resolve a
                    # provider-wide default that may not expose this action.
                    s.integration_id = binding.account_ids[0]
            runtime_apply_action_binding_schemas_to_steps(
                [s],
                provider_actions=provider_actions,
                provider_action_specs=provider_action_specs,
            )
            if binding is not None and binding.effect is not None:
                if binding.effect in {
                    MCPActionEffect.WRITE,
                    MCPActionEffect.DESTRUCTIVE,
                }:
                    s.risk_level = "high"
                    s.requires_approval = True
            inferred_capability_id = runtime_capability_id_for_action_key(
                s.action_key,
                provider=s.provider,
            )
            if inferred_capability_id and s.capability_id != inferred_capability_id:
                raise CapabilityError(
                    f"step {s.key}: capability_id={s.capability_id!r} does not "
                    f"match provider/action capability {inferred_capability_id!r}"
                )


# ── Fallback for dev / CI without LLM ─────────────────────────────────

def _fallback_plan(task: Task, ctx: _Context) -> Plan:
    """Two-step generic plan: think, then summarise.

    Used when AIEngine.chat raises (no API key, network down). Lets
    Demo A v0 smoke tests run end-to-end without hitting the LLM.
    score_goals still uses the persist-only plan.
    """
    if _is_goal_scoring_task(task):
        return _build_validated_goal_scoring_plan(task, ctx)
    if getattr(task, "workspace_id", None):
        coordinator = getattr(ctx, "coordinator", None)
        if coordinator is None:
            raise PlannerError(
                "Workspace fallback planning requires a factory-owned "
                "coordinator selection"
            )
        primary_service = coordinator.root.service_key
    else:
        # Standalone entity Tasks retain the legacy smoke-test fallback. They
        # do not have a Workspace coordinator selection by definition.
        primary_service = task.owner_service_key or next(
            iter(ctx.allowed_service_keys), None
        )
    if not primary_service:
        # No services at all — emit a single human step so the user
        # sees something instead of nothing.
        return Plan(
            steps=[
                PlanStep(
                    key="manual_only",
                    kind="human",
                    params={"prompt": f"No services configured to plan task: {task.title}"},
                    description="Manual fallback (no service_key available).",
                )
            ]
        )
    task_prompt = runtime_planner_task_prompt(task)
    return Plan(
        steps=[
            PlanStep(
                key="think",
                kind="llm",
                service_key=primary_service,
                output_shape="TextResult",
                execution_contract={"delivery": "inline", "effects": "read_only"},
                params={"prompt": f"Think out loud about how to do this task:\n\n{task_prompt}"},
                description="Reason about the task.",
            ),
            PlanStep(
                key="summarize",
                kind="llm",
                service_key=primary_service,
                output_shape="TextResult",
                execution_contract={"delivery": "inline", "effects": "read_only"},
                params={
                    "prompt": "Summarise the conclusion and any runtime requirements used in 3 bullet points: ${{ steps.think.result.text }}"
                },
                depends_on=["think"],
                description="Summarise into a concrete plan.",
            ),
        ]
    )
