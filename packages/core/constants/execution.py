"""Execution defaults shared by agent and scheduler runtimes."""

from enum import Enum
from typing import TypeVar

DEFAULT_AGENT_MAX_TURNS = 50



# ── Execution status vocabularies ──
# Derived from the actual write sites (grep `step_status = "` / `plan.status
# = "` across packages/core and apps/api) — every member below is written by
# real code, and nothing below is invented. These exist so code BRANCHES on
# enum members instead of matching keyword strings; a new status is a new
# member here, not a new literal scattered through the executor.

class ExecutionStepStatus(str, Enum):
    """Every state an ExecutionStep row takes."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    WAITING_HUMAN = "waiting_human"

    #: Held at a governance gate. Written by the blueprint simulation and
    #: rendered (and offered a retry) by the execution timeline, so it is
    #: part of the vocabulary even though the executor never assigns it.
    PAUSED = "paused"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class ExecutionPlanStatus(str, Enum):
    """Every state an ExecutionPlan row takes."""

    DRAFT = "draft"
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNED = "replanned"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class WorkLeaseStatus(str, Enum):
    """Every state a WorkLease row takes (derived from write sites)."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    NEEDS_HUMAN = "needs_human"
    # A coordinator root checkpointed its pending child tool call and gave
    # the worker slot back.  The lease is terminal; resume uses a fresh lease.
    SUSPENDED = "suspended"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class ScheduledDispatchKind(str, Enum):
    """Celery entrypoints supported by the durable scheduler handoff."""

    SCHEDULER_PARENT = "scheduler_parent"
    AGENT_TASK = "agent_task"
    WORKFLOW = "workflow"
    GOAL_MEASUREMENT = "goal_measurement"
    WORKSPACE_STAT_COLLECTION = "workspace_stat_collection"
    STRATEGIST_REVIEW = "strategist_review"
    MORNING_BRIEFING = "morning_briefing"
    OUTCOME_EVALUATION = "outcome_evaluation"
    CHAT_INSIGHT_EXTRACTION = "chat_insight_extraction"


class ScheduledDispatchState(str, Enum):
    """Persistence state of a scheduler-to-Celery handoff."""

    PREPARED = "prepared"
    PUBLISHED = "published"
    QUARANTINED = "quarantined"


class ScheduledDispatchRecoveryAction(str, Enum):
    """Safe action for recovering one persisted scheduler handoff."""

    REPUBLISH_AND_PROJECT = "republish_and_project"
    REPUBLISH_EXECUTION = "republish_execution"
    PROJECT_ONLY = "project_only"
    QUARANTINE_AMBIGUOUS = "quarantine_ambiguous_execution"


class ScheduledRecoveryKind(str, Enum):
    """Independent durable recovery phases on one ScheduledJobRun."""

    RESULT_PROJECTION = "result_projection"
    SETTLEMENT = "settlement"
    DISPATCH = "dispatch"
    EXECUTION = "execution"


class ScheduledRunStatus(str, Enum):
    """Every durable status written to a ScheduledJobRun."""

    RUNNING = "running"
    SUCCESS = "success"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ERROR = "error"


class ScheduledChildExecutionState(str, Enum):
    """Business-execution phase stored beside the dispatch handoff."""

    ADMITTED = "admitted"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED = "settled"


class ScheduledResultProjectionKind(str, Enum):
    """User-visible projections produced from terminal scheduled results."""

    WORKSPACE_CHAT = "workspace_chat"


class ScheduledResultProjectionState(str, Enum):
    """Durable delivery state for a scheduled result projection."""

    PENDING = "pending"
    DELIVERED = "delivered"
    QUARANTINED = "quarantined"


class ScheduledJobSkillGenerationStatus(str, Enum):
    """Operator-visible state of a ScheduledJob Skill generation intent."""

    IDLE = "idle"
    PENDING = "pending"
    PAUSED = "paused"
    FAILED = "failed"


