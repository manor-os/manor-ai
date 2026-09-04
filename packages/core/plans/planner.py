"""LLM-driven Planner.

Inputs: a Task (already created with owner_service_key + delegates) +
its workspace context (active subscriptions, available providers /
actions, relevant memories).

Output: a Pydantic ``Plan`` validated against the canonical schema.

Flow:
  1. Resolve workspace context — subscriptions, allowed actions per
     subscription, recent relevant memory.
  2. Build the system + user prompt.
  3. Single Claude call returning JSON.
  4. ``Plan.model_validate_json`` — on failure, one re-prompt with
     the validation error attached.
  5. Cross-check against workspace allowlists (service_keys, actions)
     so the Planner can't conjure capabilities the workspace lacks.
  6. Persist via ``plans.service.create_plan_from_dag``.

If the LLM is unreachable / no API key, falls back to a tiny
rule-based stub so Demo A v0 still demonstrates end-to-end execution
during dev / CI.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

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
    runtime_planner_action_specs_from_tools_cached,
    runtime_planner_action_binding_for,
    runtime_planner_llm_billing_context,
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
from packages.core.constants.task import TaskStatus, TaskType
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
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
from packages.core.services.workflow_run_execution_claim import (
    commit_fenced_execution_boundary,
    task_plan_execution_claim,
)

logger = logging.getLogger(__name__)


PLANNER_VERSION = "v0.1-demo-a"


class PlannerError(Exception):
    """Planner couldn't produce a valid plan after retries."""


class TaskPlanAdmissionError(PlannerError):
    """The Task lifecycle does not allow background planning."""

    def __init__(self, task_id: str, status: str | None, reason: str) -> None:
        self.task_id = task_id
        self.status = status
        self.reason = reason
        super().__init__(reason)


class CapabilityError(Exception):
    """Planner produced a plan referencing capabilities not in the
    workspace's allowlists. Should never happen if the prompt was
    followed; we re-validate as a safety net."""


