"""ExecutionPlan + ExecutionStep CRUD + plan materialisation.

The ``materialize_plan_steps`` helper is the bridge from the Pydantic
``Plan`` shape (what the Planner produces) to one ``execution_steps``
row per node (what the executor / dispatcher reads). Doing this once
on plan launch — rather than re-parsing plan_dag on every executor
cycle — keeps the hot path of ``run_cycle`` cheap and lets the
dispatcher query "all pending steps for this plan" with a single
indexed lookup.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import (
    TaskLogType,
    TaskRuntimeKind,
    TaskStatus,
)
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    PLAN_TERMINAL_STATUSES,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.step_reset import (
    lock_plan_leases_then_steps,
    revoke_leases_before_cancelling_steps,
)

if TYPE_CHECKING:
    from packages.core.services.task_execution import CoordinatorRuntimeSelection
    from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim

logger = logging.getLogger(__name__)


class ActiveTaskPlanError(Exception):
    """A Task already owns a non-terminal execution Plan.

    Task row locking in ``create_plan_from_dag`` makes this check the shared
    persistence boundary for Planner, worker, and manual API callers.  Keep the
    existing Plan identity on the exception so background duplicate deliveries
    can finish idempotently without dispatching a second execution.
    """

    def __init__(self, task_id: str, plan_id: str, status: str) -> None:
        self.task_id = task_id
        self.plan_id = plan_id
        self.status = status
        super().__init__(
            f"task {task_id} already has active plan {plan_id} ({status})"
        )


class TaskEffectReplayBlockedError(Exception):
    """A new Plan would cross a Task-history external-effect boundary."""

    def __init__(self, task_id: str, detail: str) -> None:
        self.task_id = task_id
        self.detail = detail
        super().__init__(detail)


async def get_active_task_plan(
    db: AsyncSession,
    *,
    task_id: str,
    entity_id: str,
) -> ExecutionPlan | None:
    """Return the newest non-terminal Plan currently owned by a Task."""

    terminal_statuses = [status.value for status in PLAN_TERMINAL_STATUSES]
    return (
        await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.task_id == task_id,
                ExecutionPlan.entity_id == entity_id,
                ExecutionPlan.status.notin_(terminal_statuses),
            )
            .order_by(ExecutionPlan.created_at.desc(), ExecutionPlan.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def raise_for_active_task_plan(
    db: AsyncSession,
    *,
    task_id: str,
    entity_id: str,
) -> None:
    """Raise with the reusable Plan identity when a Task is already planned."""

    active_plan = await get_active_task_plan(
        db,
        task_id=task_id,
        entity_id=entity_id,
    )
    if active_plan is not None:
        raise ActiveTaskPlanError(
            task_id,
            str(active_plan.id),
            str(active_plan.status),
        )


async def _locked_task_coordinator_run_for_plan(
    db: AsyncSession,
    *,
    task: Any,
    selection: CoordinatorRuntimeSelection,
    claim: TaskCoordinatorClaim,
    parent_plan_id: str | None,
) -> Any:
    """Fence a V3 Plan write to the exact live Task-loop owner."""

    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun

    run = (await db.execute(
        select(TaskCoordinatorRun)
        .where(TaskCoordinatorRun.id == claim.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    expiry = run.claim_expires_at if run is not None else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (
        run is None
        or run.task_id != task.id
        or run.entity_id != task.entity_id
        or run.workspace_id != task.workspace_id
        or claim.task_id != task.id
        or run.root_subscription_id != selection.root.subscription_id
        or run.root_agent_id != selection.root.agent_id
        or run.selection_fingerprint != selection.stable_fingerprint()
        or run.phase in TaskCoordinatorPhase.terminal()
        or run.claim_token != claim.claim_token
        or run.claim_generation != claim.wake_generation
        or run.wake_generation != claim.wake_generation
        or expiry is None
        or expiry <= datetime.now(timezone.utc)
    ):
        raise ValueError("Task coordinator Plan claim or factory selection is stale")
    if parent_plan_id != run.current_plan_id:
        raise ValueError("Task coordinator Plan parent does not match the current Plan")
    return run


def bind_task_output_contract(plan: Plan, task_expected_output: Any) -> Plan:
    """Make the Task deliverable the persisted terminal Plan contract.

    Materialization used to replace a Planner-authored terminal shape only on
    ``ExecutionStep``.  That left ``ExecutionPlan.plan_dag`` claiming one
    output while the worker and dispatcher enforced another.  Bind the
    runtime-owned Task contract before persistence so Plan, Step, downstream
    aggregation, and supervision all read the same schema.

    Ambiguous terminal producers are intentionally left unchanged; the shared
    ``plan_contract_gaps`` gate reports that structural error and blocks the
    Plan instead of guessing which result is final.
    """
    from packages.core.contracts.task_output import (
        task_output_envelope_schema,
        terminal_agent_step_key,
    )

    contract_schema = task_output_envelope_schema(task_expected_output)
    if contract_schema is None:
        return plan
    terminal_key = terminal_agent_step_key(plan.steps)
    if terminal_key is None:
        return plan

    changed = False
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.key != terminal_key:
            steps.append(step)
            continue
        if step.output_shape is None and step.expected_output_schema == contract_schema:
            steps.append(step)
            continue
        steps.append(
            step.model_copy(
                update={
                    "output_shape": None,
                    "expected_output_schema": contract_schema,
                }
            )
        )
        changed = True
    return Plan(steps=steps, metadata=plan.metadata) if changed else plan


# ── Plan CRUD ─────────────────────────────────────────────────────────

async def create_plan_from_dag(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str],
    task_id: Optional[str],
    agent_subscription_id: Optional[str],
    plan: Plan,
    planner_version: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    execution_mode: str = "live",
    approval_required: bool = False,
    enforce_contract: bool = False,
    allow_generated_coordinator: bool = False,
    expected_selection: CoordinatorRuntimeSelection | None = None,
    task_coordinator_claim: TaskCoordinatorClaim | None = None,
) -> ExecutionPlan:
    """Persist a validated Plan as ExecutionPlan + ExecutionStep rows.

    Sets initial status='draft' (or 'pending_approval' when the
    plan-level approval_required flag is explicitly set). Step-level
    approval is enforced by the Dispatcher when that step becomes
    runnable, so safe predecessor work can still proceed.
    """
    task_expected_output = None
    task = None
    if task_id:
        from packages.core.models.task import Task

        # Serialize every Task-bound Plan creation at the final persistence
        # boundary.  Planner releases this lock during provider I/O, so a
        # caller that wins meanwhile is observed here before any new Plan or
        # Step row is written.
        task = (
            await db.execute(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.entity_id == entity_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise ValueError(f"task {task_id} not found in entity {entity_id}")

        await raise_for_active_task_plan(
            db,
            task_id=task_id,
            entity_id=entity_id,
        )

        # Final, Task-locked replay fence. Queue-time checks are advisory: a
        # stale Celery delivery, manual API call, or planner that was already
        # in provider I/O must all revalidate the complete Task history here
        # before a new Plan/Step identity can be persisted.
        from packages.core.dispatcher.effect_safety import (
            task_execution_effect_block_detail,
        )

        effect_block = await task_execution_effect_block_detail(db, task_id)
        if effect_block:
            raise TaskEffectReplayBlockedError(task_id, effect_block)

        task_expected_output = task.expected_output

    # Params are an untrusted Planner/API surface.  Runtime-only markers must
    # never turn an ordinary step into a hidden child or enable delegation.
    # The sole exception is the exact plan shape produced by the server's
    # coordinator fold, and that exception must still be eligible for this
    # Task through the closed runtime factory.
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
        assert_no_server_owned_plan_params,
        is_generated_coordinator_plan,
        validate_coordinator_source_plan,
    )
    from packages.core.services.task_execution import resolve_task_execution

    if expected_selection is not None and (task is None or not task.workspace_id):
        raise ValueError(
            "expected coordinator selection requires a Workspace Task"
        )
    if task_coordinator_claim is not None and (task is None or not task.workspace_id):
        raise ValueError("Task coordinator Plan requires a Workspace Task")
    if task is not None and task.workspace_id and task_coordinator_claim is None:
        raise ValueError(
            "New Workspace Task Plans require a live Task coordinator claim; "
            "submit a planning request instead"
        )

    supplied_generated_coordinator = is_generated_coordinator_plan(plan)
    try:
        assert_no_server_owned_plan_params(plan)
    except ValueError:
        if (
            not allow_generated_coordinator
            or task is None
            or not task.workspace_id
            or not supplied_generated_coordinator
        ):
            raise
    else:
        supplied_generated_coordinator = False

    coordinator_selection = None
    task_coordinator_run = None
    if task is not None and task.workspace_id:
        if task.status != TaskStatus.IN_PROGRESS.value:
            raise ValueError(
                f"Task status {task.status!r} cannot create a new Plan; "
                f"expected {TaskStatus.IN_PROGRESS.value!r}"
            )
        execution = await resolve_task_execution(
            db,
            task,
            lock_subscriptions=True,
        )
        current_selection = execution.coordinator
        if expected_selection is not None:
            # The Planner generated against this exact immutable selection.
            # Re-resolve under the Task/subscription locks, then persist and
            # materialize only the expected identities—not a fresh B/C choice.
            expected_selection.require_current(current_selection)
            coordinator_selection = expected_selection
        else:
            coordinator_selection = current_selection
        if (
            execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
            or coordinator_selection is None
        ):
            if supplied_generated_coordinator:
                raise ValueError(
                    "coordinator services exceed the active Task subscription scope"
                )
            raise ValueError(
                f"Task runtime {execution.runtime_kind.value!r} cannot create "
                "a new Plan"
            )

        root_binding = coordinator_selection.root
        if (
            agent_subscription_id
            and str(agent_subscription_id) != root_binding.subscription_id
        ):
            raise ValueError(
                "Plan agent subscription does not match the Task runtime's "
                "coordinator root"
            )
        agent_subscription_id = root_binding.subscription_id

        if task_coordinator_claim is not None:
            if supplied_generated_coordinator:
                raise ValueError("Task coordinator Plan requires the source typed DAG")
            if str(workspace_id or "") != str(task.workspace_id):
                raise ValueError("Task coordinator Plan Workspace does not match its Task")
            task_coordinator_run = await _locked_task_coordinator_run_for_plan(
                db,
                task=task,
                selection=coordinator_selection,
                claim=task_coordinator_claim,
                parent_plan_id=parent_plan_id,
            )
            plan = bind_task_output_contract(plan, task_expected_output)
            validate_coordinator_source_plan(plan, coordinator=coordinator_selection)
            # V3 is a server-owned execution path: even an internal caller that
            # omits the optional generic flag cannot skip its DAG contract gate.
            enforce_contract = True

    if task is not None:
        plan = bind_task_output_contract(plan, task_expected_output)

    if enforce_contract:
        # Keep every persistence entrypoint on the same contract gate as the
        # Planner.  The Planner already performs this check before calling us;
        # manual/API callers do not, so the materializer is the final boundary.
        gaps = plan_contract_gaps(
            plan.topo_order(),
            task_expected_output=task_expected_output,
            require_explicit_agent_outputs=True,
            require_explicit_execution_contract=True,
        )
        if gaps:
            raise PlanContractError(gaps)

    # Plan-level approval is an explicit operator/API choice. Do not promote
    # every approval-gated/high-risk step into a full-plan gate; the
    # Dispatcher pauses those steps at execution time.
    plan_needs_approval = bool(approval_required)
    initial_status = "pending_approval" if plan_needs_approval else "draft"

    row = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_subscription_id=agent_subscription_id,
        plan_dag=plan.model_dump(mode="json"),
        planner_version=planner_version,
        parent_plan_id=parent_plan_id,
        status=initial_status,
        approval_required=plan_needs_approval,
        execution_mode=execution_mode,
        dispatcher_state=(
            {
                TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
                TASK_COORDINATOR_RUN_ID_KEY: task_coordinator_run.id,
            }
            if task_coordinator_run is not None else {}
        ),
    )
    db.add(row)
    await db.flush()
    if task_coordinator_run is not None:
        from packages.core.models.task_coordinator import TaskCoordinatorPhase

        task_coordinator_run.current_plan_id = row.id
        task_coordinator_run.phase = TaskCoordinatorPhase.WAITING_PLAN.value
        task_coordinator_run.state_version += 1

    # Materialise immediately so step rows exist as soon as the plan
    # does — keeps the executor path uniform regardless of whether the
    # plan needs approval (approval just gates the status transition,
    # not the existence of step rows).
    await materialize_plan_steps(
        db,
        row.id,
        plan,
        coordinator=coordinator_selection,
    )
    if plan_needs_approval and task_id:
        await _surface_pending_plan_approval(db, row)
    if not plan_needs_approval:
        # Plan creation and its first runnable cycle are one durable boundary.
        # Every caller (Planner, manual API, recovery) therefore receives the
        # same crash-safe admission contract without relying on a post-commit
        # process callback or direct heavy-task enqueue.
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            row.id,
            reason="plan_created",
        )
    return row


async def _surface_pending_plan_approval(db: AsyncSession, plan_row: ExecutionPlan) -> None:
    """Mirror a plan-level approval gate onto the task timeline."""
    if not plan_row.task_id:
        return
    from packages.core.models.task import Task
    from packages.core.constants.task_actors import TaskActor
    from packages.core.services.task_service import add_task_log
    from packages.core.services.task_state_machine import (
        TaskStatusTransitionError,
        apply_task_status_transition,
    )

    task = (await db.execute(
        select(Task).where(Task.id == plan_row.task_id, Task.entity_id == plan_row.entity_id)
    )).scalar_one_or_none()
    if not task:
        return

    if task.status != TaskStatus.WAITING_ON_CUSTOMER:
        try:
            await apply_task_status_transition(task, "waiting_on_customer", db=db)
        except TaskStatusTransitionError:
            # Approval visibility should not make plan creation fail for
            # uncommon task states; the log still gives operators a path.
            pass

    approval_steps = list((await db.execute(
        select(ExecutionStep).where(
            ExecutionStep.plan_id == plan_row.id,
            ExecutionStep.requires_approval == True,  # noqa: E712
        ).order_by(ExecutionStep.created_at)
    )).scalars().all())
    step_labels = [
        step.step_key.replace("_", " ")
        for step in approval_steps
    ] or ["approval-gated step"]
    await add_task_log(
        db,
        plan_row.task_id,
        TaskLogType.AI_HITL_REQUESTED,
        (
            "This execution plan is waiting for approval before it can start.\n\n"
            f"Approval-gated step(s): {', '.join(step_labels)}.\n\n"
            "Approve the plan to dispatch execution, or revise the task/runtime rules."
        ),
        actor=TaskActor.SYSTEM,
        created_by="system",
        metadata={
            "plan_id": plan_row.id,
            "verdict": "pending_approval",
            "approval_required": True,
            "step_ids": [step.id for step in approval_steps],
            "capability_ids": sorted({step.capability_id for step in approval_steps if step.capability_id}),
        },
    )


async def materialize_plan_steps(
    db: AsyncSession,
    plan_id: str,
    plan: Plan,
    *,
    coordinator=None,
) -> list[ExecutionStep]:
    """Insert one ExecutionStep row per node in the plan, in topo order.

    Idempotent on re-run only if no steps yet exist for the plan;
    callers should treat this as a one-shot at plan creation.
    """
    plan_row = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    )).scalar_one()
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
        assert_no_server_owned_plan_params,
        coordinator_child_idempotency_key,
        coordinator_child_step_key,
        is_generated_coordinator_plan,
        validate_coordinator_source_plan,
    )

    dispatcher_state = (
        plan_row.dispatcher_state
        if isinstance(plan_row.dispatcher_state, dict) else {}
    )
    version = dispatcher_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY)
    if version is not None and version != TASK_COORDINATOR_PLAN_VERSION:
        raise ValueError("unknown Task coordinator Plan version")
    task_coordinator_v3 = version == TASK_COORDINATOR_PLAN_VERSION
    generated_coordinator = is_generated_coordinator_plan(plan)
    if generated_coordinator and task_coordinator_v3:
        raise ValueError("Task coordinator Plan cannot contain a V2 coordinator root")
    try:
        assert_no_server_owned_plan_params(plan)
    except ValueError:
        # Re-materialisation of a persisted coordinator is valid only for the
        # same strict single-parent shape. Child/runtime markers are never
        # accepted through Plan materialisation.
        if not generated_coordinator:
            raise

    coordinator_bindings: dict[str, Any] = {}
    if task_coordinator_v3:
        from packages.core.models.task_coordinator import TaskCoordinatorRun

        run_id = str(dispatcher_state.get(TASK_COORDINATOR_RUN_ID_KEY) or "")
        run = (await db.execute(
            select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == run_id)
        )).scalar_one_or_none()
        if (
            run is None or not plan_row.task_id
            or run.task_id != plan_row.task_id
            or run.entity_id != plan_row.entity_id
            or run.workspace_id != plan_row.workspace_id
            or run.current_plan_id != plan_row.id
        ):
            raise ValueError("Task coordinator Plan has no matching current Run")

    if (generated_coordinator or task_coordinator_v3) and plan_row.task_id:
        from packages.core.models.task import Task
        from packages.core.services.task_execution import resolve_task_execution

        task = (await db.execute(
            select(Task).where(
                Task.id == plan_row.task_id,
                Task.entity_id == plan_row.entity_id,
                Task.workspace_id == plan_row.workspace_id,
            )
        )).scalar_one_or_none()
        if task is None:
            raise ValueError("coordinator Task disappeared before materialization")
        if coordinator is None:
            execution = await resolve_task_execution(db, task)
            coordinator = execution.coordinator
            if (
                execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
                or coordinator is None
            ):
                raise ValueError(
                    f"Task runtime {execution.runtime_kind.value!r} cannot "
                    "materialize a coordinator Plan"
                )
        coordinator_bindings = {
            binding.service_key: binding
            for binding in coordinator.bindings
        }
        if task_coordinator_v3:
            if (
                run.root_subscription_id != coordinator.root.subscription_id
                or run.root_agent_id != coordinator.root.agent_id
                or run.selection_fingerprint != coordinator.stable_fingerprint()
            ):
                raise ValueError("Task coordinator Plan factory selection changed")
            validate_coordinator_source_plan(plan, coordinator=coordinator)
        else:
            required_services = {
                str(value).strip()
                for value in (
                    plan.steps[0].params.get("delegate_services") or []
                )
                if str(value or "").strip()
            }
            required_services.add(str(plan.steps[0].service_key or "").strip())
            required_services.discard("")
            if (
                str(plan.steps[0].service_key or "").strip()
                != coordinator.root.service_key
                or not required_services.issubset(coordinator_bindings)
            ):
                raise ValueError(
                    "coordinator services exceed the active Task subscription scope"
                )
    workspace_retry_config: dict = {}
    if plan_row.workspace_id:
        from packages.core.models.workspace import Workspace
        from packages.core.services.retry_policy import workspace_retry_policy_config

        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == plan_row.workspace_id)
        )).scalar_one_or_none()
        workspace_retry_config = workspace_retry_policy_config(getattr(workspace, "settings", None))

    from packages.core.services.retry_policy import (
        merge_retry_policy_configs,
        plan_retry_policy_config,
        step_retry_policy_config,
    )
    plan_retry_config = plan_retry_policy_config(plan_row)

    ordered = plan.topo_order()
    shape_by_key = _resolve_output_shapes(ordered)

    # A task-authored expected-output schema is not a Planner guess.  Bind it
    # to the one terminal agent step so the worker sees it on attempt 1 and the
    # dispatcher validates it before declaring that step done.
    task_output_step_key: str | None = None
    task_output_contract_schema: dict | None = None
    if plan_row.task_id:
        from packages.core.contracts.task_output import (
            task_output_envelope_schema,
            terminal_agent_step_key,
        )
        from packages.core.models.task import Task

        task_expected_output = (
            await db.execute(select(Task.expected_output).where(Task.id == plan_row.task_id))
        ).scalar_one_or_none()
        task_output_contract_schema = task_output_envelope_schema(task_expected_output)
        if task_output_contract_schema is not None:
            task_output_step_key = terminal_agent_step_key(ordered)
            if task_output_step_key is None:
                logger.warning(
                    "plan %s has a structured task expected_output but not exactly "
                    "one terminal llm/subagent step; hard output contract was not bound",
                    plan_row.id,
                )

    rows: list[ExecutionStep] = []
    for ps in ordered:
        step_preview = type("_StepPreview", (), {"params": ps.params})()
        policy = merge_retry_policy_configs(
            ("workspace", workspace_retry_config),
            ("plan", plan_retry_config),
            ("step", step_retry_policy_config(step_preview)),
        )
        rows.append(
            _step_from_pydantic(
                plan_row,
                ps,
                max_attempts=policy.max_attempts,
                output_shape=shape_by_key.get(ps.key),
                task_output_contract_schema=(task_output_contract_schema if ps.key == task_output_step_key else None),
            )
        )

    if task_coordinator_v3:
        for row in rows:
            if row.service_key:
                binding = coordinator_bindings.get(str(row.service_key))
                if binding is None:
                    raise ValueError(
                        f"Task coordinator step {row.step_key!r} has no exact "
                        "active service binding"
                    )
                row.resolved_subscription_id = binding.subscription_id
                row.resolved_agent_id = binding.agent_id
            # Only the Task loop can release a source Step. The Dispatcher
            # never leases PAUSED rows, and no model-authored argument becomes
            # a release marker inside an action's input schema.
            row.step_status = ExecutionStepStatus.PAUSED.value

    if generated_coordinator:
        root = rows[0]
        root_binding = coordinator_bindings.get(str(root.service_key or ""))
        if coordinator_bindings and root_binding is None:
            raise ValueError("coordinator root service binding is unavailable")
        if root_binding is not None:
            root.resolved_subscription_id = root_binding.subscription_id
            root.resolved_agent_id = root_binding.agent_id
            plan_row.agent_subscription_id = root_binding.subscription_id
        root.params = {
            **deepcopy(root.params if isinstance(root.params, dict) else {}),
            # ``None`` means the root is runnable and is not parked for a
            # particular child. The dispatcher replaces it with the exact
            # child identity only while the coordinator is suspended.
            COORDINATOR_WAITING_CHILD_PARAM_KEY: None,
        }
        checklist = plan.steps[0].params.get("checklist") or []
        source_steps = [_coordinator_source_step(item) for item in checklist]
        # Revalidate the embedded source DAG at the persistence boundary. The
        # generated-plan predicate proves ownership/shape; Plan validation
        # additionally proves every dependency and cycle before rows exist.
        Plan(steps=source_steps).topo_order()
        child_key_by_source = {
            source.key: coordinator_child_step_key(root.step_key, source.key)
            for source in source_steps
        }
        if len(set(child_key_by_source.values())) != len(child_key_by_source):
            raise ValueError("coordinator child step keys are not unique")
        child_shape_by_key = _resolve_output_shapes(source_steps)

        for source, source_contract in zip(source_steps, checklist, strict=True):
            # Dormant children carry the immutable source contract, not live
            # executor arguments. The coordinator release transition resolves
            # its refs under the parent/child locks, writes those resolved
            # arguments, and only then marks the child PENDING.
            child_params = {
                "source_contract": deepcopy(source_contract),
                COORDINATOR_CHILD_PARAM_KEY: True,
                COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
                COORDINATOR_CHILD_RELEASED_PARAM_KEY: False,
                COORDINATOR_PARENT_STEP_PARAM_KEY: root.step_key,
                COORDINATOR_CHILD_ITEM_PARAM_KEY: source.key,
                COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: (
                    coordinator_child_idempotency_key(
                        plan_row.id,
                        root.step_key,
                        source.key,
                    )
                ),
                ACTION_EFFECT_GENERATION_PARAM_KEY: 0,
            }
            child_step = source.model_copy(update={
                "key": child_key_by_source[source.key],
                "params": child_params,
                "depends_on": [
                    child_key_by_source[dependency]
                    for dependency in source.depends_on
                ],
            })
            step_preview = type("_StepPreview", (), {"params": source.params})()
            policy = merge_retry_policy_configs(
                ("workspace", workspace_retry_config),
                ("plan", plan_retry_config),
                ("step", step_retry_policy_config(step_preview)),
            )
            child_row = _step_from_pydantic(
                plan_row,
                child_step,
                max_attempts=policy.max_attempts,
                output_shape=child_shape_by_key.get(source.key),
            )
            child_binding = coordinator_bindings.get(
                str(child_row.service_key or "")
            )
            if child_row.service_key and child_binding is None and coordinator_bindings:
                raise ValueError(
                    f"coordinator child service {child_row.service_key!r} "
                    "has no exact active subscription binding"
                )
            if child_binding is not None:
                child_row.resolved_subscription_id = child_binding.subscription_id
                child_row.resolved_agent_id = child_binding.agent_id
            child_row.step_status = ExecutionStepStatus.PAUSED.value
            rows.append(child_row)

    db.add_all(rows)
    await db.flush()
    return rows


def _coordinator_source_step(item: Any) -> PlanStep:
    """Restore one server-generated checklist item to its typed source step."""

    if not isinstance(item, dict):
        raise ValueError("coordinator checklist items must be objects")
    item_key = str(item.get("delegation_key") or item.get("key") or "").strip()
    params = deepcopy(item.get("params")) if isinstance(item.get("params"), dict) else {}
    kind = str(item.get("kind") or "").strip()
    if kind in {"llm", "subagent"} and not params.get("prompt") and item.get("prompt"):
        # Compatibility with the first generated coordinator payload, which
        # carried the agent prompt beside (rather than only inside) params.
        params["prompt"] = str(item["prompt"])
    return PlanStep.model_validate({
        "key": item_key,
        "kind": kind,
        "service_key": item.get("service_key"),
        "provider": item.get("provider"),
        "action_key": item.get("action_key"),
        "capability_id": item.get("capability_id"),
        "integration_id": item.get("integration_id"),
        "params": params,
        "expected_input_schema": deepcopy(item.get("expected_input_schema")),
        "expected_output_schema": deepcopy(item.get("expected_output_schema")),
        "output_shape": item.get("output_shape"),
        "execution_contract": deepcopy(item.get("execution_contract")),
        "depends_on": list(item.get("depends_on") or []),
        "risk_level": item.get("risk_level") or "low",
        "requires_approval": bool(item.get("requires_approval")),
        "max_attempts": item.get("max_attempts") or 3,
        "expects": list(item.get("expects") or []) or None,
        "description": item.get("description"),
    })


class PlanContractError(Exception):
    """A plan has unresolvable contract gaps (dangling refs / unshaped
    producers) that survived auto-repair. Such a plan must not be dispatched —
    it would die at runtime as ReferenceError/OutputSchemaError."""

    def __init__(self, gaps: list) -> None:
        self.gaps = list(gaps)
        detail = "; ".join(
            f"{getattr(g, 'step_key', '?')}: {getattr(g, 'detail', g)}" for g in self.gaps
        )
        super().__init__(detail or "plan has unresolvable contract gaps")


def _linker_lite_steps(steps: list[PlanStep]) -> list[dict]:
    """Project plan steps onto the contract fields the linker reads."""
    from packages.core.plans.refs import extract_step_ref_paths, extract_step_refs

    linked: list[dict] = []
    for s in steps:
        params = s.params if isinstance(s.params, dict) else {}
        # A generated coordinator's checklist is a server-owned embedded DAG,
        # not input consumed by the root itself. Its exact refs are validated
        # before folding and again when typed children are materialised.
        linker_params = (
            {"prompt": params.get("prompt")}
            if params.get(COORDINATOR_PARAM_KEY) is True
            else params
        )
        linked.append({
            "key": s.key,
            "kind": s.kind,
            "output_shape": s.output_shape,
            # Agent receipt fields are downgraded from required during
            # materialization because only runtime evidence can prove them.
            # Link against that same effective schema; otherwise planning can
            # promise a downstream ref that the persisted contract explicitly
            # allows the producer to omit.
            "expected_output_schema": (
                _defang_agent_payload_schema(s.expected_output_schema)
                if s.kind in ("llm", "subagent")
                else s.expected_output_schema
            ),
            "input_refs": extract_step_refs(linker_params),
            "input_ref_paths": extract_step_ref_paths(linker_params),
        })
    return linked


def plan_contract_gaps(
    steps: list[PlanStep],
    *,
    task_expected_output: Any = None,
    require_explicit_agent_outputs: bool = False,
    require_explicit_execution_contract: bool = False,
) -> list:
    """Return the contract linker's unfixable gaps (``LinkIssue`` list) for a
    plan, after auto-repair. Empty list means the plan is contract-clean (every
    consumed value is producible, every produced value is shaped). Pure — no
    side effects; callers decide whether to enforce."""
    from packages.core.contracts.linker import LinkIssue, repair_plan

    _repaired, remaining = repair_plan(_linker_lite_steps(steps))
    gaps = list(remaining)

    from packages.core.contracts.step_execution import StepDelivery, StepEffects

    for step in steps:
        contract = step.execution_contract
        if contract is None:
            if require_explicit_execution_contract and step.kind in {"llm", "subagent"}:
                gaps.append(LinkIssue(
                    "missing_execution_contract", step.key,
                    "new agent Steps must declare execution_contract.delivery and "
                    "execution_contract.effects separately from their result payload; "
                    "declare only this Step's work, not the whole Task's delivery",
                ))
            continue
        if contract.delivery is StepDelivery.INLINE and "files" in (step.expects or []):
            gaps.append(LinkIssue(
                "conflicting_delivery_contract", step.key,
                "execution_contract.delivery=inline conflicts with expects=['files']; "
                "use artifact delivery for a required file, or remove the file expectation "
                "from a genuinely inline intermediate Step",
            ))
        if contract.effects is not StepEffects.AUTHORIZED and "publish" in (step.expects or []):
            gaps.append(LinkIssue(
                "conflicting_effect_contract", step.key,
                "expects=['publish'] requires execution_contract.effects=authorized; "
                "read_only/artifact_write cannot publish or send external content",
            ))
        if step.kind == "llm" and contract.effects is not StepEffects.READ_ONLY:
            gaps.append(LinkIssue(
                "conflicting_effect_contract", step.key,
                "text-only llm Steps require execution_contract.effects=read_only; "
                "use a tool-capable subagent for permitted mutations",
            ))

    for step in steps:
        if step.kind == "llm" and "publish" in (step.expects or []):
            gaps.append(LinkIssue(
                "unexecutable_publish", step.key,
                "A text-only llm step cannot publish. Use a tool-capable subagent "
                "with captured publish evidence or an available integration action.",
            ))
        if step.kind == "llm" and "files" in (step.expects or []):
            gaps.append(LinkIssue(
                "unexecutable_file_creation", step.key,
                "A text-only llm step cannot create a file. Use a subagent with "
                "the selected Agent's file tools and captured artifact evidence.",
            ))
        if step.kind == "llm" and step.params.get("skill_refs"):
            gaps.append(LinkIssue(
                "unexecutable_skill_invocation", step.key,
                "A text-only llm step cannot invoke a Skill. Use a subagent "
                "with the selected Agent's Skill configuration.",
            ))

    from packages.core.contracts.task_output import (
        task_expected_output_declares_schema,
        task_expected_output_json_schema,
        terminal_agent_step_key,
        is_concrete_output_contract_schema,
        validate_schema_contract,
    )

    # Every persisted schema is part of the execution contract, regardless
    # of producer kind or whether this is a new-plan strict pass.  In
    # particular, ``Draft202012Validator.check_schema`` accepts a dangling
    # local ``$ref`` and jsonschema only raises when a lease validates a
    # value.  Keep the same fail-closed validation on retry/migration paths,
    # which intentionally call this function without strict agent-output
    # enforcement.
    for step in steps:
        if step.expected_output_schema is None:
            continue
        try:
            validate_schema_contract(step.expected_output_schema)
        except Exception as exc:  # noqa: BLE001
            gaps.append(
                LinkIssue(
                    "invalid_output_contract",
                    step.key,
                    f"invalid expected_output_schema: {exc}",
                )
            )

    task_schema_declared = task_expected_output_json_schema(task_expected_output) is not None
    if (
        task_expected_output_declares_schema(task_expected_output)
        and task_schema_declared is False
    ):
        gaps.append(
            LinkIssue(
                "invalid_task_output_contract",
                "task_expected_output",
                "Task.expected_output contains an invalid or unresolved JSON Schema",
            )
        )
    task_terminal_key = terminal_agent_step_key(steps) if task_schema_declared else None
    if task_schema_declared and task_terminal_key is None:
        gaps.append(
            LinkIssue(
                "task_output_contract",
                "task_expected_output",
                (
                    "structured Task.expected_output requires exactly one "
                    "terminal llm/subagent deliverable step"
                ),
            )
        )

    if require_explicit_agent_outputs:
        from packages.core.contracts.shapes import get_shape

        for step in steps:
            if step.kind not in ("llm", "subagent") or step.key == task_terminal_key:
                continue
            has_shape = bool(step.output_shape)
            has_schema = isinstance(step.expected_output_schema, dict)
            if has_shape and has_schema:
                gaps.append(
                    LinkIssue(
                        "conflicting_output_contract",
                        step.key,
                        (
                            "declare exactly one agent output contract: either a canonical "
                            "output_shape or an exact expected_output_schema"
                        ),
                    )
                )
                continue
            if not has_shape and not has_schema:
                gaps.append(
                    LinkIssue(
                        "missing_explicit_output_contract",
                        step.key,
                        (
                            "new llm/subagent plans must declare an exact "
                            "expected_output_schema or canonical output_shape before "
                            "execution; Runtime must not guess a step contract"
                        ),
                    )
                )
                continue
            if has_schema:
                if not is_concrete_output_contract_schema(step.expected_output_schema):
                    gaps.append(
                        LinkIssue(
                            "empty_output_contract",
                            step.key,
                            "expected_output_schema must constrain a concrete payload; "
                            "an empty/open schema is not an explicit output contract",
                        )
                    )
                continue
            if step.output_shape == "StepResult":
                gaps.append(
                    LinkIssue(
                        "generic_output_contract",
                        step.key,
                        (
                            "StepResult is a legacy generic envelope, not an explicit "
                            "payload contract; choose a concrete canonical output_shape"
                        ),
                    )
                )
                continue
            try:
                get_shape(step.output_shape)
            except KeyError:
                gaps.append(
                    LinkIssue(
                        "unknown_output_contract",
                        step.key,
                        f"unknown canonical output_shape {step.output_shape!r}",
                    )
                )
    return gaps


def persisted_plan_contract_gaps(
    plan_dag: object,
    *,
    task_expected_output: Any = None,
) -> list:
    """Lint a JSONB plan snapshot before re-dispatching it.

    A plan may have been materialized by an older planner version whose
    references used the StepResult ``outputs`` wrapper even though its
    producer now has a bare explicit payload.  Retry paths must not blindly
    replay that snapshot.  Legacy unshaped agent steps remain compatible via
    ``repair_plan``'s StepResult inference; only actual unresolved contracts
    are returned here.
    """
    from packages.core.contracts.linker import LinkIssue

    try:
        snapshot = Plan.model_validate(plan_dag)
    except Exception as exc:  # noqa: BLE001
        return [
            LinkIssue(
                "invalid_plan",
                "plan",
                f"persisted plan snapshot is invalid and must be replanned: {exc}",
            )
        ]
    # A pre-contract plan with no producer declaration was materialized as the
    # legacy StepResult envelope. Do not let the current repair heuristic infer
    # a new bare canonical shape from a direct field reference while auditing
    # that persisted snapshot; its stored execution step still produces
    # ``result.outputs.*``.
    legacy_steps = [
        step.model_copy(update={"output_shape": "StepResult"})
        if (
            step.kind in ("llm", "subagent")
            and not step.output_shape
            and step.expected_output_schema is None
        )
        else step
        for step in snapshot.topo_order()
    ]
    gaps = plan_contract_gaps(
        legacy_steps,
        task_expected_output=task_expected_output,
        require_explicit_agent_outputs=False,
    )
    # A structured Task.expected_output introduced after a legacy plan was
    # persisted is not represented by the old DAG's generic StepResult row.
    # Replaying that snapshot would silently drop ``outputs.data`` and defeat
    # the hard task contract. Only a materialized task-envelope marker proves
    # the persisted DAG already carries the binding; otherwise force a fresh
    # planner/materialization pass.
    from packages.core.contracts.task_output import (
        OutputContractKind,
        terminal_agent_step_key,
        task_expected_output_json_schema,
        output_contract_for_schema,
    )

    if task_expected_output_json_schema(task_expected_output) is not None:
        terminal_key = terminal_agent_step_key(legacy_steps)
        has_task_envelope = any(
            step.key == terminal_key
            and output_contract_for_schema(step.expected_output_schema).kind
            is OutputContractKind.TASK_ENVELOPE
            for step in legacy_steps
        )
        if not has_task_envelope:
            from packages.core.contracts.linker import LinkIssue

            gaps.append(
                LinkIssue(
                    "task_output_contract",
                    "task_expected_output",
                    (
                        "persisted plan lacks the task-output envelope binding; "
                        "replan before retrying so outputs.data is enforced"
                    ),
                )
            )
    return gaps


def _resolve_output_shapes(steps: list[PlanStep]) -> dict[str, str]:
    """Run the plan-time linker over the steps: auto-repair missing output
    shapes and report unfixable I/O gaps. Returns ``{step_key: shape_name}``
    for steps that resolved to a canonical shape (explicit or inferred)."""
    from packages.core.contracts.linker import repair_plan

    repaired, remaining = repair_plan(_linker_lite_steps(steps))
    for issue in remaining:
        logger.warning(
            "plan-time contract gap: step=%s kind=%s detail=%s",
            issue.step_key, issue.kind, issue.detail,
        )
    return {s["key"]: s["output_shape"] for s in repaired if s.get("output_shape")}


# Platform-receipt fields the Planner keeps demanding from free-form steps.
# The ID-trap: an llm/subagent execution may legitimately have no such value
# (nothing was published, or the id lives in a tool response the model never
# echoes), so requiring one guarantees OutputSchemaError — receipts come from
# runtime evidence, not the model. Stripped from "required" only; the
# properties stay so a value that does surface still validates its type.
_RECEIPT_FIELDS = frozenset({
    "tweet_id", "urn", "post_urn", "tweet_url", "post_url", "share_url",
    "url", "published_at", "platform", "post_id", "message_id",
})


def _strip_receipt_required_fields(schema: dict | None) -> dict | None:
    if not isinstance(schema, dict) or not isinstance(schema.get("required"), list):
        return schema
    kept = [f for f in schema["required"] if str(f) not in _RECEIPT_FIELDS]
    if len(kept) == len(schema["required"]):
        return schema
    out = dict(schema)
    out["required"] = kept
    return out


def _strip_array_cardinality(schema: Any) -> Any:
    """Relax record-count promises in every array of an agent payload schema.

    How many records a step finds is a fact the run discovers, not a promise
    the Planner can make: ``prospects: {minItems: 25, maxItems: 25}`` next to
    "mark unknown fields as null rather than inventing" left the model no
    schema-valid answer except invented rows (staging, 2026-09-11). ``maxItems``
    is dropped and ``minItems`` is clamped to at most 1: "at least one" is the
    guarantee the linker needs to prove a ``files[0]`` reference and a step
    that found nothing reports failed/partial honestly, while "at least 25"
    can only be met by invention. The count target stays in the prompt and is
    reported through the envelope's ``progress``; the contract keeps the
    fields and their requiredness. Returns the input object untouched when
    nothing needs relaxing.
    """
    if isinstance(schema, list):
        stripped = [_strip_array_cardinality(item) for item in schema]
        return stripped if any(a is not b for a, b in zip(stripped, schema)) else schema
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] | None = None
    for key, value in schema.items():
        if key == "maxItems":
            out = dict(schema) if out is None else out
            out.pop(key, None)
            continue
        if key == "minItems" and isinstance(value, int) and value > 1:
            out = dict(schema) if out is None else out
            out[key] = 1
            continue
        replacement = _strip_array_cardinality(value)
        if replacement is not value:
            out = dict(schema) if out is None else out
            out[key] = replacement
    return schema if out is None else out


def _defang_agent_payload_schema(schema: dict | None) -> dict | None:
    """Remove the promises an llm/subagent step cannot keep by construction."""
    return _strip_array_cardinality(_strip_receipt_required_fields(schema))


def _expected_output_schema_for_plan_step(
    ps: PlanStep,
    *,
    output_shape: str | None = None,
    task_output_contract_schema: dict | None = None,
) -> dict | None:
    """Return the one effective output contract materialization must persist."""
    # When a step resolves to a canonical shape (declared or linker-inferred),
    # derive expected_output_schema from the shape so producer/normalizer/
    # validator share one vocabulary. For free-form kinds (llm/subagent) the
    # canonical shape WINS over a hand-written schema — the Planner's guessed
    # schema (e.g. {text} on a step that returns {drafts:[...]}) is exactly the
    # OutputSchemaError source. Structured kinds keep an explicit schema: it's a
    # real contract with an external system, not a guess.
    from packages.core.contracts.task_output import (
        OutputContractKind,
        PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE,
        output_contract_for_schema,
        plan_output_contract_schema,
    )

    expected_output_schema = ps.expected_output_schema
    shape_schema_applied = False
    embedded_task_contract = (
        ps.expected_output_schema
        if output_contract_for_schema(ps.expected_output_schema).kind
        is OutputContractKind.TASK_ENVELOPE
        else None
    )
    if (task_output_contract_schema is not None or embedded_task_contract is not None) and ps.kind in (
        "llm",
        "subagent",
    ):
        # Runtime-authored from Task.expected_output: this is the final
        # deliverable contract, so it wins over both the generic StepResult
        # shape and any Planner-authored custom schema.
        expected_output_schema = task_output_contract_schema or embedded_task_contract
        shape_schema_applied = True
    elif getattr(ps, "output_shape", None) and output_shape:
        from packages.core.contracts.shapes import get_shape
        try:
            shape_schema = get_shape(output_shape).json_schema()
        except KeyError:
            shape_schema = None
        if shape_schema is not None and (
            expected_output_schema is None or ps.kind in ("llm", "subagent")
        ):
            expected_output_schema = shape_schema
            if (
                ps.kind in ("llm", "subagent")
                and ps.output_shape
                and ps.output_shape != "StepResult"
            ):
                expected_output_schema = plan_output_contract_schema(shape_schema)
            shape_schema_applied = True
    elif ps.expected_output_schema is not None and ps.kind in ("llm", "subagent"):
        # A new plan explicitly authored this step payload. Deterministically
        # remove provider receipts that only runtime evidence can supply, then
        # provenance-mark the remaining payload before materialization. Only
        # already-materialized legacy schemas remain unmarked/advisory.
        expected_output_schema = plan_output_contract_schema(
            _defang_agent_payload_schema(ps.expected_output_schema),
            source=PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE,
        )
        shape_schema_applied = True
    elif output_shape and (expected_output_schema is None or ps.kind in ("llm", "subagent")):
        # Linker-inferred shape for a legacy plan. It can keep old steps
        # executable, but it must never override a schema the Planner actually
        # declared and it is intentionally not provenance-marked as hard.
        from packages.core.contracts.shapes import get_shape
        try:
            expected_output_schema = get_shape(output_shape).json_schema()
        except KeyError:
            expected_output_schema = None
        shape_schema_applied = expected_output_schema is not None
    if not shape_schema_applied and ps.kind in ("llm", "subagent"):
        # A planner-authored custom schema survived (no shape resolved):
        # de-fang its receipt requirements before it can OutputSchemaError.
        expected_output_schema = _defang_agent_payload_schema(expected_output_schema)
    return expected_output_schema


def _step_from_pydantic(
    plan_row: ExecutionPlan,
    ps: PlanStep,
    *,
    max_attempts: int | None = None,
    output_shape: str | None = None,
    task_output_contract_schema: dict | None = None,
) -> ExecutionStep:
    expected_output_schema = _expected_output_schema_for_plan_step(
        ps,
        output_shape=output_shape,
        task_output_contract_schema=task_output_contract_schema,
    )
    params = dict(ps.params)
    from packages.core.contracts.step_execution import STEP_EXECUTION_CONTRACT_PARAM

    # Only the validated top-level declaration becomes execution context;
    # model-authored params may not impersonate materializer-owned state.
    params.pop(STEP_EXECUTION_CONTRACT_PARAM, None)
    execution_contract = getattr(ps, "execution_contract", None)
    if execution_contract is not None:
        params[STEP_EXECUTION_CONTRACT_PARAM] = execution_contract.model_dump(mode="json")
    return ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_row.id,
        entity_id=plan_row.entity_id,
        workspace_id=plan_row.workspace_id,
        step_key=ps.key,
        kind=ps.kind,
        service_key=ps.service_key,
        provider=ps.provider,
        action_key=ps.action_key,
        capability_id=ps.capability_id,
        integration_id=ps.integration_id,
        params=params,
        expected_input_schema=ps.expected_input_schema,
        expected_output_schema=expected_output_schema,
        depends_on=list(ps.depends_on),
        step_status=ExecutionStepStatus.PENDING.value,
        risk_level=ps.risk_level,
        requires_approval=ps.requires_approval,
        max_attempts=max_attempts or ps.max_attempts,
    )


# ── Reads ─────────────────────────────────────────────────────────────

async def get_plan(
    db: AsyncSession, plan_id: str,
    *, entity_id: Optional[str] = None,
) -> Optional[ExecutionPlan]:
    stmt = select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    if entity_id is not None:
        stmt = stmt.where(ExecutionPlan.entity_id == entity_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_step(db: AsyncSession, step_id: str) -> Optional[ExecutionStep]:
    return (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()


async def list_plan_steps(
    db: AsyncSession, plan_id: str,
) -> list[ExecutionStep]:
    return list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan_id)
        .order_by(ExecutionStep.created_at)
    )).scalars().all())


# ── Lifecycle ────────────────────────────────────────────────────────

async def cancel_plan(
    db: AsyncSession, plan_id: str,
    *, reason: Optional[str] = None,
) -> Optional[ExecutionPlan]:
    plan = (await db.execute(
        select(ExecutionPlan)
        .where(ExecutionPlan.id == plan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not plan or plan.status in (ExecutionPlanStatus.COMPLETED, ExecutionPlanStatus.CANCELLED, ExecutionPlanStatus.FAILED,):
        return plan

    now = datetime.now(timezone.utc)
    plan.status = ExecutionPlanStatus.CANCELLED.value
    plan.completed_at = now
    if reason:
        plan.last_error = {"cancelled": reason}

    # Plan -> Lease -> Step is the same lock suffix used by worker reports.
    # Revoke every live/suspended lease and clear its claim/credentials before
    # the corresponding row becomes terminal. Include PAUSED coordinator
    # roots and dormant children so a parked fold cannot survive Plan cancel.
    leases, steps = await lock_plan_leases_then_steps(db, plan_id=plan_id)
    revoke_leases_before_cancelling_steps(
        db,
        leases=leases,
        steps=steps,
        step_ids={step.id for step in steps},
        reason=reason or "plan cancelled",
        now=now,
        clear_runtime_checkpoint=False,
    )

    await db.flush()
    return plan