SCHEDULED_RECOVERY_RETRY_SECONDS = 60
SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS = 665
SCHEDULED_EXECUTION_RECOVERY_HEADER = "manor_scheduled_execution_recovery"
SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS = 1865
SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS = 3
SCHEDULED_SETTLEMENT_MAX_RETRIES = 12
SCHEDULED_DISPATCH_RECOVERY_MAX_RETRIES = 10
SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS = (
    SCHEDULED_SETTLEMENT_MAX_RETRIES + 2
) * SCHEDULED_RECOVERY_RETRY_SECONDS
SCHEDULED_DISPATCH_RECOVERY_DELAY_SECONDS = (
    SCHEDULED_DISPATCH_RECOVERY_MAX_RETRIES + 2
) * SCHEDULED_RECOVERY_RETRY_SECONDS
SCHEDULED_RESULT_PROJECTION_MAX_RETRIES = 12
SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS = 3
SCHEDULED_RESULT_PROJECTION_RETRY_SECONDS = SCHEDULED_RECOVERY_RETRY_SECONDS
# Keep the periodic recovery sweep behind one complete Celery retry chain.
SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS = (
    SCHEDULED_RESULT_PROJECTION_MAX_RETRIES + 2
) * SCHEDULED_RESULT_PROJECTION_RETRY_SECONDS
WORKFLOW_CONTINUATION_RETRY_SECONDS = 60
WORKFLOW_TERMINAL_EFFECT_RETRY_SECONDS = 60


class ScheduledChildAdmissionStatus(str, Enum):
    """Result of atomically admitting one queued scheduled child."""

    ADMITTED = "admitted"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    SETTLEMENT_PENDING = "settlement_pending"


class ScheduledSettlementKind(str, Enum):
    """Recovery entrypoint required to settle completed scheduled work."""

    GENERIC = "generic"
    AGENT_TASK = "agent_task"


class ExecutionClaimKind(str, Enum):
    """Shared namespaces for durable runtime execution fences."""

    WORKFLOW_RUN = "workflow-run"
    AGENT_TASK = "agent-task"
    TASK_PLAN = "task-plan"
    SCHEDULED_RUN = "scheduled-run"
    SCHEDULED_JOB_SKILL = "scheduled-job-skill"


class WorkerStatus(str, Enum):
    """Every state a Worker row takes.

    ``update_worker_status`` already validated against this exact set as an
    inline literal set — the vocabulary existed, it just wasn't a type, so
    the twenty-odd readers each spelled ``"active"`` themselves.
    """

    #: Registered but not yet paired with a running agent process.
    PAIRING = "pairing"

    #: Paired and eligible for leases.
    ACTIVE = "active"

    #: Temporarily withheld from dispatch by its owner.
    PAUSED = "paused"

    #: Not currently reachable (deregistered or lost its heartbeat).
    OFFLINE = "offline"

    #: Withdrawn by the dispatcher after repeated failures.
    QUARANTINED = "quarantined"

    #: Credentials withdrawn; never eligible again.
    REVOKED = "revoked"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: Statuses where the worker exists but cannot take work, and the operator
#: has to act (or its process has to come back) before it can.
WORKER_UNAVAILABLE_STATUSES: frozenset[WorkerStatus] = frozenset(
    {
        WorkerStatus.OFFLINE,
        WorkerStatus.QUARANTINED,
    }
)


# ── Groupings ──
# The questions code actually asks, answered once. A site that spells out
# ("done", "failed", "cancelled") is claiming to know the whole terminal set,
# and it stops being right the moment a state is added.

#: A step that will not run again.
STEP_TERMINAL_STATUSES: frozenset[ExecutionStepStatus] = frozenset(
    {
        ExecutionStepStatus.DONE,
        ExecutionStepStatus.FAILED,
        ExecutionStepStatus.SKIPPED,
        ExecutionStepStatus.CANCELLED,
    }
)

#: A step that did not produce its output — the executor's failure set.
STEP_UNSUCCESSFUL_STATUSES: frozenset[ExecutionStepStatus] = frozenset(
    {
        ExecutionStepStatus.FAILED,
        ExecutionStepStatus.SKIPPED,
        ExecutionStepStatus.CANCELLED,
    }
)

