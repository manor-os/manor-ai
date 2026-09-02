"""Execution defaults shared by agent and scheduler runtimes."""

from enum import Enum

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