class TaskPlanClaimHeldError(RuntimeError):
    """Another live caller already owns billable planning for this Task."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} is already being planned")


def task_plan_admission_error(task: Task) -> str | None:
    """Return why this Task cannot enter the background Plan runtime."""
    if getattr(task, "task_type", None) == TaskType.INTERACTIVE.value:
        return "interactive tasks run through Task Session, not Plans"
    status = getattr(task, "status", None)
    if status != TaskStatus.IN_PROGRESS.value:
        return (
            f"task status {status!r} cannot start a new Plan; "
            f"expected {TaskStatus.IN_PROGRESS.value!r}"
        )
    return None


@dataclass(frozen=True)
class _TaskPlanningScope:
    entity_id: str
    workspace_id: str | None


async def _lock_task_planning_origin(
    db: AsyncSession,
    task_id: str,
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
    if scope.workspace_id:
        from packages.core.services.workspace_access import (
            lock_workspace_access_boundary,
        )

        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=scope.workspace_id,
            entity_id=scope.entity_id,
        )
        if (
            workspace is None
            or workspace.deleted_at is not None
            or workspace.status != "active"
        ):
            raise PlannerError("Workspace is not active for planning")

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
    await raise_for_active_task_plan(
        db,
        task_id=str(task.id),
        entity_id=str(task.entity_id),
    )
    return task, scope


async def _admit_task_without_holding_locks(
    db: AsyncSession,
    task_id: str,
) -> _TaskPlanningScope:
    """Validate admission under row locks, then release them before LLM I/O.

    PostgreSQL releases row locks acquired after a savepoint when that
    savepoint is rolled back. Lightweight test doubles do not implement nested
    transactions, so they keep the old single-session behavior.
    """
    begin_nested = getattr(db, "begin_nested", None)
    if not callable(begin_nested):
        _task, scope = await _lock_task_planning_origin(db, task_id)
        return scope

    savepoint = await begin_nested()
    try:
        _task, scope = await _lock_task_planning_origin(db, task_id)
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
) -> None:
    context_workspace = getattr(context, "workspace", None)
    if context_workspace is None:
        return

    from packages.core.services.workspace_readiness import (
        evaluate_current_workspace_blocking_setup,
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
        raise PlannerError(
            "Workspace setup is incomplete; automated setup must run through "
            "the Blueprint's authorized setup job before Tasks may be planned"
        )


# ── Public entry point ────────────────────────────────────────────────

async def plan_task(
    db: AsyncSession,
    task_id: str,
    *,
    execution_mode: Optional[str] = None,
    before_provider: (
        Callable[[], Awaitable[RuntimeResolvedBillingScope | None]] | None
    ) = None,
) -> ExecutionPlan:
    """Generate + persist a Plan for ``task_id``. Raises if the task
    doesn't exist or the Planner can't produce a valid plan.

    ``execution_mode`` defaults to whatever the task's workspace asks
    for — sandbox workspaces get ``sandbox`` automatically, regular
    workspaces get ``live``. Callers can still force a specific mode
    (eg. UI "Run as dry-run preview" button)."""
    requested_execution_mode = execution_mode
    task_scope = await _admit_task_without_holding_locks(db, task_id)

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
    await _assert_workspace_setup_allows_planning(db, context)
    planning_task = _planning_task_snapshot(task)

    if execution_mode is None:
        from packages.core.workspaces import default_execution_mode
        execution_mode = (
            default_execution_mode(context.workspace)
            if context.workspace else "live"
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

    gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=getattr(planning_task, "expected_output", None),
        require_explicit_agent_outputs=True,
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
        gaps = plan_contract_gaps(
            plan.topo_order(),
            task_expected_output=getattr(planning_task, "expected_output", None),
            require_explicit_agent_outputs=True,
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
    task, final_scope = await _lock_task_planning_origin(db, task_id)
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

    final_context = await _gather_context(db, task)
    await _assert_workspace_setup_allows_planning(db, final_context)
    plan = _apply_replan_approval_constraints(task, plan)
    plan = bind_task_output_contract(plan, getattr(task, "expected_output", None))
    _enforce_allowlists(plan, final_context)
    final_gaps = plan_contract_gaps(
        plan.topo_order(),
        task_expected_output=getattr(task, "expected_output", None),
        require_explicit_agent_outputs=True,
    )
    final_required_step_errors = _required_plan_step_errors(task, plan)
    if final_gaps or final_required_step_errors:
        if final_required_step_errors and not final_gaps:
            raise PlannerError(
                "required plan steps changed while planning: "
                + "; ".join(final_required_step_errors)
            )
        raise PlanContractError(final_gaps)
    if requested_execution_mode is None:
        from packages.core.workspaces import default_execution_mode
        execution_mode = (
            default_execution_mode(final_context.workspace)
            if final_context.workspace else "live"
        )

    return await create_plan_from_dag(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        agent_subscription_id=task.owner_subscription_id,
        plan=plan,
        planner_version=PLANNER_VERSION,
        parent_plan_id=_replan_parent_plan_id(task),
        execution_mode=execution_mode,
        enforce_contract=True,
    )


async def plan_task_and_commit(
    db: AsyncSession,
    task_id: str,
    *,
    execution_mode: Optional[str] = None,
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
        self.agent_skill_names = agent_skill_names or {}


async def _gather_context(db: AsyncSession, task: Task) -> _Context:
    workspace: Optional[Workspace] = None
    if task.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == task.workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()

    subs: list[AgentSubscription] = []
    if task.workspace_id:
        subs = list((await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == task.entity_id,
                AgentSubscription.workspace_id == task.workspace_id,
                AgentSubscription.status == "active",
            )
        )).scalars().all())

    # Allowlist of service_keys = task.owner_service_key + delegates.
    # Falls back to "all subscriptions in the workspace" if the task
    # didn't pin an owner (legacy tasks during the migration window).
    explicit = set(task.delegate_service_keys or [])
    if task.owner_service_key:
        explicit.add(task.owner_service_key)
    allowed_service_keys = explicit if explicit else {
        s.service_key for s in subs if s.service_key
    }
    subs = [
        s for s in subs
        if getattr(s, "service_key", None) in allowed_service_keys
    ]

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

    service_provider_actions, service_provider_action_specs = await _compute_service_provider_actions(
        db,
        subscriptions=subs,
        agents_by_id=agents_by_id,
        actor_user_id=actor_user_id,
        entity_id=task.entity_id,
    )
    provider_actions = _union_provider_actions(service_provider_actions)
    provider_action_specs = _union_provider_action_specs(service_provider_action_specs)

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

    # Per-agent platform tool bindings + skill bindings — so the Planner
    # knows each agent's capabilities beyond MCP actions (e.g. write_file,
    # generate_document_file, invoke_skill, web_search).
    agent_tool_names: dict[str, list[str]] = {}
    agent_skill_names: dict[str, list[dict]] = {}
    if agent_ids:
        from packages.core.models.workspace import AgentToolBinding, ToolDefinition
        from packages.core.models.skill import AgentSkillBinding, Skill

        # Tool bindings — join to ToolDefinition to get human-readable names
        tool_binding_rows = list((await db.execute(
            select(AgentToolBinding.agent_id, ToolDefinition.name).join(
                ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id,
            ).where(AgentToolBinding.agent_id.in_(agent_ids))
        )).all())
        for agent_id_val, tool_name in tool_binding_rows:
            agent_tool_names.setdefault(agent_id_val, []).append(tool_name)

        # Skill bindings
        skill_binding_rows = list((await db.execute(
            select(AgentSkillBinding).where(AgentSkillBinding.agent_id.in_(agent_ids))
        )).scalars().all())
        skill_ids = [sb.skill_id for sb in skill_binding_rows]
        skill_map: dict[str, Skill] = {}
        if skill_ids:
            skill_rows = list((await db.execute(
                select(Skill).where(Skill.id.in_(skill_ids))
            )).scalars().all())
            skill_map = {s.id: s for s in skill_rows}
        for sb in skill_binding_rows:
            sk = skill_map.get(sb.skill_id)
            if sk:
                agent_skill_names.setdefault(sb.agent_id, []).append({
                    "slug": sk.slug or sk.name,
                    "name": sk.name,
                    "description": (sk.description or "")[:150],
                })

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
        agent_skill_names=agent_skill_names,
    )


async def _compute_service_provider_actions(
    db: AsyncSession,
    *,
    subscriptions: list[AgentSubscription],
    agents_by_id: dict[str, Agent],
    actor_user_id: str | None,
    entity_id: str,
) -> tuple[
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    """Derive {service_key: {provider_key: [allowed_action_keys, ...]}}.

    Reads agent_mcp_bindings + mcp_servers.tools_cached. Empty when no
    bindings exist — Planner gets an empty action menu for that service and
    produces a pure-LLM/subagent plan, which is the right behaviour."""
    if not agents_by_id or not subscriptions:
        return {}, {}

    service_keys_by_agent: dict[str, set[str]] = {}
    for subscription in subscriptions:
        agent_id = getattr(subscription, "agent_id", None)
        service_key = str(getattr(subscription, "service_key", "") or "").strip()
        if agent_id in agents_by_id and service_key:
            service_keys_by_agent.setdefault(agent_id, set()).add(service_key)
    if not service_keys_by_agent:
        return {}, {}

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    bindings = list((await db.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id.in_(list(service_keys_by_agent.keys())),
            AgentMCPBinding.status == "active",
        )
    )).scalars().all())
    if not bindings:
        return {}, {}

    server_ids = {b.mcp_server_id for b in bindings}
    servers = {
        s.id: s for s in (await db.execute(
            select(MCPServer).where(
                MCPServer.id.in_(server_ids),
                MCPServer.status == "active",
            )
        )).scalars().all()
    }
    from packages.core.services.mcp_account_tool_catalog import (
        actor_provider_mcp_tool_caches,
    )
    from packages.core.services.official_remote_mcp import OfficialRemoteMCPProvider

    official_providers = {item.value for item in OfficialRemoteMCPProvider}
    provider_endpoints = {
        server.server_key: server.endpoint
        for server in servers.values()
        if server.server_key in official_providers and server.endpoint
    }
    try:
        discovered_tool_caches = await actor_provider_mcp_tool_caches(
            db,
            provider_endpoints=provider_endpoints,
            user_id=str(actor_user_id or ""),
            entity_id=entity_id,
        )
    except Exception:
        logger.warning(
            "Planner could not load actor-scoped MCP account catalogs; using server fallback",
            exc_info=True,
        )
        discovered_tool_caches = {}

    out: dict[str, dict[str, list[str]]] = {}
    spec_out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for b in bindings:
        srv = servers.get(b.mcp_server_id)
        if not srv:
            continue
        tool_specs = runtime_planner_action_specs_from_tools_cached(
            discovered_tool_caches.get(srv.server_key) or srv.tools_cached or []
        )
        all_tool_names = list(tool_specs)
        configured_allowed = b.allowed_tools if b.allowed_tools is not None else srv.default_allowed_tools
        if configured_allowed is not None:
            allowed_set = {str(n) for n in configured_allowed if str(n or "").strip()}
            allowed = [n for n in all_tool_names if n in allowed_set or f"mcp__{srv.server_key}__{n}" in allowed_set]
        else:
            allowed = all_tool_names
        if not allowed:
            continue
        for service_key in service_keys_by_agent.get(b.agent_id, set()):
            service_actions = out.setdefault(service_key, {}).setdefault(srv.server_key, [])
            service_specs = spec_out.setdefault(service_key, {}).setdefault(srv.server_key, {})
            for n in allowed:
                if n not in service_actions:
                    service_actions.append(n)
                service_specs.setdefault(n, tool_specs.get(n, {}))
    from packages.core.services.agent_permission_service import resolve_agent_direct_mcp_actions

    direct_by_provider: dict[str, set[str]] = {}
    for agent_id in service_keys_by_agent:
        direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
        for provider, actions in direct_actions.items():
            direct_by_provider.setdefault(provider, set()).update(actions)
    if direct_by_provider:
        direct_servers = {
            s.server_key: s for s in (await db.execute(
                select(MCPServer).where(
                    MCPServer.server_key.in_(list(direct_by_provider)),
                    MCPServer.status == "active",
                )
            )).scalars().all()
        }
        missing_discovery_providers = set(direct_servers) - set(discovered_tool_caches)
        if missing_discovery_providers:
            missing_endpoints = {
                provider: direct_servers[provider].endpoint
                for provider in missing_discovery_providers
                if (
                    provider in official_providers
                    and direct_servers[provider].endpoint
                )
            }
            try:
                discovered_tool_caches.update(
                    await actor_provider_mcp_tool_caches(
                        db,
                        provider_endpoints=missing_endpoints,
                        user_id=str(actor_user_id or ""),
                        entity_id=entity_id,
                    )
                )
            except Exception:
                logger.warning(
                    "Planner could not load direct actor MCP catalogs; using server fallback",
                    exc_info=True,
                )
        for agent_id, service_keys in service_keys_by_agent.items():
            direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
            for provider, actions in direct_actions.items():
                srv = direct_servers.get(provider)
                if not srv:
                    continue
                tool_specs = runtime_planner_action_specs_from_tools_cached(
                    discovered_tool_caches.get(srv.server_key) or srv.tools_cached or []
                )
                allowed = [action for action in sorted(actions) if action in tool_specs]
                if not allowed:
                    continue
                for service_key in service_keys:
                    service_actions = out.setdefault(service_key, {}).setdefault(provider, [])
                    service_specs = spec_out.setdefault(service_key, {}).setdefault(provider, {})
                    for action in allowed:
                        if action not in service_actions:
                            service_actions.append(action)
                        service_specs.setdefault(action, tool_specs.get(action, {}))
    return out, spec_out


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
    """Multi-turn agent loop — the Planner researches context and tools
    before committing to a plan. Up to MAX_PLANNER_TURNS turns.

    Available tools:
      list_tools(service_key)   — see what actions an agent can perform
      get_tool_schema(provider, action_key) — input/output schema for a tool
      submit_plan(plan_json)    — finalize and validate the plan DAG
    """
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
        agent_skill_names=ctx.agent_skill_names,
    )
    user_prompt = runtime_planner_task_prompt(task)
    tools = runtime_planner_tool_schemas()

    messages: list[dict[str, Any]] = [runtime_planner_user_message(user_prompt)]

    submitted_plan: Optional[Plan] = None

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
                logger.warning("Planner: LLM unavailable, using fallback stub")
                return _fallback_plan(task, ctx)
            break

        # Handle tool calls
        if response.tool_calls:
            messages.append(runtime_planner_assistant_message(response))
            for tc in response.tool_calls:
                tool_name = tc.get("name") or (tc.get("function") or {}).get("name", "")
                raw_args = tc.get("arguments") or (tc.get("function") or {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, TypeError):
                    args = {}

                result = runtime_execute_planner_tool_call(
                    tool_name,
                    args,
                    context=ctx,
                    parse_plan=_parse_plan,
                    enforce_plan=lambda plan: _enforce_allowlists(plan, ctx),
                )

                # Check if submit_plan returned a valid plan
                if tool_name == "submit_plan" and isinstance(result, dict) and result.get("_plan"):
                    submitted_plan = _normalize_plan_for_task(task, result["_plan"])
                    result_text = result.get("message", "Plan accepted.")
                else:
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
            plan = _parse_plan(response.content)
            if plan:
                return _normalize_plan_for_task(task, plan)
            # Ask to use submit_plan tool
            messages.append(runtime_planner_assistant_message(response))
            messages.append(runtime_planner_user_message(
                "Please use the submit_plan tool to submit your plan as valid JSON."
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
    plan = _normalize_internal_agent_high_risk_steps(plan)
    plan = _normalize_planner_hard_approval_steps(plan)
    if not _task_requests_text_report_only(task):
        return plan

    depended_on = {dep for step in plan.steps for dep in step.depends_on}
    removable_keys = {
        step.key
        for step in plan.steps
        if step.key not in depended_on
        and _is_unrequested_text_report_file_write_step(step)
    }
    if not removable_keys or len(removable_keys) >= len(plan.steps):
        return plan

    normalized = Plan(
        steps=[step for step in plan.steps if step.key not in removable_keys],
        metadata=plan.metadata,
    )
    try:
        normalized.metadata.normalized_removed_steps = sorted(removable_keys)
        normalized.metadata.normalization_reason = "unrequested_text_report_file_write"
    except Exception:
        pass
    return normalized


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
    """
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
                params={"prompt": f"Think out loud about how to do this task:\n\n{task_prompt}"},
                description="Reason about the task.",
            ),
            PlanStep(
                key="summarize",
                kind="llm",
                service_key=primary_service,
                output_shape="TextResult",
                params={
                    "prompt": "Summarise the conclusion and any runtime requirements used in 3 bullet points: ${{ steps.think.result.text }}"
                },
                depends_on=["think"],
                description="Summarise into a concrete plan.",
            ),
        ]
    )