#: A step that has not finished — it is running, queued behind something,
#: or held for a person. Deliberately the complement of the terminal set:
#: readers ask "is this still open?", and a state that is neither open nor
#: terminal falls through every branch, which is how a paused step used to
#: vanish from the timeline.
STEP_OPEN_STATUSES: frozenset[ExecutionStepStatus] = frozenset(
    set(ExecutionStepStatus) - STEP_TERMINAL_STATUSES
)

#: A plan that has reached its end, whatever the outcome.
PLAN_TERMINAL_STATUSES: frozenset[ExecutionPlanStatus] = frozenset(
    {
        ExecutionPlanStatus.COMPLETED,
        ExecutionPlanStatus.FAILED,
        ExecutionPlanStatus.CANCELLED,
        ExecutionPlanStatus.REPLANNED,
    }
)

#: A plan the dispatcher still has work for.
PLAN_ACTIVE_STATUSES: frozenset[ExecutionPlanStatus] = frozenset(
    {
        ExecutionPlanStatus.PENDING,
        ExecutionPlanStatus.RUNNING,
    }
)


# ── Reported-status aliases ──
# Remote workers, CLI runners and the coding sandbox report a step's outcome
# in their own words: "complete" for done, "canceled" with one L. Call sites
# used to carry the variants inline — ``in {"done", "complete"}`` — which
# means every new reader had to know the folklore, and any reader that
# didn't silently treated a finished step as unfinished. The variants are
# data, and they live here.
_STEP_STATUS_ALIASES: dict[str, ExecutionStepStatus] = {
    "complete": ExecutionStepStatus.DONE,
    "completed": ExecutionStepStatus.DONE,
    "success": ExecutionStepStatus.DONE,
    "succeeded": ExecutionStepStatus.DONE,
    "canceled": ExecutionStepStatus.CANCELLED,
    "error": ExecutionStepStatus.FAILED,
}


def coerce_step_status(reported: object) -> ExecutionStepStatus | None:
    """The step status a worker meant, or ``None`` if it isn't one.

    ``None`` is deliberately distinct from FAILED: an unrecognised word is
    "no information", and treating it as a failure would fail steps over a
    spelling. Callers decide what missing information means for them.
    """
    text = str(reported or "").strip().lower()
    if not text:
        return None
    try:
        return ExecutionStepStatus(text)
    except ValueError:
        return _STEP_STATUS_ALIASES.get(text)


#: ``ExecutionStep.error["type"]`` written by the plan executor when a step
#: that declares an external publish (``expects: ["publish"]`` or
#: ``output_shape: PublishResult``) is held at the step boundary because a
#: dependency landed a ``partial`` StepResult envelope. The step never ran:
#: the retry surface revives it when that upstream step is retried, and an
#: operator note on the step itself lifts the hold ("publish it anyway").
UPSTREAM_PARTIAL_RESULT_ERROR_TYPE = "UpstreamPartialResult"


#: Pre-flight outcomes written by the plan executor when a plan starts.
#: ``NoExternalRoute``: a step commits to an external effect (publish/send)
#: but no connected integration or paired Chrome can carry it — a person has
#: to connect one, so the failure is human-required. ``PreflightSkipped``:
#: the other pending steps of that plan, skipped before running so the task
#: parks on the blocker instead of spending credits on work it cannot finish.
NO_EXTERNAL_ROUTE_ERROR_TYPE = "NoExternalRoute"
PREFLIGHT_SKIPPED_ERROR_TYPE = "PreflightSkipped"


# ── Dispatch health / robustness contracts ────────────────────────────
#
# Shared by the dispatcher (writer), the executor (watchdog) and the worker
# (retry resume). Declared once so the three layers cannot drift on the
# spelling of a step.error shape.


class DispatchSkipReason(str, Enum):
    """Why the dispatcher declined to lease a PENDING step this pass."""

    #: No active AgentSubscription resolves for step.service_key/workspace.
    SUBSCRIPTION_UNRESOLVED = "subscription_unresolved"
    #: The resolved subscription is not bound to this worker.
    WORKER_NOT_BOUND = "worker_not_bound"
    #: The worker does not advertise the step's runtime capability.
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    #: The step's risk level exceeds the worker's ceiling.
    RISK_EXCEEDS_WORKER = "risk_exceeds_worker"
    #: A local action the worker cannot execute.
    LOCAL_ACTION_UNSUPPORTED = "local_action_unsupported"
    #: The provider connection the action needs is not ready.
    PROVIDER_CONNECTION_NOT_READY = "provider_connection_not_ready"
    #: Workspace monthly budget is exhausted.
    WORKSPACE_BUDGET = "workspace_budget"
    #: The executor saw the step runnable but no dispatcher pass has leased it.
    AWAITING_WORKER = "awaiting_worker"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: step.error["type"] when the ONLY thing wrong with a step is that the
#: dispatcher keeps skipping it (no real execution error is being preserved).
DISPATCH_SKIPPED_ERROR_TYPE = "DispatchSkipped"

#: Sub-key of step.error carrying the skip record:
#: ``{"reason": DispatchSkipReason, "message": str, "since": iso8601, "structural": bool}``.
#: Kept as a sub-key so a real prior execution error (used for retry feedback)
#: survives being stamped.
DISPATCH_SKIP_KEY = "dispatch_skip"

#: Terminal error type the executor assigns when a step stayed undispatchable
#: past its grace period. Always carries ``failure.requires_human = True``.
STEP_UNDISPATCHABLE_ERROR_TYPE = "StepUndispatchable"

#: Grace before a structurally undispatchable step (no subscription, no
#: bound worker, unsupported capability) is failed and parked on a human.
UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS = 10 * 60
#: Grace for soft reasons (budget, no worker pass yet) — long, because a
#: backlog is not a fault; past this it is an incident the user must see.
UNDISPATCHABLE_SOFT_GRACE_SECONDS = 2 * 60 * 60

#: Lease error types written when a lease ends for a reason other than the
#: worker's own report.
LEASE_REVOKED_ERROR_TYPE = "LeaseRevoked"
LEASE_SUPERSEDED_ERROR_TYPE = "LeaseSuperseded"

#: Plan-level total runtime budget exceeded (all remaining steps fail with it).
PLAN_DEADLINE_ERROR_TYPE = "PlanDeadlineExceeded"
DEFAULT_PLAN_MAX_RUNTIME_SECONDS = 24 * 60 * 60

#: ``execution_steps.runtime_checkpoint`` schema written by the internal
#: worker when a subagent attempt fails with a usable transcript. Distinct
#: from ``RuntimeAgentCheckpoint`` (request_input pauses): this one has no
#: pending tool call — the next attempt replays the transcript and appends
#: the failure feedback as a new user turn.
WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA = "worker-transcript/1"

#: Max serialized size of a worker transcript checkpoint (bytes).
WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES = 256_000

#: How many in-loop correction rounds a hard output contract gets before the
#: attempt is failed (and resumed on the next attempt from its transcript).
CONTRACT_CORRECTION_ROUNDS = 2

#: Step params keys (underscore-prefixed, model-invisible, same convention
#: as SUPERVISOR_STEP_RETRY_FLAG).
COORDINATOR_PARAM_KEY = "_coordinator"
# Version 2 pre-materialises every checklist item as a typed child. Roots
# without this marker are legacy V1 rows and retain the dynamic nested-loop
# compatibility path during rolling upgrades.
COORDINATOR_VERSION_PARAM_KEY = "_coordinator_version"
COORDINATOR_TYPED_VERSION = 2
COORDINATOR_CHILD_PARAM_KEY = "_coordinator_child"
COORDINATOR_PARENT_STEP_PARAM_KEY = "_coordinator_parent_step_key"
COORDINATOR_CHILD_ITEM_PARAM_KEY = "_coordinator_item_key"
COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY = "_coordinator_idempotency_key"
# Durable proof that the parent transcript checkpoint contains the exact
# terminal result currently stored on a coordinator child.  A child can
# finish just before its tool result reaches the parent; without this ACK a
# recovered coordinator could submit a final answer it never actually saw.
COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY = "_coordinator_parent_handoff_ack"
COORDINATOR_CHILD_HANDOFF_ACK_VERSION = 1
# First 16 hex chars of the exact JSON tool result handed to AgenticLoop.
# AgenticLoop copies that digest into its trusted truncation marker, allowing
# a large result to be ACKed without retaining its full text in context.
COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY = "_coordinator_response_digest"
# V2 coordinator children are materialized with their original typed
# executor contract.  They remain dormant until the parent releases them.
COORDINATOR_TYPED_CHILD_PARAM_KEY = "_coordinator_typed_child"
COORDINATOR_CHILD_RELEASED_PARAM_KEY = "_coordinator_child_released"
# Exact child identity a parked root RuntimeAgentCheckpoint is waiting for.
COORDINATOR_WAITING_CHILD_PARAM_KEY = "_coordinator_waiting_child"
# Durable wake deadline for a typed sleep child.  Keeping it on the row means
# an unrelated plan wake cannot accidentally shorten the requested delay.
COORDINATOR_CHILD_WAKE_AT_PARAM_KEY = "_coordinator_child_wake_at"
# Server-owned retry generation for an external effect that an operator
# explicitly verified did not happen.  Generation zero is the original
# attempt; each reconciled replay gets a fresh provider idempotency identity.
ACTION_EFFECT_GENERATION_PARAM_KEY = "_action_effect_generation"
# Server-owned binding between one coordinator child's parked request_input
# call and the answer fields stored on its parent step.  The child checkpoint
# commit writes this marker before the outer card is projected; resume accepts
# an answer only when both durable identities still match.
COORDINATOR_INPUT_CONTEXT_PARAM_KEY = "_coordinator_input_context"
COORDINATOR_INPUT_CONTEXT_VERSION = 1
# A lease-level approval created while a coordinator child is blocked must be
# bound to the exact Runtime tool-call approval it mirrors.  The dispatcher
# writes this server-owned object into the outer HitlRequest context; the
# unified approval service mirrors the human verdict to that one nested
# request.  Never accept this link from model-authored step params.
COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY = "coordinator_nested_approval"
COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY = "coordinator_outer_approval"
COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION = 1
SUPERVISOR_FEEDBACK_PARAM_KEY = "_supervisor_feedback"


def is_coordinator_child_params(params: object) -> bool:
    """Whether step params identify a coordinator's durable child row.

    Coordinator children carry their own usage watermark, while the parent
    step rolls that usage up for plan accounting.  Raw step-level reports must
    therefore exclude these implementation rows or they count the same spend
    twice.
    """

    return (
        isinstance(params, dict)
        and params.get(COORDINATOR_CHILD_PARAM_KEY) is True
    )


_ExecutionStepT = TypeVar("_ExecutionStepT")


def without_rolled_up_coordinator_children(
    steps: list[_ExecutionStepT],
) -> list[_ExecutionStepT]:
    """Drop child rows only when their terminal parent owns a cost roll-up.

    A completed child must continue to count while its coordinator parent is
    still running.  Once that parent is terminal and has a cost payload, the
    same child usage is already represented there and the child becomes a
    duplicate for raw step-cost aggregation.
    """

    rolled_up_parents: set[tuple[object, object]] = set()
    for step in steps:
        params = getattr(step, "params", None)
        cost = getattr(step, "cost", None)
        if (
            isinstance(params, dict)
            and params.get(COORDINATOR_PARAM_KEY) is True
            and params.get(COORDINATOR_VERSION_PARAM_KEY)
            != COORDINATOR_TYPED_VERSION
            and getattr(step, "finished_at", None) is not None
            and isinstance(cost, dict)
            and bool(cost)
        ):
            rolled_up_parents.add((
                getattr(step, "plan_id", None),
                getattr(step, "step_key", None),
            ))

    visible: list[_ExecutionStepT] = []
    for step in steps:
        params = getattr(step, "params", None)
        if is_coordinator_child_params(params) and (
            getattr(step, "plan_id", None),
            params.get(COORDINATOR_PARENT_STEP_PARAM_KEY),
        ) in rolled_up_parents:
            continue
        visible.append(step)
    return visible

#: Dynamic tool the coordinator step uses to delegate to a workspace service.
DELEGATE_TO_SERVICE_TOOL_NAME = "delegate_to_service"
