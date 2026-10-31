"""AI execution Celery tasks — dispatched by API, executed by workers."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from celery import Task
from celery.exceptions import Ignore, Retry
from celery.result import EagerResult

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.task import TaskStatus
from packages.core.constants.execution import (
    ScheduledChildAdmissionStatus,
    SCHEDULED_EXECUTION_RECOVERY_HEADER,
    SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS,
    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS,
    SCHEDULED_RECOVERY_RETRY_SECONDS,
    ScheduledDispatchKind,
    ScheduledRecoveryKind,
    SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS,
    SCHEDULED_RESULT_PROJECTION_MAX_RETRIES,
    SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS,
    SCHEDULED_RESULT_PROJECTION_RETRY_SECONDS,
    SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
    SCHEDULED_SETTLEMENT_MAX_RETRIES,
    SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
    ScheduledResultProjectionState,
    ScheduledRunStatus,
    ScheduledSettlementKind,
    WORKFLOW_TERMINAL_EFFECT_RETRY_SECONDS,
    workflow_continuation_retry_seconds,
)
from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async
from packages.core.ai.llm_client import CreditExhaustedError, LLMRateLimited
from packages.core.queues import CeleryQueue
from packages.core.services.task_requester_identity import TaskRequesterIdentityError

logger = logging.getLogger(__name__)


class _PausedDocumentEmbeddingTask(Task):
    """Acknowledge embedding requests without publishing broker messages."""

    abstract = True

    def apply_async(self, args=None, kwargs=None, **options):
        document_id = args[0] if args else (kwargs or {}).get("document_id")
        result = {"document_id": document_id, "status": "disabled"}
        logger.debug(
            "Document embedding is temporarily disabled; skipped publish for %s",
            document_id,
        )
        return EagerResult(str(uuid4()), result, "SUCCESS", name=self.name)


def _scheduled_agent_terminal_status(
    result: dict[str, Any],
) -> ScheduledRunStatus | None:
    """Map a terminal Agent Task result onto its scheduler occurrence.

    ``waiting_on_customer`` is a durable suspension, not a business outcome.
    Keeping it unrepresented here prevents every settlement path from closing
    the occurrence or incrementing the parent Job's failure streak while the
    exact same Task is waiting to resume.
    """

    task_status = str(result.get("status") or "").strip().lower()
    if task_status == TaskStatus.COMPLETED.value:
        return ScheduledRunStatus.SUCCESS
    if task_status == TaskStatus.CANCELLED.value:
        return ScheduledRunStatus.CANCELLED
    if task_status == TaskStatus.FAILED.value:
        return ScheduledRunStatus.ERROR
    # A scheduler delivery is also the durable Task-runtime gateway.  Planning,
    # approval waits, coordinator suspension, and duplicate suppression are all
    # non-terminal acknowledgements; none is authority to close the occurrence.
    return None


class _TaskRuntimeHandoffError(RuntimeError):
    """A Task runtime was selected durably but its broker handoff failed."""


class _WorkflowExecutionClaimHeld(RuntimeError):
    """A duplicate delivery arrived while the original runner is still live."""


class _ScheduledSettlementHandoffError(RuntimeError):
    """Neither durable storage nor the broker accepted a terminal settlement."""

    def __init__(self, settlement):
        super().__init__("scheduled settlement handoff was not accepted")
        self.settlement = settlement


class AgentClaimLossRecoveryIntentState(StrEnum):
    """Durable evidence available after a business worker loses its claim."""

    PERSISTED = "persisted"
    SUSPENDED = "suspended"
    TASK_TERMINAL = "task_terminal"
    TASK_MISSING = "task_missing"


class AgentClaimLossRecoveryIntentField(StrEnum):
    """Stable Task.details keys owned by the claim-loss recovery protocol."""

    VERSION = "version"
    REQUESTED_AT = "requested_at"
    NEXT_ATTEMPT_AT = "next_attempt_at"
    SWEEP_ATTEMPTS = "sweep_attempts"
    PUBLISH_CLAIM_ID = "publish_claim_id"
    LAST_SWEEP_AT = "last_sweep_at"
    LAST_PUBLISH_ERROR = "last_publish_error"
    SCHEDULED_RUN_ID = "scheduled_run_id"
    SCHEDULED_JOB_ID = "scheduled_job_id"
    ERROR = "error"


class AgentClaimLossRecoveryIntentFactory:
    """Normalize, schedule, and claim durable recovery intents."""

    VERSION = 2

    @classmethod
    def persist(
        cls,
        existing: Any,
        *,
        scheduled_run_id: str | None,
        scheduled_job_id: str | None,
        error: str,
        now: datetime,
        retry_after_seconds: float,
        publish_claim_id: str | None = None,
    ) -> dict[str, Any]:
        current = dict(existing) if isinstance(existing, dict) else {}
        requested_at = current.get(
            AgentClaimLossRecoveryIntentField.REQUESTED_AT.value
        ) or now.isoformat()
        raw_next_attempt_at = current.get(
            AgentClaimLossRecoveryIntentField.NEXT_ATTEMPT_AT.value
        )
        has_publish_claim = bool(publish_claim_id)
        next_attempt_at = now.timestamp() + max(0.0, retry_after_seconds)
        if (
            not has_publish_claim
            and not isinstance(raw_next_attempt_at, bool)
            and isinstance(raw_next_attempt_at, (int, float))
        ):
            next_attempt_at = float(raw_next_attempt_at)
        persisted = {
            AgentClaimLossRecoveryIntentField.VERSION.value: cls.VERSION,
            AgentClaimLossRecoveryIntentField.REQUESTED_AT.value: requested_at,
            AgentClaimLossRecoveryIntentField.NEXT_ATTEMPT_AT.value: next_attempt_at,
            AgentClaimLossRecoveryIntentField.SWEEP_ATTEMPTS.value: (
                0 if has_publish_claim else cls._attempts(current)
            ),
            AgentClaimLossRecoveryIntentField.SCHEDULED_RUN_ID.value: (
                scheduled_run_id
                if scheduled_run_id is not None
                else current.get(
                    AgentClaimLossRecoveryIntentField.SCHEDULED_RUN_ID.value
                )
            ),
            AgentClaimLossRecoveryIntentField.SCHEDULED_JOB_ID.value: (
                scheduled_job_id
                if scheduled_job_id is not None
                else current.get(
                    AgentClaimLossRecoveryIntentField.SCHEDULED_JOB_ID.value
                )
            ),
            AgentClaimLossRecoveryIntentField.ERROR.value: error,
        }
        if has_publish_claim:
            persisted[AgentClaimLossRecoveryIntentField.PUBLISH_CLAIM_ID.value] = (
                publish_claim_id
            )
        return persisted

    @classmethod
    def claim(
        cls,
        existing: Any,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        current = dict(existing) if isinstance(existing, dict) else {}
        sweep_attempt = cls._attempts(current) + 1
        claimed = {
            **current,
            AgentClaimLossRecoveryIntentField.VERSION.value: cls.VERSION,
            AgentClaimLossRecoveryIntentField.NEXT_ATTEMPT_AT.value: (
                now.timestamp() + cls.recovery_delay_seconds(sweep_attempt)
            ),
            AgentClaimLossRecoveryIntentField.SWEEP_ATTEMPTS.value: sweep_attempt,
            AgentClaimLossRecoveryIntentField.PUBLISH_CLAIM_ID.value: uuid4().hex,
            AgentClaimLossRecoveryIntentField.LAST_SWEEP_AT.value: now.isoformat(),
        }
        claimed.pop(
            AgentClaimLossRecoveryIntentField.LAST_PUBLISH_ERROR.value,
            None,
        )
        return claimed

    @staticmethod
    def candidate(task_id: Any, existing: Any) -> dict[str, Any]:
        current = dict(existing) if isinstance(existing, dict) else {}
        run_id = current.get(
            AgentClaimLossRecoveryIntentField.SCHEDULED_RUN_ID.value
        )
        job_id = current.get(
            AgentClaimLossRecoveryIntentField.SCHEDULED_JOB_ID.value
        )
        return {
            "task_id": str(task_id),
            "scheduled_run_id": str(run_id) if run_id else None,
            "scheduled_job_id": str(job_id) if job_id else None,
            "_sweep_attempt": AgentClaimLossRecoveryIntentFactory._attempts(
                current
            ),
            "_publish_claim_id": current.get(
                AgentClaimLossRecoveryIntentField.PUBLISH_CLAIM_ID.value
            ),
        }

    @classmethod
    def release_publish_claim(
        cls,
        existing: Any,
        *,
        expected_sweep_attempt: int,
        expected_publish_claim_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        current = dict(existing) if isinstance(existing, dict) else {}
        if (
            cls._attempts(current) != expected_sweep_attempt
            or current.get(
                AgentClaimLossRecoveryIntentField.PUBLISH_CLAIM_ID.value
            )
            != expected_publish_claim_id
        ):
            return None
        current[AgentClaimLossRecoveryIntentField.SWEEP_ATTEMPTS.value] = max(
            expected_sweep_attempt - 1,
            0,
        )
        current.pop(
            AgentClaimLossRecoveryIntentField.PUBLISH_CLAIM_ID.value,
            None,
        )
        current[AgentClaimLossRecoveryIntentField.NEXT_ATTEMPT_AT.value] = (
            now.timestamp() + SCHEDULED_RECOVERY_RETRY_SECONDS
        )
        current[AgentClaimLossRecoveryIntentField.LAST_PUBLISH_ERROR.value] = (
            "broker handoff failed"
        )
        return current

    @staticmethod
    def recovery_delay_seconds(sweep_attempt: int) -> int:
        exponent = min(max(int(sweep_attempt) - 1, 0), 5)
        return min(
            SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS * (2 ** exponent),
            _AGENT_CLAIM_LOSS_RECOVERY_MAX_DELAY_SECONDS,
        )

    @staticmethod
    def _attempts(existing: dict[str, Any]) -> int:
        try:
            return max(
                0,
                int(
                    existing.get(
                        AgentClaimLossRecoveryIntentField.SWEEP_ATTEMPTS.value
                    )
                    or 0
                ),
            )
        except (TypeError, ValueError):
            return 0


_AGENT_CLAIM_LOSS_RECOVERY_KEY = "_agent_claim_loss_recovery_v1"
_AGENT_CLAIM_LOSS_RECOVERY_BATCH = 100
_AGENT_CLAIM_LOSS_RECOVERY_MAX_DELAY_SECONDS = 6 * 60 * 60


async def runtime_assert_credit_available(*args, **kwargs):
    """Lazy Runtime import so Celery module loading stays lightweight."""
    from packages.core.ai.runtime import runtime_assert_credit_available as _assert

    return await _assert(*args, **kwargs)


async def _scheduled_job_skill_generation_byok(job, *, db=None) -> bool:
    try:
        from packages.core.ai.llm_client import metadata_has_native_byok
        from packages.core.services.model_resolver import resolve_llm_metadata_for_user

        metadata = await resolve_llm_metadata_for_user(
            "skill_generator",
            user_id=getattr(job, "user_id", None),
            entity_id=getattr(job, "entity_id", None),
            db=db,
            workspace_id=getattr(job, "workspace_id", None),
        )
        return metadata_has_native_byok(metadata)
    except Exception:
        logger.debug("Unable to resolve scheduled skill-generation BYOK metadata", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Helpers — mark a task as failed when Celery retries are exhausted
# ---------------------------------------------------------------------------

async def _apply_task_execution_failure(
    db,
    task_id: str,
    error_msg: str,
    *,
    error_type: str,
) -> tuple[str, dict] | None:
    """Fail an in-progress Task under the canonical Workspace -> Task locks."""

    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import apply_task_status_transition
    from packages.core.services.task_runtime_approvals import (
        expire_task_runtime_approval_for_terminal,
        lock_task_runtime_approval_boundary,
    )
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )

    task_scope = (
        await db.execute(
            select(Task.id, Task.entity_id, Task.workspace_id).where(
                Task.id == task_id
            )
        )
    ).one_or_none()
    if task_scope is None:
        return None

    entity_id = str(task_scope.entity_id)
    workspace_id = (
        str(task_scope.workspace_id) if task_scope.workspace_id else None
    )
    if workspace_id:
        workspace = await lock_workspace_access_boundary(
            db,
            workspace_id=workspace_id,
            entity_id=entity_id,
        )
        if workspace is None:
            return None

    await lock_task_runtime_approval_boundary(db, task_id=task_id)

    task = (
        await db.execute(
            select(Task)
            .where(
                Task.id == task_id,
                Task.entity_id == entity_id,
                Task.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS:
        return None

    await expire_task_runtime_approval_for_terminal(
        db,
        task=task,
        reason="task_planning_failed",
    )
    await apply_task_status_transition(task, "failed", db=db)
    task.actual_output = {
        "task_status": "failed",
        "error_type": error_type,
        "error_message": error_msg,
    }
    try:
        from packages.core.services.workspace_operation_service import (
            check_work_batch_completion,
        )

        await check_work_batch_completion(
            db,
            task,
            trigger_source="task_planning_failed",
        )
    except Exception:
        logger.warning(
            "task %s: failed to evaluate workspace work batch completion after planning failure",
            task_id,
            exc_info=True,
        )
    return entity_id, {
        "task_id": task.id,
        "title": task.title,
        "task_status": "failed",
        "error_type": error_type,
        "error_message": error_msg,
    }




def _mark_task_failed(
    task_id: str,
    error_msg: str,
    *,
    error_type: str = "TaskPlanningExhausted",
) -> None:
    """Mark a Task as failed after its runner crashes."""
    try:
        async def _do():
            from packages.core.database import create_worker_session
            async with create_worker_session()() as db:
                failure = await _apply_task_execution_failure(
                    db,
                    task_id,
                    error_msg,
                    error_type=error_type,
                )
                await db.commit()
                if failure is not None:
                    entity_id, event_payload = failure
                    from packages.core.services import event_emitter
                    event_emitter.emit(
                        entity_id,
                        "task.failed",
                        source="ai_tasks",
                        payload=event_payload,
                    )
        _run_async(_do())
    except Exception:
        logger.exception("Failed to mark task %s as failed", task_id)


def _retry_once_on_credit_exhausted(self, exc: CreditExhaustedError, *, countdown: int = 120) -> None:
    """Retry once so a manual top-up can resume the original task."""
    if self.request.retries < 1 and self.max_retries > 0:
        logger.warning("Credits exhausted, scheduling one retry in %ss", countdown)
        raise self.retry(exc=exc, countdown=countdown)


# ---------------------------------------------------------------------------
# Scheduled-task finaliser — closes the loop on dispatcher status updates.
# ---------------------------------------------------------------------------
#
# The scheduler dispatcher creates a ``scheduled_job_runs`` row with
# status='running' and sets ``last_status='running'`` on the parent
# job, then dispatches the Celery task. Without these helpers, the
# task succeeds (or skips) but never marks its row complete — so every
# successful run stays "running" in the DB forever, making the admin
# UI useless for "did this automation actually run?".
#
# All scheduled task types (``run_morning_briefing``,
# ``run_outcome_evaluation``, ``run_chat_insight_extraction``,
# ``run_strategist_review``, ``run_goal_measurement``) accept ``run_id`` and
# ``job_id_str``
# kwargs and call ``_finalize_scheduled_run`` on their three exit
# paths: success, credit-exhaustion, terminal failure (after retries
# exhausted). Manual triggers (``.delay(workspace_id)`` from the API)
# leave both kwargs at None and skip the finalize step.

async def _finalize_scheduled_run(
    *, run_id: str | None, job_id_str: str | None,
    result: dict | None = None, error: str | None = None,
    outcome=None,
    execution_claim=None,
) -> None:
    """Mark a scheduled_job_runs row + its parent scheduled_jobs as
    completed/skipped/cancelled/error.

    Status decided in this priority order:
      - ``error`` set → "error"
      - ``result["skipped"] == True`` → "skipped"
      - ``result["status"] == "cancelled"`` → "cancelled"
      - otherwise → "completed"
    """
    if not run_id and not job_id_str:
        return
    from datetime import datetime, timezone
    from packages.core.database import create_worker_session
    from packages.core.services.scheduler_service import (
        ScheduledJobMutationFactory,
        lock_scheduled_job_and_run,
        notify_scheduled_job_auto_paused,
        project_scheduled_job_outcome,
        reconcile_scheduled_job_run_projection,
    )

    now = datetime.now(timezone.utc)
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunOutcome,
        apply_scheduled_run_outcome,
    )

    terminal_outcome = outcome or ScheduledRunOutcome.from_execution(
        result=result,
        error=error,
    )
    run_status = terminal_outcome.status.value

    session_factory = create_worker_session()
    async with session_factory() as db:
        job, run = await lock_scheduled_job_and_run(
            db,
            job_id=job_id_str,
            run_id=run_id,
        )
        if run_id and run is None:
            await db.rollback()
            return
        run_finalized = False
        if run is not None:
            if run and run.status == "running":
                apply_scheduled_run_outcome(
                    run,
                    terminal_outcome,
                    completed_at=now,
                )
                run_finalized = True
        if job:
            if run_finalized and run is not None:
                from packages.core.ledger.adapters import (
                    record_automation_run_finished,
                )
                await record_automation_run_finished(
                    db,
                    job,
                    run_id=run.id,
                    status=run_status,
                )
                auto_paused = await reconcile_scheduled_job_run_projection(
                    db,
                    job,
                    finalized_run_id=run.id,
                )
            elif run is None:
                auto_paused = project_scheduled_job_outcome(job, run_status)
                if auto_paused:
                    await ScheduledJobMutationFactory.apply_locked(
                        db,
                        job,
                        {"enabled": False},
                        causation_id=now.isoformat(),
                        allow_agent_initiative_driver=True,
                    )
            else:
                auto_paused = False
            if auto_paused:
                await notify_scheduled_job_auto_paused(
                    db,
                    job,
                    failure_key=(run.id if run else now.isoformat()),
                )
        if execution_claim is None:
            await db.commit()
        else:
            from packages.core.services.workflow_run_execution_claim import (
                commit_fenced_execution_boundary,
            )

            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=execution_claim.raise_if_lost,
                after_commit=execution_claim.mark_terminal_committed,
                execution_claim=execution_claim,
                session=db,
            )


async def _persist_scheduled_run_settlement(
    *,
    run_id: str,
    job_id_str: str | None,
    settlement,
    execution_claim=None,
) -> bool:
    from packages.core.database import create_worker_session
    from packages.core.services.scheduled_run_lifecycle import (
        persist_scheduled_run_settlement_pending,
    )

    async with create_worker_session()() as db:
        persisted = await persist_scheduled_run_settlement_pending(
            db,
            run_id=run_id,
            job_id=job_id_str,
            settlement=settlement,
        )
        if execution_claim is None:
            await db.commit()
        else:
            from packages.core.services.workflow_run_execution_claim import (
                commit_fenced_execution_boundary,
            )

            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=execution_claim.raise_if_lost,
                after_commit=execution_claim.mark_terminal_committed,
                execution_claim=execution_claim,
                session=db,
            )
        return persisted


def _enqueue_scheduled_run_settlement(
    *,
    run_id: str,
    job_id_str: str | None,
    settlement,
) -> None:
    _scheduled_settlement_signature(
        run_id=run_id,
        job_id_str=job_id_str,
        settlement=settlement,
    ).apply_async()


async def _defer_scheduled_run_settlement(
    *,
    run_id: str,
    job_id_str: str | None,
    result: dict | None,
    error: str | None,
    execution_claim=None,
) -> None:
    """Fence completed work and enqueue a settlement-only recovery."""

    from packages.core.services.scheduled_run_lifecycle import ScheduledRunSettlement

    settlement = ScheduledRunSettlement.create(result=result, error=error)
    persisted = False
    enqueued = False
    try:
        persisted = await _persist_scheduled_run_settlement(
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=settlement,
            execution_claim=execution_claim,
        )
    except Exception:
        if execution_claim is not None:
            execution_claim.raise_if_lost()
        logger.exception(
            "Failed to persist scheduled settlement fence run=%s job=%s",
            run_id,
            job_id_str,
        )
        if execution_claim is not None:
            # A settlement-only redelivery will reacquire the occurrence claim.
            # Never publish an unfenced result directly from the stale owner.
            raise _ScheduledSettlementHandoffError(settlement)
    if execution_claim is not None and not persisted:
        # The occurrence already reached a terminal state while this token was
        # still current, so there is no settlement work left to enqueue.
        return
    try:
        _enqueue_scheduled_run_settlement(
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=settlement,
        )
        enqueued = True
    except Exception:
        logger.exception(
            "Failed to queue scheduled settlement recovery run=%s job=%s",
            run_id,
            job_id_str,
        )
    if not persisted and not enqueued:
        raise _ScheduledSettlementHandoffError(settlement)


def _scheduled_settlement_signature(
    *,
    run_id: str,
    job_id_str: str | None,
    settlement,
):
    """Build one version-isolated settlement-only delivery."""

    if settlement.kind is ScheduledSettlementKind.AGENT_TASK:
        task = settle_scheduled_agent_run
    elif settlement.kind is ScheduledSettlementKind.GENERIC:
        task = settle_scheduled_run
    else:
        raise ValueError(f"unsupported scheduled settlement kind: {settlement.kind!r}")
    return task.s(
        run_id,
        job_id_str,
        settlement.to_payload(),
    ).set(queue=CeleryQueue.RECOVERY_V2.value)


def _replace_with_scheduled_settlement(
    celery_task,
    *,
    run_id: str,
    job_id_str: str | None,
    settlement,
):
    """End a completed business task as a dedicated settlement delivery."""

    return celery_task.replace(
        _scheduled_settlement_signature(
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=settlement,
        )
    )


async def _finalize_scheduled_run_with_claim(
    *,
    run_id: str,
    job_id_str: str | None,
    outcome,
) -> bool:
    """Apply a settlement-only payload under the occurrence execution fence."""

    from packages.core.services.workflow_run_execution_claim import (
        scheduled_run_execution_claim,
    )

    async with scheduled_run_execution_claim(run_id) as claim:
        if not claim:
            return False
        await _finalize_scheduled_run(
            run_id=run_id,
            job_id_str=job_id_str,
            outcome=outcome,
            execution_claim=claim,
        )
        return True


@celery_app.task(
    bind=True,
    name="scheduler.settle_scheduled_run",
    max_retries=SCHEDULED_SETTLEMENT_MAX_RETRIES,
)
def settle_scheduled_run(
    self,
    run_id: str,
    job_id_str: str | None,
    settlement_payload: dict,
):
    """Retry scheduler bookkeeping without replaying completed business work."""

    from packages.core.services.scheduled_run_lifecycle import ScheduledRunSettlement

    settlement = ScheduledRunSettlement.from_payload(settlement_payload)
    if settlement is None or settlement.kind is not ScheduledSettlementKind.GENERIC:
        raise ValueError("invalid generic scheduled settlement payload")
    try:
        settled = _run_async(_finalize_scheduled_run_with_claim(
            run_id=run_id,
            job_id_str=job_id_str,
            outcome=settlement.outcome,
        ))
        if not settled:
            raise RuntimeError("scheduled settlement execution claim is held")
        return {
            "run_id": run_id,
            "status": settlement.outcome.status.value,
        }
    except Exception as exc:
        raise self.retry(exc=exc, countdown=SCHEDULED_RECOVERY_RETRY_SECONDS)


def _finalize_scheduled_run_best_effort(
    *,
    run_id: str | None,
    job_id_str: str | None,
    result: dict | None = None,
    error: str | None = None,
    celery_task=None,
) -> None:
    """Persist scheduler bookkeeping without replaying completed business work."""

    try:
        _run_async(_finalize_scheduled_run(
            run_id=run_id,
            job_id_str=job_id_str,
            result=result,
            error=error,
        ))
    except Exception:
        logger.exception(
            "Failed to finalize scheduled run=%s job=%s",
            run_id,
            job_id_str,
        )
        if run_id:
            try:
                _run_async(_defer_scheduled_run_settlement(
                    run_id=run_id,
                    job_id_str=job_id_str,
                    result=result,
                    error=error,
                ))
            except _ScheduledSettlementHandoffError as handoff:
                if celery_task is None:
                    raise
                return _replace_with_scheduled_settlement(
                    celery_task,
                    run_id=run_id,
                    job_id_str=job_id_str,
                    settlement=handoff.settlement,
                )


def _compact_error(error_msg: str, *, limit: int = 500) -> str:
    """Keep user-facing failure cards readable without losing the root cause."""
    compact = " ".join(str(error_msg or "Unknown error").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


async def _post_strategist_failure_card(
    *,
    workspace_id: str,
    trigger,
    error_msg: str,
    run_id: str | None = None,
    job_id_str: str | None = None,
) -> None:
    """Surface terminal Strategist failures where the operator already works."""
    from sqlalchemy import select
    from packages.core.database import create_worker_session
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import service as chat_service

    reason = _compact_error(error_msg)
    session_factory = create_worker_session()
    async with session_factory() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not workspace:
            return

        body = (
            "The workspace review could not be completed.\n\n"
            f"Reason: {reason}\n\n"
            "Fix the issue if needed, then retry the review from here."
        )
        await chat_service.post_message(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            body=body,
            message_kind="system",
            author_kind="system",
            refs=[{"type": "workspace", "id": workspace.id}],
            pending_action={
                "kind": PendingActionKind.RETRY_STRATEGIST_REVIEW.value,
                "workspace_id": workspace.id,
                # Display/audit only — the retry re-enqueues as
                # HUMAN_REQUESTED because a person clicked the button.
                "trigger": getattr(trigger, "label", trigger),
                "run_id": run_id,
                "job_id": job_id_str,
                "error": reason,
                "options": ["retry"],
            },
        )
        await db.commit()


async def _admit_scheduled_child(
    run_id: str,
    job_id_str: str | None,
    *,
    child_kind: ScheduledDispatchKind | None = None,
    child_id: str | None = None,
    execution_claim=None,
):
    """Atomically admit or settle one exact scheduled occurrence."""
    from packages.core.database import create_worker_session
    from packages.core.services.scheduled_run_lifecycle import admit_scheduled_child

    async with create_worker_session()() as db:
        admission = await admit_scheduled_child(
            db,
            run_id=run_id,
            job_id=job_id_str,
            child_kind=child_kind,
            child_id=child_id,
        )
        if execution_claim is None:
            await db.commit()
        else:
            from packages.core.services.workflow_run_execution_claim import (
                commit_fenced_execution_boundary,
            )

            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=execution_claim.raise_if_lost,
                execution_claim=execution_claim,
                session=db,
            )
        return admission


async def _scheduled_run_is_open(
    run_id: str,
    job_id_str: str | None,
) -> bool:
    """Read-only lifecycle probe retained for diagnostics and tests."""

    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    query = (
        select(ScheduledJobRun.id)
        .join(ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id)
        .where(
            ScheduledJobRun.id == run_id,
            ScheduledJobRun.status == ScheduledRunStatus.RUNNING.value,
            ScheduledJob.enabled.is_(True),
        )
        .with_for_update(of=ScheduledJob)
    )
    if job_id_str:
        query = query.where(ScheduledJobRun.job_id == job_id_str)
    async with create_worker_session()() as db:
        return (await db.execute(query)).scalar_one_or_none() is not None


async def _terminalize_suppressed_scheduled_child(
    session_factory,
    *,
    child_kind: ScheduledDispatchKind,
    child_id: str,
    scheduled_run_id: str,
    scheduled_job_id: str | None,
) -> bool:
    """Cancel a pristine prepared child while owning its execution claim."""
    if not scheduled_job_id:
        return False

    from packages.core.services.scheduled_run_lifecycle import (
        cancel_scheduled_child_if_pristine,
    )
    from packages.core.services.workflow_run_execution_claim import (
        agent_task_execution_claim,
        commit_fenced_execution_boundary,
        workflow_run_execution_claim,
    )

    claim_factory = (
        agent_task_execution_claim
        if child_kind is ScheduledDispatchKind.AGENT_TASK
        else workflow_run_execution_claim
    )
    async with claim_factory(child_id) as claim:
        if not claim:
            return False
        async with session_factory() as db:
            terminalized = await cancel_scheduled_child_if_pristine(
                db,
                child_kind=child_kind,
                child_id=child_id,
                run_id=scheduled_run_id,
                job_id=scheduled_job_id,
            )
            if terminalized:
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=claim.raise_if_lost,
                    after_commit=claim.mark_terminal_committed,
                    execution_claim=claim,
                    session=db,
                )
            else:
                await db.rollback()
            return terminalized


def _suppressed_child_needs_terminalization(admission) -> bool:
    """Return whether rejection represents a pre-execution close boundary."""
    return (
        admission.status is ScheduledChildAdmissionStatus.CANCELLED
        or admission.reason
        in {
            "scheduled_occurrence_missing",
            "scheduled_occurrence_terminal",
        }
    )


async def _run_scheduled_agent_task_if_open(
    session_factory,
    task_id: str,
    agent_id: str | None,
    *,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
    claim_recheck: bool = False,
) -> tuple[bool, dict | None]:
    """Admit scheduled agent work only while its durable occurrence is open."""
    admission = None
    if scheduled_run_id:
        admission = await _admit_scheduled_child(
            scheduled_run_id,
            scheduled_job_id,
            child_kind=ScheduledDispatchKind.AGENT_TASK,
            child_id=task_id,
        )
    if admission is not None and not admission.admitted:
        child_terminalized = False
        if _suppressed_child_needs_terminalization(admission):
            child_terminalized = await _terminalize_suppressed_scheduled_child(
                session_factory,
                child_kind=ScheduledDispatchKind.AGENT_TASK,
                child_id=task_id,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
            )
        logger.info(
            "Scheduled agent child suppressed run=%s job=%s reason=%s "
            "child_terminalized=%s",
            scheduled_run_id,
            scheduled_job_id,
            admission.reason,
            child_terminalized,
        )
        return False, {
            "task_id": task_id,
            "status": (
                ScheduledRunStatus.CANCELLED.value
                if child_terminalized
                or admission.status is ScheduledChildAdmissionStatus.CANCELLED
                else "already_settled"
            ),
            "duplicate_suppressed": True,
            "scheduled_occurrence_closed": True,
            "reason": admission.reason,
            "child_terminalized": child_terminalized,
        }
    suspended_status = (
        await _scheduled_agent_recovery_is_suspended(
            session_factory,
            task_id=task_id,
            scheduled_run_id=scheduled_run_id,
            scheduled_job_id=scheduled_job_id,
        )
        if claim_recheck
        else None
    )
    if suspended_status:
        return False, {
            "task_id": task_id,
            "status": suspended_status,
            "duplicate_suppressed": True,
            "recovery_suspended": True,
            "reason": "task_human_suspended",
        }
    return await _run_agent_task_with_execution_claim(
        session_factory,
        task_id,
        agent_id,
    )


async def _scheduled_agent_recovery_is_suspended(
    session_factory,
    *,
    task_id: str,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
) -> str | None:
    """Fence a recovery-only delivery at the Task's durable HITL state.

    Returns the Task's real status when it is durably waiting for a human
    (WAITING_ON_CUSTOMER, BLOCKED or a runtime HITL gate) for exactly this
    scheduled occurrence, otherwise ``None``.
    """

    if not scheduled_run_id or not scheduled_job_id:
        return None
    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    async with session_factory() as db:
        task = (await db.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        # A terminal Task is never suspended, whatever a stale HITL marker says.
        if task is None or task.status in TERMINAL_STATUSES:
            return None
        # BLOCKED waits for a human exactly like WAITING_ON_CUSTOMER: periodic
        # recovery re-entry only yields another coordinator monitor result.
        # The Task terminal transition settles the occurrence instead.
        if task.status != TaskStatus.BLOCKED.value and not (
            _scheduled_agent_task_has_runtime_human_gate(task)
        ):
            return None
        details = task.details if isinstance(task.details, dict) else {}
        if (
            str(details.get("scheduled_run_id") or "") == scheduled_run_id
            and str(details.get("scheduled_job_id") or "") == scheduled_job_id
        ):
            return str(task.status)
        return None


async def _run_scheduled_once(
    coro_factory,
    *,
    run_id: str | None,
    job_id_str: str | None,
) -> tuple[bool, object]:
    """Run one scheduled child body while owning its occurrence claim."""
    if not run_id:
        return True, await coro_factory()

    from packages.core.services.workflow_run_execution_claim import (
        ScheduledRunExecutionClaimLost,
        scheduled_run_execution_claim,
    )

    async with scheduled_run_execution_claim(run_id) as claim:
        if not claim:
            logger.info(
                "Scheduled run %s execution claim denied (%s)",
                run_id,
                claim.reason,
            )
            return False, {
                "status": "in_progress",
                "duplicate_suppressed": True,
            }
        admission = await _admit_scheduled_child(
            run_id,
            job_id_str,
            execution_claim=claim,
        )
        if not admission.admitted:
            logger.info(
                "Scheduled run %s suppressed (%s)",
                run_id,
                admission.reason,
            )
            return False, {
                "status": "already_settled",
                "duplicate_suppressed": True,
                "reason": admission.reason,
            }

        result = await coro_factory()
        claim.raise_if_lost()
        try:
            await _finalize_scheduled_run(
                run_id=run_id,
                job_id_str=job_id_str,
                result=result if isinstance(result, dict) else None,
                execution_claim=claim,
            )
        except ScheduledRunExecutionClaimLost:
            # A successor may already own the occurrence. The stale token must
            # not write an unfenced settlement or mark the claim terminal.
            raise
        except Exception:
            # The business body may have performed irreversible provider I/O.
            # Do not replay it merely because bookkeeping could not commit.
            logger.exception(
                "Failed to finalize completed scheduled run=%s job=%s",
                run_id,
                job_id_str,
            )
            await _defer_scheduled_run_settlement(
                run_id=run_id,
                job_id_str=job_id_str,
                result=result if isinstance(result, dict) else None,
                error=None,
                execution_claim=claim,
            )
        claim.mark_terminal_committed()
        return True, result


def _run_scheduled(
    self,
    label: str,
    workspace_id: str,
    coro_factory,
    *,
    run_id: str | None,
    job_id_str: str | None,
    countdown_on_retry: int = 300,
    on_terminal_error=None,
):
    """Execute a scheduled body, settling terminal errors without replay.

    Reduces 12 lines of boilerplate per task to one call. ``coro_factory``
    is called with no args and must return a coroutine — the lambda
    pattern lets callers close over their own imports/state without a
    nested ``async def _go``.
    """
    from packages.core.services.workflow_run_execution_claim import (
        ScheduledRunExecutionClaimLost,
    )
    from packages.core.strategist.prompt import StrategistProposalGenerationError

    try:
        _executed, result = _run_async(_run_scheduled_once(
            coro_factory,
            run_id=run_id,
            job_id_str=job_id_str,
        ))
        if not run_id:
            _finalize_scheduled_run_best_effort(
                run_id=run_id,
                job_id_str=job_id_str,
                result=result if isinstance(result, dict) else None,
                celery_task=self,
            )
        return result
    except (Ignore, Retry):
        raise
    except _ScheduledSettlementHandoffError as handoff:
        if not run_id:
            raise
        return _replace_with_scheduled_settlement(
            self,
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=handoff.settlement,
        )
    except ScheduledRunExecutionClaimLost as exc:
        error = str(exc) or "Scheduled run execution claim was lost"
        logger.error(
            "%s %s stopped after losing its execution claim",
            label,
            workspace_id,
        )
        # Only the current durable token may settle the occurrence or emit its
        # terminal side effects. Leave the run open for fenced recovery.
        return {"status": "failed", "error": error, "claim_lost": True}
    except LLMRateLimited as exc:
        logger.warning(
            "%s %s rate limited; provider requested %.1fs backoff",
            label,
            workspace_id,
            exc.retry_after,
        )
        if self.request.retries >= self.max_retries:
            error = (
                f"Provider remained rate limited after "
                f"{self.max_retries + 1} attempts: {exc}"
            )
            _finalize_scheduled_run_best_effort(
                run_id=run_id,
                job_id_str=job_id_str,
                error=error,
                celery_task=self,
            )
            if on_terminal_error:
                _run_async(on_terminal_error(error))
            return {"status": "failed", "error": error}
        raise self.retry(
            exc=RuntimeError(str(exc)),
            countdown=max(1, int(exc.retry_after)),
        )
    except CreditExhaustedError as exc:
        try:
            _retry_once_on_credit_exhausted(self, exc, countdown=120)
        except Exception:
            raise
        logger.warning("%s %s skipped: credits exhausted", label, workspace_id)
        _finalize_scheduled_run_best_effort(
            run_id=run_id, job_id_str=job_id_str,
            error=f"credits_exhausted: {exc}",
            celery_task=self,
        )
        if on_terminal_error:
            _run_async(on_terminal_error(f"credits_exhausted: {exc}"))
    except StrategistProposalGenerationError as exc:
        # Proposal generation already used its bounded format retry. Replaying
        # this review would spend another model budget without new evidence.
        error = f"{exc.code}: {exc}"
        result = {"status": "failed", "error": error, "code": exc.code, "details": exc.details}
        logger.error("%s %s failed: %s", label, workspace_id, error)
        _finalize_scheduled_run_best_effort(
            run_id=run_id, job_id_str=job_id_str, result=result, error=error,
            celery_task=self,
        )
        if on_terminal_error:
            try:
                _run_async(on_terminal_error(error))
            except Exception:
                logger.exception("Failed to post terminal failure card for %s %s", label, workspace_id)
        return result
    except Exception as exc:
        logger.error("%s %s failed: %s", label, workspace_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _finalize_scheduled_run_best_effort(
                run_id=run_id, job_id_str=job_id_str, error=str(exc),
                celery_task=self,
            )
            if on_terminal_error:
                try:
                    _run_async(on_terminal_error(str(exc)))
                except Exception:
                    logger.exception("Failed to post terminal failure card for %s %s", label, workspace_id)
        raise self.retry(exc=exc, countdown=countdown_on_retry)


@celery_app.task(bind=True, max_retries=0)
def cleanup_expired_leases(self):
    """Reclaim leases past their TTL. Beat-driven."""
    try:
        from packages.core.dispatcher import Dispatcher
        from packages.core.database import create_worker_session

        async def _go():
            session_factory = create_worker_session()
            async with session_factory() as db:
                n = await Dispatcher().expire_leases(db)
                await db.commit()
            expired_user_sessions = 0
            try:
                from packages.core.services.user_session_service import (
                    cleanup_expired_user_session_leases,
                )

                async with session_factory() as db:
                    expired_user_sessions = (
                        await cleanup_expired_user_session_leases(db)
                    )
                    await db.commit()
            except Exception:
                # Session analytics must never prevent the dispatcher lease
                # cleanup from committing during a rolling schema migration.
                logger.debug(
                    "user session lease cleanup unavailable",
                    exc_info=True,
                )
            stale_workers = 0
            try:
                from packages.core.workers import (
                    mark_stale_external_workers_offline,
                )

                async with session_factory() as db:
                    stale_workers = await mark_stale_external_workers_offline(db)
                    await db.commit()
            except Exception:
                logger.debug(
                    "stale external worker cleanup unavailable",
                    exc_info=True,
                )
            return n, expired_user_sessions, stale_workers

        n, expired_user_sessions, stale_workers = _run_async(_go())
        if n:
            logger.info("cleanup_expired_leases: reclaimed %d", n)
        if expired_user_sessions:
            logger.info(
                "cleanup_expired_leases: closed %d abandoned user sessions",
                expired_user_sessions,
            )
        if stale_workers:
            logger.info(
                "cleanup_expired_leases: marked %d stale external workers offline",
                stale_workers,
            )
    except Exception:
        logger.exception("cleanup_expired_leases failed")


@celery_app.task(bind=True, max_retries=0)
def budget_monthly_reset(self):
    """Zero monthly_spent for workspaces / workers due for reset.

    Beat-driven daily — first day of month catches anything not
    already reset by an earlier tick. Idempotent; rows already reset
    within the current month are no-ops on subsequent runs."""
    try:
        from packages.core.budget import monthly_reset_scan
        from packages.core.database import create_worker_session

        async def _go():
            async with create_worker_session()() as db:
                result = await monthly_reset_scan(db)
                await db.commit()
                return result

        result = _run_async(_go())
        if result.get("workspaces_reset") or result.get("workers_reset"):
            logger.info("budget_monthly_reset: %s", result)
    except Exception:
        logger.exception("budget_monthly_reset failed")


@celery_app.task(bind=True, max_retries=2)
def run_morning_briefing(
    self, workspace_id: str,
    *,
    timezone_name: str | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    job_id_str: str | None = None,
):
    """Build + post the daily morning briefing for a workspace.

    Cadence comes from a ScheduledJob row tagged
    ``execution_type='briefing'`` (see briefing.scheduling). Manual
    triggers route through the same task via ``.delay()``.

    ``run_id`` and ``job_id_str`` are passed by the scheduler dispatcher
    so the run + parent job get marked complete. None for manual.
    """
    logger.info(
        "Morning briefing for workspace %s (attempt %d)",
        workspace_id, self.request.retries + 1,
    )

    async def _go():
        from packages.core.briefing import generate_briefing
        from packages.core.database import create_worker_session
        from packages.core.ai.runtime import runtime_ensure_morning_briefing_billing_context
        async with create_worker_session()() as db:
            await runtime_ensure_morning_briefing_billing_context(
                db,
                workspace_id,
            )
            return await generate_briefing(
                db,
                workspace_id,
                timezone_name=timezone_name,
                user_id=user_id,
                require_autonomous_runtime=not await _scheduled_workspace_run_is_manual(
                    db, workspace_id=workspace_id, run_id=run_id, job_id_str=job_id_str,
                ),
            )

    return _run_scheduled(
        self, "Briefing", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=300,
    )


@celery_app.task(bind=True, max_retries=2)
def run_outcome_evaluation(
    self, workspace_id: str,
    *,
    run_id: str | None = None,
    job_id_str: str | None = None,
):
    """Label completed Strategist proposals — see strategist/evaluation.py.

    Compares predicted vs actual goal delta over each goal's
    ``outcome_window_days``, persists the label on the Task, populates
    GoalTaskLink.actual_impact, and writes ``learning`` memory entries
    when a clear bad-pattern emerges. Runs daily per workspace.
    """
    logger.info(
        "Outcome evaluation for workspace %s (attempt %d)",
        workspace_id, self.request.retries + 1,
    )

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.strategist.evaluation import evaluate_workspace_outcomes
        from packages.core.ai.runtime import runtime_ensure_outcome_evaluation_billing_context
        async with create_worker_session()() as db:
            await runtime_ensure_outcome_evaluation_billing_context(
                db,
                workspace_id,
            )
            result = await evaluate_workspace_outcomes(
                db, workspace_id,
                require_autonomous_runtime=not await _scheduled_workspace_run_is_manual(
                    db, workspace_id=workspace_id, run_id=run_id, job_id_str=job_id_str,
                ),
            )
            await db.commit()
            return result

    return _run_scheduled(
        self, "Outcome evaluation", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=900,
    )






@celery_app.task(bind=True, max_retries=2)
def run_chat_insight_extraction(
    self, workspace_id: str,
    *,
    run_id: str | None = None,
    job_id_str: str | None = None,
):
    """Extract operator preferences/guidance from recent workspace chat.

    See memory/chat_extractor.py. Runs every ~6h per workspace; uses an
    on-row bookmark so each pass only processes new messages.
    """
    logger.info(
        "Chat insight extraction for workspace %s (attempt %d)",
        workspace_id, self.request.retries + 1,
    )

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.memory.chat_extractor import extract_chat_insights
        from packages.core.ai.runtime import runtime_ensure_chat_insight_extraction_billing_context
        async with create_worker_session()() as db:
            await runtime_ensure_chat_insight_extraction_billing_context(
                db,
                workspace_id,
            )
            result = await extract_chat_insights(
                db, workspace_id,
                require_autonomous_runtime=not await _scheduled_workspace_run_is_manual(
                    db, workspace_id=workspace_id, run_id=run_id, job_id_str=job_id_str,
                ),
            )
            await db.commit()
            return result

    return _run_scheduled(
        self, "Chat extraction", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=900,
    )


@celery_app.task(bind=True, max_retries=1, name="memory.entity_chat_extraction_sweep")
def run_entity_chat_extraction_sweep(self):
    """Extract insights from entity-level (workspace-less) chats.

    The per-workspace extraction job never sees the main assistant chat
    (conversations with ``workspace_id IS NULL``), so preferences typed
    there never reached long-term memory. This sweep finds entities with
    recent entity-level operator messages and runs one bookmark-guarded
    extraction pass per entity.
    """
    logger.info(
        "Entity chat extraction sweep (attempt %d)", self.request.retries + 1
    )

    async def _go():
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text as sql_text

        from packages.core.database import create_worker_session
        from packages.core.memory.chat_extractor import (
            LOOKBACK_HOURS,
            extract_entity_chat_insights,
        )

        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        results = []
        async with create_worker_session()() as db:
            entity_ids = [
                row[0]
                for row in (await db.execute(sql_text("""
                    SELECT DISTINCT c.entity_id
                    FROM conversations c
                    JOIN messages m ON m.conversation_id = c.id
                    WHERE c.workspace_id IS NULL
                      AND m.author_kind = 'user'
                      AND m.created_at > :cutoff
                """), {"cutoff": cutoff})).all()
            ]
            for entity_id in entity_ids:
                try:
                    results.append(await extract_entity_chat_insights(db, entity_id))
                except Exception:
                    logger.warning(
                        "Entity chat extraction failed for %s",
                        entity_id, exc_info=True,
                    )
            await db.commit()
        return {"entities": len(entity_ids), "results": results}

    try:
        return _run_async(_go())
    except Exception as exc:
        logger.error("Entity chat extraction sweep failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=900)


@celery_app.task(bind=True, max_retries=2, name="learning.apply_candidate")
def apply_learning_candidate_async(
    self,
    entity_id: str,
    candidate_id: str,
    *,
    workspace_id: str | None = None,
    user_id: str | None = None,
):
    """Apply a queued learning candidate outside the chat/API request path."""
    logger.info(
        "Applying learning candidate %s (attempt %d)",
        candidate_id,
        self.request.retries + 1,
    )

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.services.runtime_learning import apply_queued_learning_candidate

        async with create_worker_session()() as db:
            row = await apply_queued_learning_candidate(
                db,
                entity_id=entity_id,
                candidate_id=candidate_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            await db.commit()
            if not row:
                return {"candidate_id": candidate_id, "status": "not_found"}
            return {"candidate_id": row.id, "status": row.status}

    try:
        return _run_async(_go())
    except Exception as exc:
        error_msg = str(exc)
        logger.error("learning candidate apply failed: %s", exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            async def _mark_failed():
                from packages.core.database import create_worker_session
                from packages.core.services.runtime_learning import mark_learning_candidate_apply_failed

                async with create_worker_session()() as db:
                    await mark_learning_candidate_apply_failed(
                        db,
                        entity_id=entity_id,
                        candidate_id=candidate_id,
                        workspace_id=workspace_id,
                        error=error_msg,
                    )
                    await db.commit()

            _run_async(_mark_failed())
            raise
        raise self.retry(exc=exc, countdown=60)


async def _execute_strategist_review_cycle(
    db,
    workspace_id: str,
    trigger,
    *,
    execution_owner: str | None = None,
    automation_context: dict | None = None,
    user_id: str | None = None,
) -> dict:
    """Compatibility wrapper around the reusable Review orchestrator."""
    from packages.core.strategist.orchestrator import run_strategist_review_cycle

    return await run_strategist_review_cycle(
        db,
        workspace_id,
        trigger,
        execution_owner=execution_owner,
        automation_context=automation_context,
        user_id=user_id,
    )


@celery_app.task(
    bind=True,
    max_retries=2,
    # Override app-level task_soft_time_limit=300. A review cycle collects
    # evidence and generates the next plan; 5 minutes is too tight and
    # surfaces Retry Strategist via SoftTimeLimitExceeded.
    soft_time_limit=1500,
    time_limit=1800,
)
def run_strategist_review(
    self, workspace_id: str, trigger: str | None = None,
    *,
    trigger_kind: str | None = None,
    trigger_detail: str | None = None,
    run_id: str | None = None, job_id_str: str | None = None,
    user_id: str | None = None,
):
    """Trigger one Strategist review cycle.

    Cadence comes from a ScheduledJob row tagged
    ``execution_type='strategist_review'`` (see strategist.scheduling).
    Manual triggers route through the same task via ``.apply_async()``.

    Wire format: producers send ``trigger_kind`` (a ``ReviewTriggerKind``
    value) plus optional ``trigger_detail`` prose. The second positional
    ``trigger`` argument is the legacy single-string form; it is kept ONLY
    so a worker that restarts mid-deploy can still drain messages that
    were queued before the split. Those are classified by
    ``ReviewTrigger.from_wire`` with a deprecation warning instead of
    crashing the worker.
    """
    from packages.core.strategist import ReviewTrigger

    review_trigger = ReviewTrigger.from_wire(
        trigger=trigger,
        trigger_kind=trigger_kind,
        trigger_detail=trigger_detail,
    )
    logger.info(
        "Strategist review for workspace %s (trigger=%s, attempt %d)",
        workspace_id, review_trigger.kind.value, self.request.retries + 1,
    )

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.ai.runtime import runtime_ensure_strategist_review_billing_context
        async with create_worker_session()() as db:
            await runtime_ensure_strategist_review_billing_context(
                db,
                workspace_id,
            )
            automation_context = None
            if (
                review_trigger.kind.value == "scheduled"
                and run_id
                and job_id_str
            ):
                automation_context = {
                    "scheduled_job_id": job_id_str,
                    "scheduled_run_id": run_id,
                }
            return await _execute_strategist_review_cycle(
                db,
                workspace_id,
                review_trigger,
                execution_owner=str(
                    self.request.id or f"strategist:{workspace_id}:{self.request.retries}"
                ),
                automation_context=automation_context,
                user_id=user_id,
            )

    async def _notify_failure(error_msg: str):
        await _post_strategist_failure_card(
            workspace_id=workspace_id,
            trigger=review_trigger,
            error_msg=error_msg,
            run_id=run_id,
            job_id_str=job_id_str,
        )

    return _run_scheduled(
        self, "Strategist review", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=300,
        on_terminal_error=_notify_failure,
    )


async def _scheduled_workspace_run_is_manual(
    db,
    *,
    workspace_id: str,
    run_id: str | None,
    job_id_str: str | None,
) -> bool:
    """Read manual intent from the exact Workspace's durable scheduler receipt."""
    if not run_id or not job_id_str:
        return False
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import is_manual_scheduled_occurrence

    run = (await db.execute(
        select(ScheduledJobRun)
        .join(ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id)
        .where(
            ScheduledJobRun.id == run_id,
            ScheduledJob.job_id == job_id_str,
            ScheduledJob.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    return is_manual_scheduled_occurrence(run, job_id=job_id_str)


async def _scheduled_goal_measurement_manual_scope(
    db,
    *,
    goal_id: str,
    job_id_str: str | None,
    run_id: str | None = None,
) -> tuple[str, str | None, str] | None:
    """Authorize and resolve billing scope for one exact manual Goal occurrence."""
    if not job_id_str or not run_id:
        return None

    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJob

    scheduled_job = (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.job_id == job_id_str,
            ScheduledJob.execution_type == "goal_measurement",
        )
    )).scalar_one_or_none()
    if scheduled_job is None:
        return None
    target = scheduled_job.execution_target or {}
    scheduled_goal_id = target.get("goal_id") or scheduled_job.goal_id
    if scheduled_goal_id != goal_id:
        return None
    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import manual_scheduled_request_user_id
    from packages.core.tasks.scheduler_tasks import _manual_scheduled_request_allowed

    run = await db.get(ScheduledJobRun, run_id)
    manual_user_id = manual_scheduled_request_user_id(run, job_id=scheduled_job.job_id)
    if not manual_user_id:
        return None
    if not await _manual_scheduled_request_allowed(db, scheduled_job, manual_user_id):
        raise ValueError("Manual Goal measurement requester is no longer authorized")
    return scheduled_job.entity_id, scheduled_job.workspace_id, manual_user_id


@celery_app.task(bind=True, max_retries=2)
def run_goal_measurement(
    self,
    goal_id: str,
    *,
    run_id: str | None = None,
    job_id_str: str | None = None,
):
    """Take one measurement of a Goal.

    Dispatched by the scheduler when a Goal's measurement_cadence
    fires (see scheduler_tasks.py + goals/scheduling.py). Delegates to
    the goals.measurement service which handles credential lease,
    adapter dispatch, value extraction, pace recompute, and event emit.
    """
    logger.info("Measuring goal %s (attempt %d)", goal_id, self.request.retries + 1)
    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.ai.runtime import (
            runtime_ensure_goal_measurement_billing_context,
            runtime_llm_billing_context,
        )
        from packages.core.ai.runtime.sources import RUNTIME_GOAL_MEASUREMENT_SOURCE
        from packages.core.goals.measurement import measure_goal

        # Set billing context in same event loop as the LLM call.
        async with create_worker_session()() as db:
            manual_scope = await _scheduled_goal_measurement_manual_scope(
                db,
                goal_id=goal_id,
                job_id_str=job_id_str,
                run_id=run_id,
            )
            if manual_scope is None:
                await runtime_ensure_goal_measurement_billing_context(db, goal_id)
        if manual_scope is not None:
            entity_id, workspace_id, manual_user_id = manual_scope
            async with runtime_llm_billing_context(
                entity_id, source=RUNTIME_GOAL_MEASUREMENT_SOURCE,
                workspace_id=workspace_id, user_id=manual_user_id,
            ):
                return await measure_goal(goal_id, require_autonomous_runtime=False)
        return await measure_goal(goal_id, require_autonomous_runtime=True)

    return _run_scheduled(
        self,
        "Goal measurement",
        goal_id,
        _go,
        run_id=run_id,
        job_id_str=job_id_str,
        countdown_on_retry=300,
    )


@celery_app.task(bind=True, max_retries=2)
def run_workspace_stat_collection(
    self,
    stat_id: str,
    *,
    run_id: str | None = None,
    job_id_str: str | None = None,
):
    """Collect one deterministic Workspace Stat observation.

    The Stat must belong to the scheduling job's company and, when the job has
    one, its Workspace; without a job to scope it nothing is collected. The
    scope is read from the job so the task signature stays wire-compatible.
    """
    logger.info("Collecting workspace stat %s (attempt %d)", stat_id, self.request.retries + 1)

    async def _go():
        from sqlalchemy import select
        from packages.core.database import create_worker_session
        from packages.core.models.scheduler import ScheduledJob
        from packages.core.models.workspace_stat import WorkspaceStat
        from packages.core.stats.service import StatError, collect_stat

        async with create_worker_session()() as db:
            job_scope = (await db.execute(
                select(ScheduledJob.entity_id, ScheduledJob.workspace_id).where(
                    ScheduledJob.job_id == job_id_str
                )
            )).first() if job_id_str else None
            if job_scope is None or not job_scope.entity_id:
                raise StatError(f"stat {stat_id} has no scheduling job to scope it")
            scope_entity_id, scope_workspace_id = job_scope
            stat_scope = [WorkspaceStat.id == stat_id, WorkspaceStat.entity_id == scope_entity_id]
            if scope_workspace_id:
                stat_scope.append(WorkspaceStat.workspace_id == scope_workspace_id)
            stat = (await db.execute(
                select(WorkspaceStat).where(*stat_scope)
            )).scalar_one_or_none()
            if stat is None:
                raise StatError(f"stat {stat_id} not found")
            observation = await collect_stat(db, stat)
            await db.commit()
            return {
                "stat_id": stat_id,
                "observation_id": observation.id,
                "value": float(observation.value),
            }

    return _run_scheduled(
        self,
        "Workspace stat collection",
        stat_id,
        _go,
        run_id=run_id,
        job_id_str=job_id_str,
        countdown_on_retry=300,
    )


def _task_terminal_result(task) -> dict[str, Any]:
    """Project one terminal Task row into the scheduler settlement contract."""

    actual_output = (
        dict(task.actual_output) if isinstance(task.actual_output, dict) else {}
    )
    details = task.details if isinstance(task.details, dict) else {}
    ai_result = details.get("ai_result")
    if not isinstance(ai_result, dict):
        ai_result = {}

    response = actual_output.get("response") or ai_result.get("response")
    structured_result = actual_output.get("result")
    if not response and isinstance(structured_result, dict):
        response = next(
            (
                structured_result.get(key)
                for key in ("response", "final_answer", "summary", "message")
                if isinstance(structured_result.get(key), str)
                and structured_result.get(key).strip()
            ),
            None,
        )
    if not response and structured_result is not None:
        response = (
            structured_result
            if isinstance(structured_result, str)
            else json.dumps(structured_result, ensure_ascii=False, default=str)
        )
    if not response and task.status == TaskStatus.FAILED.value:
        response = (
            actual_output.get("error_message")
            or actual_output.get("error")
            or "Coordinated Task execution failed"
        )

    result: dict[str, Any] = {
        "task_id": task.id,
        "status": task.status,
        "response": str(response or ""),
        "turns_used": actual_output.get("turns_used", 0),
        "runtime_kind": "coordinated_plan",
    }
    # acceptance_exceptions / unconfirmed_effects: a delivered agent-loop Task's
    # criteria left unmet or unverified, and its calls whose effect stayed
    # unconfirmed; the occurrence settles completed and still shows them.
    for key in (
        "error_type", "error", "error_message", "supervisor_verdict",
        "acceptance_exceptions", "unconfirmed_effects",
    ):
        value = actual_output.get(key)
        if value is not None:
            result[key] = value
    if task.started_at is not None and task.completed_at is not None:
        result["duration_ms"] = max(
            0,
            int((task.completed_at - task.started_at).total_seconds() * 1000),
        )

    # Agent Initiative remains a scheduler-level delivery contract even when
    # its Workspace execution is implemented by a coordinated Plan.
    if isinstance(structured_result, dict) and {
        "version",
        "decision",
    }.issubset(structured_result):
        result["initiative_decision"] = structured_result
        result["initiative_decision_error"] = None
    return result


async def _reject_inadmissible_initiative_start(db, task) -> str | None:
    """Re-check an Agent Initiative's authority as its Task starts.

    The scheduler admitted the occurrence earlier; the Initiative, its
    subscription or its conversation may have changed since. A Task without
    an Initiative marker passes. A rejected one is failed here (the caller
    commits) and the reason is returned.
    """

    from packages.core.services.agent_initiative_decision import (
        InitiativeAdmissionRejected,
        has_initiative_task_marker,
        initiative_task_marker,
        mark_initiative_run_started,
    )
    from packages.core.services.task_runtime_approvals import (
        fail_task_runtime_approval_task,
    )

    if not has_initiative_task_marker(task.details):
        return None
    if initiative_task_marker(task.details) is None:
        reason = "initiative_task_marker_invalid"
    else:
        try:
            await mark_initiative_run_started(db, task)
            return None
        except InitiativeAdmissionRejected as exc:
            reason = exc.reason_code
    await fail_task_runtime_approval_task(
        db, task=task, reason=reason, error_type="InitiativeAdmissionRejected",
    )
    return reason


async def _prepare_agent_task_runtime(
    session_factory,
    task_id: str,
    *,
    execution_claim=None,
):
    """Resolve the closed Task runtime and hand the Task to its run.

    ``ScheduledDispatchKind.AGENT_TASK`` is a durable transport name retained
    for rolling compatibility. The Task factory is authoritative: an
    executable Task's start intent is staged for its durable run (the agent
    loop), a terminal Task reports its result, and a Task with no runner
    fails. The result is always the gateway's terminal or monitor result.
    """

    from sqlalchemy import select

    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.models.task import Task
    from packages.core.services.task_execution import (
        resolve_task_execution,
        stage_task_execution_intent,
    )
    from packages.core.services.task_runtime_approvals import (
        lock_task_runtime_approval_boundary,
    )
    from packages.core.services.task_state_machine import apply_task_status_transition
    from packages.core.services.workspace_access import lock_workspace_access_boundary
    from packages.core.services.workflow_run_execution_claim import (
        commit_fenced_execution_boundary,
    )

    async with session_factory() as db:
        scope = (
            await db.execute(
                select(Task.id, Task.entity_id, Task.workspace_id).where(
                    Task.id == task_id
                )
            )
        ).one_or_none()
        if scope is None:
            return None, {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "error_type": "TaskNotFound",
                "error": "Task no longer exists",
                "response": "Task no longer exists",
            }

        entity_id = str(scope.entity_id)
        workspace_id = str(scope.workspace_id) if scope.workspace_id else None
        if workspace_id:
            workspace = await lock_workspace_access_boundary(
                db,
                workspace_id=workspace_id,
                entity_id=entity_id,
            )
            if workspace is None or getattr(workspace, "deleted_at", None) is not None:
                await db.rollback()
                return None, {
                    "task_id": task_id,
                    "status": TaskStatus.FAILED.value,
                    "error_type": "TaskRuntimeAdmissionError",
                    "error": "Task Workspace is unavailable",
                    "response": "Task Workspace is unavailable",
                }

        await lock_task_runtime_approval_boundary(db, task_id=task_id)
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id, Task.entity_id == entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None or (
            (str(task.workspace_id) if task.workspace_id else None) != workspace_id
        ):
            await db.rollback()
            return None, {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "error_type": "TaskRuntimeAdmissionError",
                "error": "Task scope changed during runtime admission",
                "response": "Task scope changed during runtime admission",
            }
        locked_task_id = str(task.id)
        locked_task_status = str(task.status)
        if task.status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.FAILED.value,
        }:
            terminal_result = _task_terminal_result(task)
            await db.rollback()
            return None, terminal_result

        dispatch = await resolve_task_execution(db, task)
        if dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
            if task.status in {
                TaskStatus.CREATED.value,
                TaskStatus.PENDING.value,
                TaskStatus.SCHEDULED.value,
            }:
                await apply_task_status_transition(
                    task,
                    TaskStatus.IN_PROGRESS.value,
                    db=db,
                )
                rejected = await _reject_inadmissible_initiative_start(db, task)
                if rejected is not None:
                    terminal_result = _task_terminal_result(task)
                    await commit_fenced_execution_boundary(
                        db.commit,
                        before_commit=(
                            execution_claim.raise_if_lost
                            if execution_claim is not None
                            else None
                        ),
                        after_commit=(
                            execution_claim.mark_terminal_committed
                            if execution_claim is not None
                            else None
                        ),
                        execution_claim=execution_claim,
                        session=db,
                    )
                    return None, terminal_result
            elif task.status != TaskStatus.IN_PROGRESS.value:
                if task.status in {
                    TaskStatus.WAITING_ON_CUSTOMER.value,
                    TaskStatus.ON_HOLD.value,
                    TaskStatus.BLOCKED.value,
                }:
                    monitor_result = {
                        "task_id": locked_task_id,
                        "status": locked_task_status,
                        "runtime_kind": dispatch.runtime_kind.value,
                        "coordinator_monitor": True,
                        "duplicate_suppressed": True,
                        "reason": "coordinator_task_not_runnable",
                    }
                    await db.rollback()
                    return None, monitor_result
                suppressed_result = {
                    "task_id": locked_task_id,
                    "status": locked_task_status,
                    "runtime_kind": dispatch.runtime_kind.value,
                    "duplicate_suppressed": True,
                    "reason": "coordinator_task_requires_explicit_start",
                }
                await db.rollback()
                return None, suppressed_result

            # Scheduler admission and the Workspace Task start outbox are two
            # independent durable ledgers. Stage the shared Task intent in the
            # same transaction as the pending -> in_progress transition; the
            # outbox consumer is the only publisher of initial Plan work.
            try:
                await stage_task_execution_intent(db, task, dispatch)
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=(
                        execution_claim.raise_if_lost
                        if execution_claim is not None
                        else None
                    ),
                    execution_claim=execution_claim,
                    session=db,
                )
            except Exception as exc:
                await db.rollback()
                raise _TaskRuntimeHandoffError(
                    f"Could not stage {dispatch.runtime_kind.value} for Task {task_id}"
                ) from exc
            return None, {
                "task_id": task_id,
                "status": TaskStatus.IN_PROGRESS.value,
                "runtime_kind": dispatch.runtime_kind.value,
                "coordinator_handoff": True,
                "coordinator_handoff_staged": True,
                "coordinator_monitor": True,
            }
        else:
            error = (
                "Agent Task delivery rejected by TaskExecution factory: "
                f"runtime={dispatch.runtime_kind.value}"
            )
            if dispatch.runtime_kind is TaskRuntimeKind.MANUAL:
                if task.status in {
                    TaskStatus.CREATED.value,
                    TaskStatus.PROPOSED.value,
                }:
                    await apply_task_status_transition(
                        task,
                        TaskStatus.IN_PROGRESS.value,
                        db=db,
                    )
                await apply_task_status_transition(
                    task,
                    TaskStatus.FAILED.value,
                    db=db,
                )
                task.actual_output = {
                    "task_status": TaskStatus.FAILED.value,
                    "error_type": "TaskRuntimeAdmissionError",
                    "error_message": error,
                    "response": error,
                }
                terminal_result = _task_terminal_result(task)
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=(
                        execution_claim.raise_if_lost
                        if execution_claim is not None
                        else None
                    ),
                    after_commit=(
                        execution_claim.mark_terminal_committed
                        if execution_claim is not None
                        else None
                    ),
                    execution_claim=execution_claim,
                    session=db,
                )
                return None, terminal_result
            rejected_result = {
                "task_id": locked_task_id,
                "status": TaskStatus.FAILED.value,
                "error_type": "TaskRuntimeAdmissionError",
                "error": error,
                "response": error,
                "runtime_kind": dispatch.runtime_kind.value,
                "duplicate_suppressed": True,
            }
            await db.rollback()
            return None, rejected_result

    raise RuntimeError("Task runtime admission produced no executable handoff")


async def _run_agent_task_with_execution_claim(
    session_factory,
    task_id: str,
    agent_id: str | None,
) -> tuple[bool, dict | None]:
    """Hand one Task to its run only while this delivery owns its execution lease.

    The run itself (the Task agent loop) owns every later approval and answer.
    """

    from packages.core.services.workflow_run_execution_claim import (
        agent_task_execution_claim,
    )

    del agent_id
    async with agent_task_execution_claim(task_id) as claim:
        if not claim:
            logger.info(
                "Agent task %s execution claim denied (%s)",
                task_id,
                claim.reason,
            )
            return False, None
        _, result = await _prepare_agent_task_runtime(
            session_factory,
            task_id,
            execution_claim=claim,
        )
        if result is None:
            raise RuntimeError("Task runtime factory returned no gateway result")
        return True, result


def _scheduled_agent_task_has_runtime_human_gate(task) -> bool:
    """Recognize a scheduled Task whose runtime waits on a person."""

    details = task.details if isinstance(getattr(task, "details", None), dict) else {}
    return bool(
        str(details.get("scheduled_run_id") or "").strip()
        and str(details.get("scheduled_job_id") or "").strip()
        and task.status == TaskStatus.WAITING_ON_CUSTOMER.value
    )


def _agent_claim_loss_suspension_result(task_id: str) -> dict[str, Any]:
    """Return a non-settling result for a stale claim-loss delivery."""

    return {
        "task_id": task_id,
        "status": TaskStatus.WAITING_ON_CUSTOMER.value,
        "duplicate_suppressed": True,
        "recovery_suspended": True,
        "reason": "scheduled_task_runtime_human_gate",
    }


async def _settle_lost_agent_task_execution_claim(
    session_factory,
    task_id: str,
    error: str,
) -> dict | None:
    """Fail an abandoned Agent Task only while holding fresh recovery ownership."""

    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import (
        TERMINAL_STATUSES,
        TaskStatusTransitionError,
        apply_task_status_transition,
    )
    from packages.core.services.task_runtime_approvals import (
        expire_task_runtime_approval_for_terminal,
        lock_task_runtime_approval_boundary,
    )
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )
    from packages.core.services.workflow_run_execution_claim import (
        agent_task_execution_claim,
        commit_fenced_execution_boundary,
    )

    async with agent_task_execution_claim(task_id) as recovery_claim:
        if not recovery_claim:
            logger.info(
                "Agent task %s claim-loss settlement deferred to current owner",
                task_id,
            )
            return None

        event_payload = None
        event_entity_id = None
        async with session_factory() as db:
            task_scope = (
                await db.execute(
                    select(Task.id, Task.entity_id, Task.workspace_id).where(
                        Task.id == task_id
                    )
                )
            ).one_or_none()
            if task_scope is None:
                recovery_claim.mark_terminal_committed()
                return {
                    "task_id": task_id,
                    "status": TaskStatus.FAILED.value,
                    "turns_used": 0,
                    "error_type": "TaskNotFound",
                    "error": "Agent task no longer exists",
                    "response": "Agent task no longer exists",
                }

            entity_id = str(task_scope.entity_id)
            workspace_id = (
                str(task_scope.workspace_id) if task_scope.workspace_id else None
            )
            if workspace_id:
                workspace = await lock_workspace_access_boundary(
                    db,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                )
                if workspace is None:
                    await db.rollback()
                    return None

            # This is a terminal writer.  Enter the same serialization domain
            # as request creation/resolution before taking the Task row lock,
            # so a guard-committed request cannot become an orphan behind it.
            await lock_task_runtime_approval_boundary(db, task_id=task_id)

            task = (
                await db.execute(
                    select(Task)
                    .where(
                        Task.id == task_id,
                        Task.entity_id == entity_id,
                        Task.workspace_id == workspace_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if task is None:
                recovery_claim.mark_terminal_committed()
                return {
                    "task_id": task_id,
                    "status": TaskStatus.FAILED.value,
                    "turns_used": 0,
                    "error_type": "TaskNotFound",
                    "error": "Agent task no longer exists",
                    "response": "Agent task no longer exists",
                }

            actual_output = (
                dict(task.actual_output)
                if isinstance(task.actual_output, dict)
                else {}
            )
            if task.status in TERMINAL_STATUSES:
                recovery_claim.mark_terminal_committed()
                response = str(
                    actual_output.get("response")
                    or actual_output.get("error_message")
                    or ""
                )
                return {
                    "task_id": task_id,
                    "status": task.status,
                    "turns_used": int(actual_output.get("turns_used") or 0),
                    "duration_ms": actual_output.get("duration_ms"),
                    "response": response,
                    "error": actual_output.get("error_message"),
                    "error_type": actual_output.get("error_type"),
                }

            if _scheduled_agent_task_has_runtime_human_gate(task):
                # An old claim-loss delivery has no authority to close a newer
                # human-gated generation.  Retire its durable intent while the
                # Task row is locked; already-published copies hit this same
                # final fence and become harmless no-ops.
                task_details = (
                    dict(task.details)
                    if isinstance(getattr(task, "details", None), dict)
                    else {}
                )
                had_intent = (
                    task_details.pop(_AGENT_CLAIM_LOSS_RECOVERY_KEY, None)
                    is not None
                )
                if had_intent:
                    task.details = task_details
                    await commit_fenced_execution_boundary(
                        db.commit,
                        execution_claim=recovery_claim,
                        session=db,
                        after_commit=recovery_claim.mark_terminal_committed,
                    )
                else:
                    await db.rollback()
                    recovery_claim.mark_terminal_committed()
                return _agent_claim_loss_suspension_result(task_id)

            # Close every still-live request, including the hard-crash window
            # where the guard committed a pending row but the Task checkpoint
            # and public marker were never written.
            await expire_task_runtime_approval_for_terminal(
                db,
                task=task,
                reason="agent_task_execution_claim_lost",
            )

            terminal_error_type = "AgentTaskExecutionClaimLost"
            terminal_error = error

            try:
                await apply_task_status_transition(task, "failed", db=db)
            except TaskStatusTransitionError:
                await db.rollback()
                logger.exception(
                    "Agent task %s claim-loss settlement found incompatible status %s",
                    task_id,
                    task.status,
                )
                return None

            task.actual_output = {
                **actual_output,
                "task_status": TaskStatus.FAILED.value,
                "turns_used": 0,
                "error_type": terminal_error_type,
                "error_message": terminal_error,
                "response": terminal_error,
            }
            task_details = (
                dict(task.details)
                if isinstance(getattr(task, "details", None), dict)
                else {}
            )
            task_details.pop(_AGENT_CLAIM_LOSS_RECOVERY_KEY, None)
            task.details = task_details
            result = {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "turns_used": 0,
                "error_type": terminal_error_type,
                "error": terminal_error,
                "response": terminal_error,
            }
            event_entity_id = entity_id
            event_payload = {
                "task_id": task.id,
                "title": task.title,
                "task_status": TaskStatus.FAILED.value,
                "error_type": terminal_error_type,
                "error_message": terminal_error,
            }
            await commit_fenced_execution_boundary(
                db.commit,
                execution_claim=recovery_claim,
                session=db,
                after_commit=recovery_claim.mark_terminal_committed,
            )

        if event_payload is not None and event_entity_id is not None:
            try:
                from packages.core.services import event_emitter

                event_emitter.emit(
                    event_entity_id,
                    "task.failed",
                    source="ai_tasks",
                    payload=event_payload,
                )
            except Exception:
                logger.debug(
                    "Agent task %s claim-loss event emit failed",
                    task_id,
                    exc_info=True,
                )
        return result


async def _persist_agent_claim_loss_recovery_intent(
    session_factory,
    task_id: str,
    *,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
    error: str,
    publish_claim_id: str | None = None,
) -> AgentClaimLossRecoveryIntentState:
    """Commit a replay-safe recovery handoff without claiming business ownership."""

    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import TERMINAL_STATUSES
    from packages.core.services.workflow_run_execution_claim import (
        AGENT_TASK_EXECUTION_RECHECK_SECONDS,
    )
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    async with session_factory() as db:
        task_scope = (
            await db.execute(
                select(Task.id, Task.entity_id, Task.workspace_id).where(
                    Task.id == task_id
                )
            )
        ).one_or_none()
        if task_scope is None:
            await db.rollback()
            return AgentClaimLossRecoveryIntentState.TASK_MISSING

        entity_id = str(task_scope.entity_id)
        workspace_id = (
            str(task_scope.workspace_id) if task_scope.workspace_id else None
        )
        if workspace_id:
            workspace = await lock_workspace_access_boundary(
                db,
                workspace_id=workspace_id,
                entity_id=entity_id,
            )
            if workspace is None:
                await db.rollback()
                raise RuntimeError(
                    f"Agent task {task_id} recovery Workspace is missing"
                )

        task = (
            await db.execute(
                select(Task)
                .where(
                    Task.id == task_id,
                    Task.entity_id == entity_id,
                    Task.workspace_id == workspace_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            await db.rollback()
            return AgentClaimLossRecoveryIntentState.TASK_MISSING
        if task.status in TERMINAL_STATUSES:
            await db.rollback()
            return AgentClaimLossRecoveryIntentState.TASK_TERMINAL

        if _scheduled_agent_task_has_runtime_human_gate(task):
            details = dict(task.details) if isinstance(task.details, dict) else {}
            had_intent = details.pop(_AGENT_CLAIM_LOSS_RECOVERY_KEY, None) is not None
            if had_intent:
                task.details = details
                await db.commit()
            else:
                await db.rollback()
            return AgentClaimLossRecoveryIntentState.SUSPENDED

        details = dict(task.details) if isinstance(task.details, dict) else {}
        existing = details.get(_AGENT_CLAIM_LOSS_RECOVERY_KEY)
        existing_intent = dict(existing) if isinstance(existing, dict) else {}
        details[_AGENT_CLAIM_LOSS_RECOVERY_KEY] = (
            AgentClaimLossRecoveryIntentFactory.persist(
                existing_intent,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
                error=error,
                now=datetime.now(timezone.utc),
                retry_after_seconds=(
                    AGENT_TASK_EXECUTION_RECHECK_SECONDS
                    + SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS
                ),
                publish_claim_id=publish_claim_id,
            )
        )
        task.details = details
        await db.commit()
        return AgentClaimLossRecoveryIntentState.PERSISTED


async def _recover_agent_task_claim_loss(
    session_factory,
    task_id: str,
    *,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
    error: str,
) -> tuple[AgentClaimLossRecoveryIntentState, dict | None]:
    """Persist recovery ownership, then attempt only the fenced settlement."""

    intent_state = await _persist_agent_claim_loss_recovery_intent(
        session_factory,
        task_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
        error=error,
    )
    if intent_state is AgentClaimLossRecoveryIntentState.SUSPENDED:
        return intent_state, _agent_claim_loss_suspension_result(task_id)
    result = await _settle_lost_agent_task_execution_claim(
        session_factory,
        task_id,
        error,
    )
    return intent_state, result


def _agent_claim_loss_recovery_signature(
    *,
    task_id: str,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
):
    """Build a rolling-deploy-safe settlement-only delivery."""

    return recover_agent_task_claim_loss.s(
        task_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
    ).set(queue=CeleryQueue.RECOVERY_V2.value)


def _enqueue_agent_claim_loss_recovery(
    *,
    task_id: str,
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
    countdown: float = 0,
) -> None:
    _agent_claim_loss_recovery_signature(
        task_id=task_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
    ).apply_async(countdown=countdown)


async def _load_agent_claim_loss_recovery_intents(
    session_factory,
    *,
    now: datetime | None = None,
    limit: int = _AGENT_CLAIM_LOSS_RECOVERY_BATCH,
) -> list[dict]:
    """Claim one due recovery batch without holding a DB txn during publish."""

    from sqlalchemy import Float, case, cast, func, or_, select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    async with session_factory() as db:
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            raise ValueError("Agent claim-loss recovery time must be timezone-aware")
        next_attempt_value = Task.details[_AGENT_CLAIM_LOSS_RECOVERY_KEY][
            AgentClaimLossRecoveryIntentField.NEXT_ATTEMPT_AT.value
        ]
        next_attempt_at = case(
            (
                func.jsonb_typeof(next_attempt_value) == "number",
                cast(next_attempt_value.astext, Float),
            ),
            else_=None,
        )
        tasks = list((await db.execute(
            select(Task)
            .where(
                Task.status.notin_(TERMINAL_STATUSES),
                Task.details.has_key(  # type: ignore[attr-defined]  # noqa: W601
                    _AGENT_CLAIM_LOSS_RECOVERY_KEY
                ),
                or_(
                    next_attempt_at.is_(None),
                    next_attempt_at <= checked_at.timestamp(),
                ),
            )
            .order_by(
                func.coalesce(next_attempt_at, 0).asc(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(max(1, int(limit)))
        )).scalars())

        candidates: list[dict] = []
        for task in tasks:
            details = dict(task.details) if isinstance(task.details, dict) else {}
            if _scheduled_agent_task_has_runtime_human_gate(task):
                details.pop(_AGENT_CLAIM_LOSS_RECOVERY_KEY, None)
                task.details = details
                continue
            intent = details.get(_AGENT_CLAIM_LOSS_RECOVERY_KEY)
            claimed_intent = AgentClaimLossRecoveryIntentFactory.claim(
                intent,
                now=checked_at,
            )
            candidates.append(
                AgentClaimLossRecoveryIntentFactory.candidate(
                    task.id,
                    claimed_intent,
                )
            )
            details[_AGENT_CLAIM_LOSS_RECOVERY_KEY] = claimed_intent
            task.details = details
        await db.commit()
        return candidates


async def _retry_agent_claim_loss_recovery_intent(
    session_factory,
    task_id: str,
    *,
    expected_sweep_attempt: int,
    expected_publish_claim_id: str,
    now: datetime | None = None,
) -> bool:
    """Release a failed broker handoff without shortening a newer claim."""

    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("Agent claim-loss recovery time must be timezone-aware")
    async with session_factory() as db:
        task = (await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None or task.status in TERMINAL_STATUSES:
            await db.rollback()
            return False
        details = dict(task.details) if isinstance(task.details, dict) else {}
        released = AgentClaimLossRecoveryIntentFactory.release_publish_claim(
            details.get(_AGENT_CLAIM_LOSS_RECOVERY_KEY),
            expected_sweep_attempt=expected_sweep_attempt,
            expected_publish_claim_id=expected_publish_claim_id,
            now=checked_at,
        )
        if released is None:
            await db.rollback()
            return False
        details[_AGENT_CLAIM_LOSS_RECOVERY_KEY] = released
        task.details = details
        await db.commit()
        return True


@celery_app.task(
    bind=True,
    name="agent.recover_claim_loss_v2",
    max_retries=SCHEDULED_SETTLEMENT_MAX_RETRIES,
)
def recover_agent_task_claim_loss(
    self,
    task_id: str,
    *,
    scheduled_run_id: str | None = None,
    scheduled_job_id: str | None = None,
):
    """Settle a lost Agent claim without ever replaying TaskRunner."""

    from packages.core.database import create_worker_session

    error = "Agent task execution claim was lost"
    session_factory = create_worker_session()
    try:
        _intent_state, result = _run_async(_recover_agent_task_claim_loss(
            session_factory,
            task_id,
            scheduled_run_id=scheduled_run_id,
            scheduled_job_id=scheduled_job_id,
            error=error,
        ))
        if result is None:
            raise RuntimeError("Agent task claim-loss settlement is still owned")
        if result.get("recovery_suspended"):
            return result
        _update_job_run_status(
            session_factory,
            task_id,
            result,
            scheduled_run_id=scheduled_run_id,
            scheduled_job_id=scheduled_job_id,
            celery_task=self,
        )
        return result
    except (Ignore, Retry):
        raise
    except Exception as exc:
        raise self.retry(exc=exc, countdown=SCHEDULED_RECOVERY_RETRY_SECONDS)


@celery_app.task(name="agent.recover_claim_loss_sweep_v2")
def recover_agent_task_claim_loss_sweep():
    """Republish durable claim-loss intents after broker/task-chain failures."""

    from packages.core.database import create_worker_session

    candidates = _run_async(_load_agent_claim_loss_recovery_intents(
        create_worker_session()
    ))
    queued = 0
    for candidate in candidates:
        delivery = {
            "task_id": candidate["task_id"],
            "scheduled_run_id": candidate["scheduled_run_id"],
            "scheduled_job_id": candidate["scheduled_job_id"],
        }
        try:
            _enqueue_agent_claim_loss_recovery(**delivery)
            queued += 1
        except Exception:
            logger.exception(
                "Agent task %s durable claim-loss recovery could not be queued",
                candidate["task_id"],
            )
            try:
                _run_async(_retry_agent_claim_loss_recovery_intent(
                    create_worker_session(),
                    candidate["task_id"],
                    expected_sweep_attempt=candidate["_sweep_attempt"],
                    expected_publish_claim_id=candidate["_publish_claim_id"],
                ))
            except Exception:
                logger.exception(
                    "Agent task %s claim-loss broker retry could not be released",
                    candidate["task_id"],
                )
    return {"found": len(candidates), "queued": queued}


@celery_app.task(bind=True, max_retries=3, soft_time_limit=1500, time_limit=1800)
def run_agent_task(
    self,
    task_id: str,
    agent_id: str | None = None,
    *,
    scheduled_run_id: str | None = None,
    scheduled_job_id: str | None = None,
    claim_recheck: bool = False,
):
    """Admit a durable Task transport delivery through its runtime factory.

    The Celery name and signature remain compatible with scheduled/legacy
    envelopes. Executable Tasks stage their durable run's start intent (the
    Task agent loop); this gateway then settles scheduled occurrences.
    """
    logger.info(
        "Running agent task: task=%s agent=%s (attempt %d)",
        task_id,
        agent_id,
        self.request.retries + 1,
    )
    session_factory = None
    from packages.core.services.workflow_run_execution_claim import (
        AgentTaskExecutionClaimLost,
    )
    try:
        from packages.core.database import create_worker_session
        session_factory = create_worker_session()
        claimed, result = _run_async(_run_scheduled_agent_task_if_open(
            session_factory,
            task_id,
            agent_id,
            scheduled_run_id=scheduled_run_id,
            scheduled_job_id=scheduled_job_id,
            claim_recheck=claim_recheck,
        ))
        if not claimed:
            if result and (
                result.get("scheduled_occurrence_closed")
                or result.get("recovery_suspended")
            ):
                return result
            if claim_recheck:
                return {
                    "task_id": task_id,
                    "status": "in_progress",
                    "duplicate_suppressed": True,
                    "recheck_scheduled": False,
                }
            from packages.core.services.workflow_run_execution_claim import (
                AGENT_TASK_EXECUTION_RECHECK_SECONDS,
            )

            # A denied delivery normally means another healthy worker is
            # running. It can also be a PostgreSQL row lease left by a worker
            # that died before releasing it, so ack only after publishing one
            # durable recheck after the lease TTL. The Task terminal guard
            # makes that recheck a no-op when the original worker finishes.
            self.apply_async(
                args=[task_id, agent_id],
                kwargs={
                    "scheduled_run_id": scheduled_run_id,
                    "scheduled_job_id": scheduled_job_id,
                    "claim_recheck": True,
                },
                countdown=AGENT_TASK_EXECUTION_RECHECK_SECONDS,
            )
            return {
                "task_id": task_id,
                "status": "in_progress",
                "duplicate_suppressed": True,
                "recheck_scheduled": True,
            }
        if result is None:
            raise RuntimeError("Agent task execution returned no result")
        if result.get("coordinator_monitor"):
            recheck_scheduled = False
            # A claim_recheck delivery is the scheduler sweep's own bounded
            # probe (one per run per SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
            # fenced by scheduled_execution_recovery_next_attempt_at). Arming
            # another recheck from it starts a chain that never ends while the
            # Plan stays non-terminal — the recheck storm.
            if scheduled_run_id and not claim_recheck:
                try:
                    self.apply_async(
                        args=[task_id, agent_id],
                        kwargs={
                            "scheduled_run_id": scheduled_run_id,
                            "scheduled_job_id": scheduled_job_id,
                            "claim_recheck": True,
                        },
                        countdown=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
                    )
                    recheck_scheduled = True
                except Exception:
                    # The ScheduledJobRun remains RUNNING with an admitted
                    # child. Its durable recovery sweep republishes this exact
                    # gateway delivery, so a lost optimization must not fail
                    # the Task or settle the occurrence.
                    logger.exception(
                        "Could not publish coordinator monitor for scheduled "
                        "Task %s; durable scheduler recovery remains armed",
                        task_id,
                    )
            return {**result, "recheck_scheduled": recheck_scheduled}
        if result.get("reason") == "workspace_autonomy_paused":
            # Only the original delivery polls for autonomy to resume; a
            # claim_recheck delivery is already the sweep's periodic probe.
            if claim_recheck:
                return {**result, "recheck_scheduled": False}
            self.apply_async(
                args=[task_id, agent_id],
                kwargs={"scheduled_run_id": scheduled_run_id, "scheduled_job_id": scheduled_job_id},
                countdown=60,
            )
            return {**result, "recheck_scheduled": True}
        logger.info(
            "Agent task completed: task=%s status=%s turns=%s",
            task_id,
            result.get("status"),
            result.get("turns_used"),
        )

        # A HITL wait keeps the same scheduled occurrence open.  The approval
        # continuation will settle it after the Task reaches a terminal state.
        if _scheduled_agent_terminal_status(result) is not None:
            _update_job_run_status(
                session_factory,
                task_id,
                result,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
                celery_task=self,
            )

        return result
    except (Ignore, Retry):
        raise
    except _TaskRuntimeHandoffError as exc:
        logger.error("Task %s runtime handoff failed: %s", task_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=30 * (2 ** self.request.retries),
            )
        if scheduled_run_id:
            # The admitted ScheduledJobRun is the durable retry intent. Keep it
            # open; the scheduler recovery sweep will republish this transport
            # envelope without rerunning TaskRunner or closing the occurrence.
            return {
                "task_id": task_id,
                "status": TaskStatus.IN_PROGRESS.value,
                "runtime_handoff_deferred": True,
                "recheck_scheduled": False,
                "error": str(exc),
            }
        _mark_task_failed(
            task_id,
            str(exc),
            error_type=type(exc).__name__,
        )
        return {
            "task_id": task_id,
            "status": TaskStatus.FAILED.value,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "response": str(exc),
        }
    except AgentTaskExecutionClaimLost:
        error = "Agent task execution claim was lost"
        logger.error("Agent task %s stopped after losing its execution claim", task_id)
        result = None
        if session_factory is not None:
            try:
                result = _run_async(_settle_lost_agent_task_execution_claim(
                    session_factory,
                    task_id,
                    error,
                ))
            except Exception:
                logger.exception(
                    "Agent task %s fresh claim-loss settlement failed",
                    task_id,
                )
        if result is not None and session_factory is not None:
            if not result.get("recovery_suspended"):
                _update_job_run_status(
                    session_factory,
                    task_id,
                    result,
                    scheduled_run_id=scheduled_run_id,
                    scheduled_job_id=scheduled_job_id,
                    celery_task=self,
                )
            return result

        from packages.core.services.workflow_run_execution_claim import (
            AGENT_TASK_EXECUTION_RECHECK_SECONDS,
        )

        intent_state = None
        intent_error = None
        publish_claim_id = uuid4().hex
        if session_factory is not None:
            try:
                intent_state = _run_async(_persist_agent_claim_loss_recovery_intent(
                    session_factory,
                    task_id,
                    scheduled_run_id=scheduled_run_id,
                    scheduled_job_id=scheduled_job_id,
                    error=error,
                    publish_claim_id=publish_claim_id,
                ))
            except Exception as exc:
                intent_error = exc
                logger.exception(
                    "Agent task %s claim-loss recovery intent could not be persisted",
                    task_id,
                )

        if intent_state is AgentClaimLossRecoveryIntentState.SUSPENDED:
            return _agent_claim_loss_suspension_result(task_id)

        recheck_scheduled = False
        publish_error = None
        try:
            _enqueue_agent_claim_loss_recovery(
                task_id=task_id,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
                countdown=AGENT_TASK_EXECUTION_RECHECK_SECONDS,
            )
            recheck_scheduled = True
        except Exception as exc:
            publish_error = exc
            logger.exception(
                "Agent task %s claim-loss settlement recovery could not be queued",
                task_id,
            )
            if (
                session_factory is not None
                and intent_state is AgentClaimLossRecoveryIntentState.PERSISTED
            ):
                try:
                    _run_async(_retry_agent_claim_loss_recovery_intent(
                        session_factory,
                        task_id,
                        expected_sweep_attempt=0,
                        expected_publish_claim_id=publish_claim_id,
                    ))
                except Exception:
                    logger.exception(
                        "Agent task %s initial broker retry could not be released",
                        task_id,
                    )

        if intent_state is None and not recheck_scheduled:
            raise RuntimeError(
                "Agent task claim-loss recovery had no durable handoff"
            ) from (publish_error or intent_error)
        return {
            "task_id": task_id,
            "status": TaskStatus.IN_PROGRESS.value,
            "duplicate_suppressed": True,
            "claim_loss_settlement_deferred": True,
            "recheck_scheduled": recheck_scheduled,
            "recovery_intent": (
                intent_state.value if intent_state is not None else None
            ),
        }
    except LLMRateLimited as exc:
        logger.warning(
            "Agent task %s rate limited; provider requested %.1fs backoff",
            task_id,
            exc.retry_after,
        )
        if self.request.retries >= self.max_retries:
            _mark_task_failed(
                task_id,
                f"Provider remained rate limited after {self.max_retries + 1} attempts: {exc}",
                error_type=type(exc).__name__,
            )
            result = {
                "task_id": task_id,
                "status": "failed",
                "turns_used": 0,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "response": str(exc),
            }
            if session_factory is not None:
                _update_job_run_status(
                    session_factory,
                    task_id,
                    result,
                    scheduled_run_id=scheduled_run_id,
                    scheduled_job_id=scheduled_job_id,
                    celery_task=self,
                )
            return result
        raise self.retry(
            exc=RuntimeError(str(exc)),
            countdown=max(1, int(exc.retry_after)),
        )
    except CreditExhaustedError as exc:
        logger.warning("Agent task %s aborted: credits exhausted", task_id)
        _mark_task_failed(
            task_id,
            str(exc),
            error_type=type(exc).__name__,
        )
        result = {
            "task_id": task_id,
            "status": "failed",
            "turns_used": 0,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "response": str(exc),
        }
        if session_factory is not None:
            _update_job_run_status(
                session_factory,
                task_id,
                result,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
                celery_task=self,
            )
        return result
    except TaskRequesterIdentityError as exc:
        logger.error("Agent task %s rejected invalid requester identity: %s", task_id, exc)
        _mark_task_failed(
            task_id,
            str(exc),
            error_type=type(exc).__name__,
        )
        result = {
            "task_id": task_id,
            "status": "failed",
            "turns_used": 0,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if session_factory is not None:
            _update_job_run_status(
                session_factory,
                task_id,
                result,
                scheduled_run_id=scheduled_run_id,
                scheduled_job_id=scheduled_job_id,
                celery_task=self,
            )
        return result
    except Exception as exc:
        logger.error("Agent task %s failed: %s", task_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _mark_task_failed(
                task_id,
                f"Agent execution failed after {self.max_retries + 1} attempts: {exc}",
                error_type=type(exc).__name__,
            )
            result = {
                "task_id": task_id,
                "status": "failed",
                "turns_used": 0,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "response": str(exc),
            }
            if session_factory is not None:
                _update_job_run_status(
                    session_factory,
                    task_id,
                    result,
                    scheduled_run_id=scheduled_run_id,
                    scheduled_job_id=scheduled_job_id,
                    celery_task=self,
                )
            return result
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


def _update_job_run_status(
    session_factory,
    task_id: str,
    result: dict,
    *,
    scheduled_run_id: str | None = None,
    scheduled_job_id: str | None = None,
    celery_task=None,
):
    """Update the ScheduledJobRun and ScheduledJob status after agent execution."""
    import asyncio

    if _scheduled_agent_terminal_status(result) is None:
        return

    try:
        asyncio.run(_update_job_run_status_async(
            session_factory,
            task_id,
            result,
            scheduled_run_id=scheduled_run_id,
            scheduled_job_id=scheduled_job_id,
        ))
    except Exception as e:
        logger.warning("Failed to update job run status for task %s: %s", task_id, e)
        if scheduled_run_id:
            try:
                _run_async(_defer_scheduled_agent_settlement(
                    task_id=task_id,
                    run_id=scheduled_run_id,
                    job_id_str=scheduled_job_id,
                    result=result,
                ))
            except _ScheduledSettlementHandoffError as handoff:
                logger.exception(
                    "Failed to defer scheduled agent settlement task=%s run=%s",
                    task_id,
                    scheduled_run_id,
                )
                if celery_task is None:
                    raise
                return _replace_with_scheduled_settlement(
                    celery_task,
                    run_id=scheduled_run_id,
                    job_id_str=scheduled_job_id,
                    settlement=handoff.settlement,
                )


async def _defer_scheduled_agent_settlement(
    *,
    task_id: str,
    run_id: str,
    job_id_str: str | None,
    result: dict,
) -> None:
    from packages.core.services.scheduled_run_lifecycle import ScheduledRunSettlement

    status = _scheduled_agent_terminal_status(result)
    if status is None:
        return
    settlement = ScheduledRunSettlement.create(
        kind=ScheduledSettlementKind.AGENT_TASK,
        child_id=task_id,
        status=status,
        result=result,
        error=(
            str(
                result.get("response")
                or result.get("error")
                or result.get("error_type")
                or "Agent task failed"
            )[:500]
            if status is ScheduledRunStatus.ERROR
            else None
        ),
    )
    persisted = False
    enqueued = False
    try:
        persisted = await _persist_scheduled_run_settlement(
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=settlement,
        )
    except Exception:
        logger.exception(
            "Failed to persist scheduled agent settlement fence task=%s run=%s",
            task_id,
            run_id,
        )
    try:
        _enqueue_scheduled_run_settlement(
            run_id=run_id,
            job_id_str=job_id_str,
            settlement=settlement,
        )
        enqueued = True
    except Exception:
        logger.exception(
            "Failed to queue scheduled agent settlement task=%s run=%s",
            task_id,
            run_id,
        )
    if not persisted and not enqueued:
        raise _ScheduledSettlementHandoffError(settlement)


async def _update_job_run_status_async(
    session_factory,
    task_id: str,
    result: dict,
    *,
    scheduled_run_id: str | None = None,
    scheduled_job_id: str | None = None,
):
    """Finalize a scheduled agent run and deliver successful Chat results once."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.scheduler_service import (
        lock_scheduled_job_and_run,
        notify_scheduled_job_auto_paused,
        reconcile_scheduled_job_run_projection,
    )

    status = _scheduled_agent_terminal_status(result)
    if status is None:
        return

    projected_message = None
    projection_retry_run_id = None
    projected_entity_id = None
    projected_workspace_id = None
    initiative_delivery_run_id = None
    async with session_factory() as db:
        task = (
            await db.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None and scheduled_run_id is None:
            return
        details = (task.details or {}) if task is not None else {}
        run_id = (
            scheduled_run_id
            if scheduled_run_id is not None
            else details.get("scheduled_run_id")
        )
        job_id_str = (
            scheduled_job_id
            if scheduled_job_id is not None
            else details.get("scheduled_job_id")
        )
        if not run_id and not job_id_str:
            return

        duration_ms = result.get("duration_ms")
        error_msg = None
        if status is ScheduledRunStatus.ERROR:
            error_msg = str(
                result.get("response")
                or result.get("error")
                or result.get("error_type")
                or "Agent task failed"
            )[:500]
        job, run = await lock_scheduled_job_and_run(
            db,
            job_id=job_id_str,
            run_id=run_id,
        )
        if run_id and run is None:
            await db.rollback()
            return
        run_was_running = False
        if run is not None:
            if run and run.status == "running":
                from packages.core.services.scheduled_run_lifecycle import (
                    ScheduledRunOutcome,
                    apply_scheduled_run_outcome,
                )

                run_was_running = True
                apply_scheduled_run_outcome(
                    run,
                    ScheduledRunOutcome.from_execution(
                        status=status,
                        result=result,
                        error=error_msg,
                    ),
                    completed_at=datetime.now(timezone.utc),
                )
                if duration_ms is not None:
                    run.duration_ms = duration_ms

        if job and run_was_running and run_id:
            auto_paused = await reconcile_scheduled_job_run_projection(
                db,
                job,
                finalized_run_id=run_id,
            )
            if auto_paused:
                await notify_scheduled_job_auto_paused(
                    db,
                    job,
                    failure_key=run_id,
                )
            if run is not None:
                from packages.core.ledger.adapters import record_automation_run_finished

                await record_automation_run_finished(
                    db,
                    job,
                    run_id=run_id,
                    status=status.value,
                )

        if (
            run_was_running
            and job
            and task is not None
            and status is ScheduledRunStatus.SUCCESS
        ):
            from packages.core.services.scheduled_run_lifecycle import (
                apply_scheduled_result_projection,
                defer_scheduled_recovery,
            )

            projection = _scheduled_agent_result_projection(
                task=task,
                job=job,
                result=result,
            )
            if projection is not None and run is not None:
                apply_scheduled_result_projection(run, projection)
                await db.flush()
                try:
                    async with db.begin_nested():
                        projected_message = await _deliver_scheduled_agent_result(
                            db,
                            task=task,
                            job=job,
                            run_id=run_id,
                            result=result,
                            force_workspace_chat=True,
                        )
                except Exception as exc:
                    await db.refresh(run)
                    defer_scheduled_recovery(
                        run,
                        kind=ScheduledRecoveryKind.RESULT_PROJECTION,
                        now=datetime.now(timezone.utc),
                        retry_after=timedelta(
                            seconds=(
                                SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS
                            )
                        ),
                    )
                    apply_scheduled_result_projection(
                        run,
                        projection.with_state(
                            ScheduledResultProjectionState.PENDING,
                            error=str(exc),
                        ),
                    )
                    projection_retry_run_id = run.id
                    logger.warning(
                        "Scheduled agent Chat projection deferred task=%s run=%s",
                        task_id,
                        run_id,
                        exc_info=True,
                    )
                else:
                    apply_scheduled_result_projection(
                        run,
                        projection.with_state(
                            ScheduledResultProjectionState.DELIVERED,
                        ),
                    )
                    projected_entity_id = task.entity_id
                    projected_workspace_id = task.workspace_id

        startup_workspace_id = (
            str(job.workspace_id)
            if job is not None
            and job.workspace_id
            and run_was_running
            and status is ScheduledRunStatus.SUCCESS
            else None
        )
        await db.commit()
        from packages.core.services.agent_initiative_decision import (
            initiative_task_marker,
            settle_initiative_task,
        )

        if task is not None and initiative_task_marker(task.details) is not None:
            initiative_delivery_run_id = await settle_initiative_task(
                session_factory,
                task_id=task.id,
                result=result,
                scheduled_run_id=run_id,
                scheduled_job_id=job_id_str,
            )
            if initiative_delivery_run_id is not None:
                _enqueue_agent_initiative_delivery(initiative_delivery_run_id)
        if startup_workspace_id:
            try:
                from packages.core.services.blueprint_startup_service import (
                    reconcile_blueprint_startup,
                )

                await reconcile_blueprint_startup(
                    db,
                    workspace_id=startup_workspace_id,
                    trigger="scheduled_job_success",
                )
            except Exception:
                await db.rollback()
                logger.exception(
                    "Blueprint startup reconciliation failed after scheduled "
                    "job success workspace=%s run=%s",
                    startup_workspace_id,
                    run_id,
                )
        if projected_message is not None:
            from packages.core.workspace_chat import service as chat_service

            await chat_service.publish_workspace_chat_message_event(
                projected_entity_id,
                workspace_id=projected_workspace_id,
                message=projected_message,
            )
        elif projection_retry_run_id is not None:
            try:
                _enqueue_scheduled_agent_result_projection(projection_retry_run_id)
            except Exception:
                logger.exception(
                    "Failed to queue scheduled Agent result projection run=%s",
                    projection_retry_run_id,
                )


def _scheduled_agent_result_projection(*, task, job, result: dict):
    """Build a durable Chat projection intent only for eligible final output."""
    from packages.core.services.agent_initiative_decision import (
        has_initiative_task_marker,
    )

    if (
        getattr(job, "execution_type", None) == "agent_initiative"
        or has_initiative_task_marker(getattr(task, "details", None))
    ):
        return None
    delivery_mode = str(
        job.default_delivery_mode
        or (task.details or {}).get("default_delivery_mode")
        or ""
    ).strip()
    response = str(result.get("response") or "").strip()
    if (
        delivery_mode != "workspace_chat"
        or not task.entity_id
        or not task.workspace_id
        or not response
    ):
        return None

    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
    )

    return ScheduledResultProjection.workspace_chat(task_id=task.id)


def _scheduled_projection_task_matches_lineage(*, task, job, run) -> bool:
    """Require a projection Task to belong to the exact Job occurrence scope."""

    details = task.details if isinstance(task.details, dict) else {}
    return bool(
        task.entity_id
        and task.workspace_id
        and str(task.entity_id) == str(job.entity_id)
        and str(task.workspace_id) == str(job.workspace_id)
        and str(details.get("scheduled_job_id") or "") == str(job.job_id)
        and str(details.get("scheduled_run_id") or "") == str(run.id)
    )


def _enqueue_scheduled_agent_result_projection(run_id: str) -> None:
    project_scheduled_agent_result.apply_async(args=[run_id])


async def _ensure_scheduled_result_projection_recovery_chain(
    session_factory,
    run_id: str,
    recovery_chain_id: str,
) -> bool:
    """Count one accepted Celery chain once, using its stable task id."""

    from packages.core.services.scheduler_service import lock_scheduled_job_and_run
    from packages.core.services.scheduled_run_lifecycle import (
        apply_scheduled_result_projection,
        scheduled_result_projection,
    )

    normalized_chain_id = str(recovery_chain_id or "").strip()
    if not normalized_chain_id:
        raise ValueError("scheduled result projection recovery requires a task id")

    async with session_factory() as db:
        job, run = await lock_scheduled_job_and_run(
            db,
            job_id=None,
            run_id=run_id,
        )
        projection = scheduled_result_projection(run) if run is not None else None
        if (
            job is None
            or run is None
            or projection is None
            or projection.state is not ScheduledResultProjectionState.PENDING
        ):
            await db.rollback()
            return False
        if (
            projection.recovery_chain_id != normalized_chain_id
            and projection.recovery_attempts
            >= SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS
        ):
            await db.rollback()
            return False

        apply_scheduled_result_projection(
            run,
            projection.for_retry(recovery_chain_id=normalized_chain_id),
        )
        await db.commit()
        return True


async def _project_scheduled_agent_result_async(session_factory, run_id: str) -> dict:
    """Project one terminal Agent result without reopening business execution."""
    from sqlalchemy import select

    from packages.core.models.task import Task
    from packages.core.services.scheduler_service import lock_scheduled_job_and_run
    from packages.core.services.scheduled_run_lifecycle import (
        apply_scheduled_result_projection,
        scheduled_result_projection,
    )

    message = None
    entity_id = None
    workspace_id = None
    async with session_factory() as db:
        job, run = await lock_scheduled_job_and_run(
            db,
            job_id=None,
            run_id=run_id,
        )
        projection = scheduled_result_projection(run) if run is not None else None
        if job is None or run is None or projection is None:
            await db.rollback()
            return {"run_id": run_id, "status": "missing"}
        if projection.state is not ScheduledResultProjectionState.PENDING:
            await db.rollback()
            return {"run_id": run_id, "status": projection.state.value}

        task = (await db.execute(
            select(Task).where(Task.id == projection.task_id)
        )).scalar_one_or_none()
        if task is None:
            apply_scheduled_result_projection(
                run,
                projection.with_state(
                    ScheduledResultProjectionState.QUARANTINED,
                    error="scheduled projection task missing",
                ),
            )
            await db.commit()
            return {"run_id": run_id, "status": "quarantined"}
        if not _scheduled_projection_task_matches_lineage(
            task=task,
            job=job,
            run=run,
        ):
            apply_scheduled_result_projection(
                run,
                projection.with_state(
                    ScheduledResultProjectionState.QUARANTINED,
                    error="scheduled projection task lineage mismatch",
                ),
            )
            await db.commit()
            return {"run_id": run_id, "status": "quarantined"}

        message = await _deliver_scheduled_agent_result(
            db,
            task=task,
            job=job,
            run_id=run.id,
            result=dict(run.result or {}),
            force_workspace_chat=True,
        )
        if message is None:
            apply_scheduled_result_projection(
                run,
                projection.with_state(
                    ScheduledResultProjectionState.QUARANTINED,
                    error="scheduled projection output unavailable",
                ),
            )
            await db.commit()
            return {"run_id": run_id, "status": "quarantined"}

        apply_scheduled_result_projection(
            run,
            projection.with_state(ScheduledResultProjectionState.DELIVERED),
        )
        entity_id = task.entity_id
        workspace_id = task.workspace_id
        await db.commit()

    from packages.core.workspace_chat import service as chat_service

    await chat_service.publish_workspace_chat_message_event(
        entity_id,
        workspace_id=workspace_id,
        message=message,
    )
    return {"run_id": run_id, "status": "delivered"}


@celery_app.task(
    bind=True,
    name="scheduler.project_scheduled_agent_result",
    max_retries=SCHEDULED_RESULT_PROJECTION_MAX_RETRIES,
)
def project_scheduled_agent_result(self, run_id: str):
    """Retry a durable Agent result projection without rerunning TaskRunner."""
    from packages.core.database import create_worker_session

    try:
        session_factory = create_worker_session()
        recovery_chain_id = str(getattr(self.request, "id", "") or "").strip()
        if not _run_async(_ensure_scheduled_result_projection_recovery_chain(
            session_factory,
            run_id,
            recovery_chain_id,
        )):
            return {"run_id": run_id, "status": "recovery_not_started"}
        return _run_async(_project_scheduled_agent_result_async(
            session_factory,
            run_id,
        ))
    except Exception as exc:
        raise self.retry(
            exc=exc,
            countdown=SCHEDULED_RESULT_PROJECTION_RETRY_SECONDS,
        )


@celery_app.task(
    bind=True,
    name="scheduler.settle_scheduled_agent_run",
    max_retries=SCHEDULED_SETTLEMENT_MAX_RETRIES,
)
def settle_scheduled_agent_run(
    self,
    run_id: str,
    job_id_str: str | None,
    settlement_payload: dict,
):
    """Retry Agent scheduler settlement without rerunning TaskRunner."""

    from packages.core.database import create_worker_session
    from packages.core.services.scheduled_run_lifecycle import ScheduledRunSettlement

    settlement = ScheduledRunSettlement.from_payload(settlement_payload)
    if (
        settlement is None
        or settlement.kind is not ScheduledSettlementKind.AGENT_TASK
        or settlement.child_id is None
    ):
        raise ValueError("invalid scheduled Agent settlement payload")
    result = settlement.outcome.result or {
        "status": "failed",
        "error": settlement.outcome.error or "Agent task settlement failed",
    }
    try:
        _run_async(_update_job_run_status_async(
            create_worker_session(),
            settlement.child_id,
            result,
            scheduled_run_id=run_id,
            scheduled_job_id=job_id_str,
        ))
        return {
            "run_id": run_id,
            "task_id": settlement.child_id,
            "status": settlement.outcome.status.value,
        }
    except Exception as exc:
        raise self.retry(exc=exc, countdown=SCHEDULED_RECOVERY_RETRY_SECONDS)


def _enqueue_agent_initiative_delivery(
    run_id: str,
    kind: str | None = None,
    claim_token: str | None = None,
) -> None:
    """Publish only the projection worker; never re-enter TaskRunner."""

    args = (
        [run_id]
        if claim_token is None
        else [run_id, kind, claim_token]
    )
    deliver_agent_initiative_run.apply_async(args=args)


@celery_app.task(
    bind=True,
    name="agent_initiative.deliver",
    max_retries=5,
)
def deliver_agent_initiative_run(
    self,
    run_id: str,
    kind: str | None = None,
    claim_token: str | None = None,
):
    """Retry Initiative delivery independently from model execution."""

    from packages.core.database import create_worker_session
    from packages.core.services.agent_initiative_delivery import (
        deliver_initiative_run,
    )

    try:
        return _run_async(deliver_initiative_run(
            create_worker_session(),
            run_id=run_id,
            kind=kind,
            claim_token=claim_token,
        ))
    except Exception as exc:
        raise self.retry(
            exc=exc,
            countdown=SCHEDULED_RECOVERY_RETRY_SECONDS,
        )


@celery_app.task(name="agent_initiative.delivery_sweep")
def recover_pending_agent_initiative_deliveries():
    """Republish due delivery intents left by a worker or broker crash."""

    from packages.core.database import create_worker_session
    from packages.core.services.agent_initiative_delivery import (
        pending_initiative_delivery_run_ids,
    )

    session_factory = create_worker_session()
    run_ids = _run_async(pending_initiative_delivery_run_ids(
        session_factory
    ))
    queued = 0
    publish_failed = 0
    for run_id in run_ids:
        try:
            # Keep the broker contract run-id-only for rolling compatibility.
            # The receiving worker spends an attempt only after it has acquired
            # the durable per-step claim, so a broker outage cannot quarantine
            # a delivery that never ran.
            _enqueue_agent_initiative_delivery(run_id)
        except Exception:
            publish_failed += 1
            logger.exception(
                "Failed to republish Agent Initiative delivery run=%s",
                run_id,
            )
        else:
            queued += 1
    return {
        "found": len(run_ids),
        "queued": queued,
        "publish_failed": publish_failed,
    }


async def _deliver_scheduled_agent_result(
    db,
    *,
    task,
    job,
    run_id: str | None,
    result: dict,
    force_workspace_chat: bool = False,
):
    """Post the final response to the main Workspace Chat when requested."""
    from packages.core.services.agent_initiative_decision import (
        has_initiative_task_marker,
    )

    if (
        getattr(job, "execution_type", None) == "agent_initiative"
        or has_initiative_task_marker(getattr(task, "details", None))
    ):
        return None
    delivery_mode = (
        "workspace_chat"
        if force_workspace_chat
        else str(
            job.default_delivery_mode
            or (task.details or {}).get("default_delivery_mode")
            or ""
        ).strip()
    )
    response = str(result.get("response") or "").strip()
    if (
        delivery_mode != "workspace_chat"
        or not task.entity_id
        or not task.workspace_id
        or not response
    ):
        return

    from sqlalchemy import select

    from packages.core.models.workspace import AgentSubscription
    from packages.core.workspace_chat import service as chat_service

    subscription_id = None
    owner_subscription_id = str(task.owner_subscription_id or "").strip()
    if owner_subscription_id:
        exact_subscription = (
            await db.execute(
                select(AgentSubscription.id).where(
                    AgentSubscription.id == owner_subscription_id,
                    AgentSubscription.entity_id == task.entity_id,
                    AgentSubscription.workspace_id == task.workspace_id,
                    AgentSubscription.agent_id == job.agent_id,
                )
            )
        ).scalar_one_or_none()
        subscription_id = exact_subscription
    elif job.agent_id:
        legacy_candidates = list((await db.execute(
            select(AgentSubscription.id).where(
                AgentSubscription.entity_id == task.entity_id,
                AgentSubscription.workspace_id == task.workspace_id,
                AgentSubscription.agent_id == job.agent_id,
                AgentSubscription.status == "active",
            ).order_by(AgentSubscription.id.asc()).limit(2)
        )).scalars().all())
        if len(legacy_candidates) == 1:
            subscription_id = legacy_candidates[0]

    return await chat_service.post_message(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        body=response,
        message_kind="text",
        author_kind="agent",
        author_subscription_id=subscription_id,
        refs=[
            {"type": "task", "id": task.id},
            {"type": "scheduled_job", "id": job.id},
        ],
        meta={
            "delivery_mode": "workspace_chat",
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run_id,
            "task_id": task.id,
        },
        publish_event=False,
    )


class _ScheduledJobSkillGenerationPending(RuntimeError):
    """The producer transaction has not made its ScheduledJob visible yet."""


class _ScheduledJobSkillGenerationExhausted(RuntimeError):
    """The durable provider-attempt budget has been exhausted."""


async def _record_scheduled_job_skill_generation_failure(
    job_id: str,
    revision: int,
    exc: BaseException,
    *,
    claim=None,
) -> bool | None:
    """Persist one provider failure without overwriting a newer Job intent.

    ``True`` means the attempt budget is exhausted, ``False`` means the same
    revision remains retryable, and ``None`` means the Job was changed,
    disabled, or removed while the provider was running.
    """

    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )

    session_factory = create_worker_session()
    async with session_factory() as db:
        entity_id = (await db.execute(
            select(ScheduledJob.entity_id).where(ScheduledJob.id == job_id)
        )).scalar_one_or_none()
        if not entity_id:
            await db.rollback()
            return None
        await db.rollback()
        await lock_reusable_resource_lifecycle(db, entity_id=str(entity_id))
        job = (await db.execute(
            select(ScheduledJob)
            .where(
                ScheduledJob.id == job_id,
                ScheduledJob.entity_id == entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (
            job is None
            or not job.enabled
            or int(job.revision or 1) != int(revision)
            or int(job.skill_generation_revision or 0) != int(revision)
        ):
            await db.rollback()
            return None

        error = f"{type(exc).__name__}: {exc}"[:4000]
        attempts = int(job.skill_generation_attempts or 0)
        exhausted = attempts >= SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS
        job.skill_generation_last_error = error
        if exhausted:
            job.skill_generation_revision = None
            job.skill_generation_next_attempt_at = None
        else:
            job.skill_generation_next_attempt_at = datetime.now(timezone.utc) + timedelta(
                seconds=SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
            )
        if claim is None:
            await db.commit()
        else:
            from packages.core.services.workflow_run_execution_claim import (
                commit_fenced_execution_boundary,
            )

            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=claim.raise_if_lost,
                after_commit=(
                    claim.mark_terminal_committed if exhausted else None
                ),
                execution_claim=claim,
                session=db,
            )
        return exhausted


@celery_app.task(
    bind=True,
    max_retries=SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS - 1,
)
def generate_job_skill(
    self,
    job_id: str,
    payload_message: str,
    job_name: str = "",
    queued_revision: int | None = None,
):
    """Auto-generate a Skill for a scheduled job via LLM.

    One revision-scoped renewable claim suppresses concurrent broker/API
    deliveries before they cross the billable provider boundary. Every accepted
    generation writes a new Skill version and atomically rebinds the Job. This
    copy-on-write boundary preserves old/shared Skill content and keeps an
    uncommitted MinIO directory unreachable. The final Skill write and Job
    revision CAS remain one database transaction.
    """
    logger.info("Generating skill for job %s", job_id)
    try:
        from packages.core.database import create_worker_session

        async def _generate():
            from sqlalchemy import select
            from packages.core.models.scheduler import ScheduledJob
            from packages.core.models.skill import Skill
            from packages.core.models.workspace import Workspace
            from packages.core.models.permission import Visibility
            from packages.core.revisions import StaleRevisionError
            from packages.core.services.reusable_resource_locks import (
                lock_reusable_resource_lifecycle,
            )
            from packages.core.services.scheduler_service import (
                ScheduledJobMutationFactory,
            )
            from packages.core.services.skill_generator import (
                GeneratedSkillVersionFactory,
                generate_skill_draft,
                persist_generated_skill_draft,
            )
            from packages.core.services.workflow_run_execution_claim import (
                commit_fenced_execution_boundary,
                scheduled_job_skill_generation_claim,
            )

            session_factory = None
            claim_revision = queued_revision
            if claim_revision is None:
                # Rolling-upgrade deliveries did not carry a revision. Resolve
                # the durable revision before claiming so old and new workers
                # contend on the same single-flight key.
                session_factory = create_worker_session()
                async with session_factory() as revision_db:
                    revision_row = (await revision_db.execute(
                        select(ScheduledJob).where(ScheduledJob.id == job_id)
                    )).scalar_one_or_none()
                    if revision_row is None:
                        raise _ScheduledJobSkillGenerationPending(
                            f"scheduled job {job_id} is not visible"
                        )
                    claim_revision = int(
                        getattr(revision_row, "revision", 1) or 1
                    )

            async def _workspace_is_active(
                db,
                *,
                entity_id: str,
                workspace_id: str | None,
                for_update: bool,
            ) -> bool:
                if not workspace_id:
                    return True
                statement = select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                )
                if for_update:
                    statement = statement.with_for_update().execution_options(
                        populate_existing=True
                    )
                workspace = (await db.execute(statement)).scalar_one_or_none()
                return bool(
                    workspace is not None
                    and getattr(workspace, "deleted_at", None) is None
                    and getattr(workspace, "status", "active") == "active"
                )

            def _cleanup_uncommitted_skill(skill) -> None:
                if skill is None:
                    return
                try:
                    from packages.core.services.skill_file_storage import (
                        delete_skill_files,
                    )

                    config = dict(getattr(skill, "config", None) or {})
                    delete_skill_files(
                        str(getattr(skill, "entity_id", None) or ""),
                        str(getattr(skill, "id", None) or ""),
                        skill_dir=config.get("minio_dir") or None,
                        config=config,
                    )
                except Exception:
                    logger.warning(
                        "Failed to clean uncommitted generated Skill files %s",
                        getattr(skill, "id", None),
                        exc_info=True,
                    )

            async with scheduled_job_skill_generation_claim(
                job_id,
                int(claim_revision),
            ) as claim:
                if not claim:
                    logger.info(
                        "Suppressed duplicate skill generation for job %s "
                        "revision=%s",
                        job_id,
                        queued_revision,
                    )
                    return

                if session_factory is None:
                    session_factory = create_worker_session()
                async with session_factory() as db:
                    result = await db.execute(
                        select(ScheduledJob).where(ScheduledJob.id == job_id)
                    )
                    job = result.scalar_one_or_none()
                    if not job:
                        raise _ScheduledJobSkillGenerationPending(
                            f"scheduled job {job_id} is not visible"
                        )
                    if not job.enabled:
                        logger.info(
                            "Skipped Skill generation for disabled job %s",
                            job_id,
                        )
                        return

                    entity_id = str(job.entity_id or "").strip()
                    if not entity_id:
                        logger.warning(
                            "Job %s has no entity_id for skill generation",
                            job_id,
                        )
                        return
                    if not await _workspace_is_active(
                        db,
                        entity_id=entity_id,
                        workspace_id=job.workspace_id,
                        for_update=False,
                    ):
                        logger.info(
                            "Skipped Skill generation for inactive Workspace job %s",
                            job_id,
                        )
                        return
                    expected_revision = int(getattr(job, "revision", 1) or 1)
                    if expected_revision < int(claim_revision):
                        raise _ScheduledJobSkillGenerationPending(
                            f"scheduled job {job_id} revision {claim_revision} "
                            "is not visible"
                        )
                    if expected_revision > int(claim_revision):
                        logger.info(
                            "Skipped superseded skill generation for job %s "
                            "queued_revision=%s current_revision=%s",
                            job_id,
                            claim_revision,
                            expected_revision,
                        )
                        return
                    source_message = str(
                        job.payload_message or payload_message or ""
                    ).strip()
                    source_name = str(
                        job.name or job_name or "Scheduled Task"
                    ).strip()
                    source_job_key = str(job.job_id)
                    source_user_id = job.user_id
                    source_workspace_id = job.workspace_id
                    source_agent_id = job.agent_id
                    source_job_name = job.name
                    generated_skill_config = {
                        "source": "scheduled_job",
                        "generation_source": "llm-generated",
                        "scheduled_job_id": source_job_key,
                        "scheduled_job_pk": job_id,
                        "workspace_id": source_workspace_id,
                        "agent_id": source_agent_id,
                        "automation_name": source_job_name,
                    }
                    queued_message = str(payload_message or "").strip()
                    if queued_message and queued_message != str(
                        job.payload_message or ""
                    ).strip():
                        logger.info(
                            "Skipped obsolete skill generation for job %s "
                            "revision=%s",
                            job_id,
                            expected_revision,
                        )
                        return
                    await runtime_assert_credit_available(
                        entity_id,
                        source="scheduled_job",
                        user_id=job.user_id,
                        workspace_id=job.workspace_id,
                        byok=await _scheduled_job_skill_generation_byok(
                            job,
                            db=db,
                        ),
                    )

                    referenced_skill_id = str(
                        (job.execution_target or {}).get("skill_id") or ""
                    ).strip()
                    previous_skill = None
                    if referenced_skill_id:
                        previous_skill = (await db.execute(
                            select(Skill).where(
                                Skill.id == referenced_skill_id,
                                Skill.entity_id == entity_id,
                            )
                        )).scalar_one_or_none()
                    generated_version = GeneratedSkillVersionFactory.for_scheduled_job(
                        job_key=source_job_key,
                        previous_version=(
                            str(getattr(previous_skill, "version", "") or "")
                            if previous_skill is not None
                            and getattr(previous_skill, "version", None)
                            else None
                        ),
                    )
                    generated_skill_config.update(generated_version.config)

                    # End the read transaction before provider I/O. The
                    # renewable claim uses short independent sessions and
                    # remains authoritative while this worker owns no DB
                    # connection.
                    await db.rollback()

                    # Serialize the last admission check with pause/edit/purge,
                    # then persist the provider-attempt budget before crossing
                    # the billable boundary. A later pause may let an admitted
                    # call finish, but the final revision CAS discards it.
                    await lock_reusable_resource_lifecycle(
                        db,
                        entity_id=entity_id,
                    )
                    if not await _workspace_is_active(
                        db,
                        entity_id=entity_id,
                        workspace_id=source_workspace_id,
                        for_update=True,
                    ):
                        await db.rollback()
                        logger.info(
                            "Skipped inactive Workspace Skill generation admission "
                            "for job %s revision=%s",
                            job_id,
                            expected_revision,
                        )
                        return
                    locked_job = (await db.execute(
                        select(ScheduledJob)
                        .where(
                            ScheduledJob.id == job_id,
                            ScheduledJob.entity_id == entity_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if (
                        locked_job is None
                        or not locked_job.enabled
                        or int(locked_job.revision or 1) != expected_revision
                        or int(locked_job.skill_generation_revision or 0)
                        != expected_revision
                        or str(locked_job.payload_message or "").strip()
                        != source_message
                        or str(
                            (locked_job.execution_target or {}).get("skill_id")
                            or ""
                        ).strip()
                        != referenced_skill_id
                    ):
                        await db.rollback()
                        logger.info(
                            "Skipped stale or paused Skill generation admission "
                            "for job %s revision=%s",
                            job_id,
                            expected_revision,
                        )
                        return
                    if (
                        int(locked_job.skill_generation_attempts or 0)
                        >= SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS
                    ):
                        locked_job.skill_generation_revision = None
                        locked_job.skill_generation_next_attempt_at = None
                        locked_job.skill_generation_last_error = (
                            locked_job.skill_generation_last_error
                            or "Skill generation attempt budget exhausted"
                        )
                        await commit_fenced_execution_boundary(
                            db.commit,
                            before_commit=claim.raise_if_lost,
                            after_commit=claim.mark_terminal_committed,
                            execution_claim=claim,
                            session=db,
                        )
                        return
                    locked_job.skill_generation_attempts = (
                        int(locked_job.skill_generation_attempts or 0) + 1
                    )
                    locked_job.skill_generation_last_error = None
                    locked_job.skill_generation_next_attempt_at = (
                        datetime.now(timezone.utc)
                        + timedelta(
                            seconds=SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
                        )
                    )
                    await commit_fenced_execution_boundary(
                        db.commit,
                        before_commit=claim.raise_if_lost,
                        execution_claim=claim,
                        session=db,
                    )

                    prompt = (
                        f"Scheduled automation: {source_name}\n\n"
                        f"{source_message}"
                    )
                    skill = None
                    try:
                        generated_draft = await generate_skill_draft(
                            prompt,
                            entity_id,
                            user_id=source_user_id,
                            category="automation",
                            tags=[
                                "auto-generated",
                                "scheduled-job",
                                source_job_key,
                            ],
                        )

                        # Re-enter through the entity lifecycle -> Workspace ->
                        # Job -> Skill lock order after provider I/O.
                        await lock_reusable_resource_lifecycle(
                            db,
                            entity_id=entity_id,
                        )
                        if not await _workspace_is_active(
                            db,
                            entity_id=entity_id,
                            workspace_id=source_workspace_id,
                            for_update=True,
                        ):
                            await db.rollback()
                            logger.info(
                                "Discarded generated Skill output for inactive "
                                "Workspace job %s revision=%s",
                                job_id,
                                expected_revision,
                            )
                            return
                        locked_job = (await db.execute(
                            select(ScheduledJob)
                            .where(
                                ScheduledJob.id == job_id,
                                ScheduledJob.entity_id == entity_id,
                            )
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )).scalar_one_or_none()
                        if (
                            locked_job is None
                            or not locked_job.enabled
                            or int(locked_job.revision or 1) != expected_revision
                            or int(locked_job.skill_generation_revision or 0)
                            != expected_revision
                            or str(locked_job.payload_message or "").strip()
                            != source_message
                            or str(
                                (locked_job.execution_target or {}).get("skill_id")
                                or ""
                            ).strip()
                            != referenced_skill_id
                        ):
                            await db.rollback()
                            logger.info(
                                "Discarded stale Skill provider output for job %s "
                                "revision=%s",
                                job_id,
                                expected_revision,
                            )
                            return

                        skill = await persist_generated_skill_draft(
                            generated_draft,
                            prompt=prompt,
                            entity_id=entity_id,
                            db=db,
                            category="automation",
                            tags=[
                                "auto-generated",
                                "scheduled-job",
                                source_job_key,
                            ],
                            config_overrides=generated_skill_config,
                            owner_user_id=job.user_id,
                            workspace_id=source_workspace_id,
                            visibility=(
                                Visibility.WORKSPACE
                                if source_workspace_id
                                else Visibility.ENTITY
                            ),
                            version=generated_version.version,
                        )
                        complexity = (skill.config or {}).get(
                            "complexity",
                            "primary",
                        )
                        target = dict(locked_job.execution_target or {})
                        target["skill_id"] = skill.id
                        target["complexity"] = complexity
                        updates = {
                            "execution_target": target,
                            "execution_type": "skill",
                            # The Skill bundle is the versioned executable
                            # source of truth. A copied prompt here would be a
                            # second, stale content snapshot.
                            "execution_script": None,
                        }

                        try:
                            # The lifecycle fence is already owned before the
                            # Job row. ``apply`` revalidates the new Skill
                            # reference and owns topology/revision/audit.
                            mutation = await ScheduledJobMutationFactory.apply(
                                db,
                                locked_job,
                                updates,
                                expected_revision=expected_revision,
                                causation_id=skill.id,
                            )
                        except StaleRevisionError:
                            # The Skill and stale Job link share a transaction.
                            # Discard both instead of overwriting a newer edit.
                            await db.rollback()
                            _cleanup_uncommitted_skill(skill)
                            logger.info(
                                "Discarded stale generated skill for job %s "
                                "revision=%s",
                                job_id,
                                expected_revision,
                            )
                            return
                        if mutation is None:
                            await db.rollback()
                            _cleanup_uncommitted_skill(skill)
                            logger.info(
                                "Discarded generated skill for removed job %s",
                                job_id,
                            )
                            return

                        # Close the durable request only with the generated
                        # Skill and revisioned Job mutation in this commit.
                        locked_job.skill_generation_revision = None
                        locked_job.skill_generation_next_attempt_at = None
                        locked_job.skill_generation_attempts = 0
                        locked_job.skill_generation_last_error = None

                        await commit_fenced_execution_boundary(
                            db.commit,
                            before_commit=claim.raise_if_lost,
                            after_commit=claim.mark_terminal_committed,
                            execution_claim=claim,
                            session=db,
                        )
                        logger.info(
                            "Generated skill %s (%s) for job %s revision=%s",
                            skill.id,
                            skill.name,
                            job_id,
                            expected_revision,
                        )
                    except Exception as exc:
                        await db.rollback()
                        _cleanup_uncommitted_skill(skill)
                        exhausted = (
                            await _record_scheduled_job_skill_generation_failure(
                                job_id,
                                expected_revision,
                                exc,
                                claim=claim,
                            )
                        )
                        if exhausted:
                            raise _ScheduledJobSkillGenerationExhausted(
                                f"Skill generation exhausted for job {job_id}"
                            ) from exc
                        raise

        _run_async(_generate())
    except CreditExhaustedError:
        logger.warning("Skill generation for job %s skipped: credits exhausted", job_id)
    except _ScheduledJobSkillGenerationExhausted as exc:
        logger.error("%s", exc)
    except Exception as exc:
        logger.error("Skill generation failed for job %s: %s", job_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(
    name='run_workflow',
    bind=True,
    max_retries=2,
    soft_time_limit=10800,
    time_limit=11100,
)
def run_workflow(
    self,
    workflow_run_id: str,
    scheduled_run_id: str | None = None,
    scheduled_job_id: str | None = None,
    claim_recheck: bool = False,
):
    """Execute a workflow run.

    Called when a workflow is started. Delegates to WorkflowRunner which
    processes steps sequentially (or in parallel where specified), handles
    condition branching, and pauses on wait steps.  Each invocation runs
    the workflow to completion or pause.
    """
    from packages.core.services.workflow_claim_recovery import (
        WORKFLOW_CLAIM_HANDOFF_HEADER,
    )

    request_headers = getattr(self.request, "headers", None)
    if (
        isinstance(request_headers, dict)
        and request_headers.get(SCHEDULED_EXECUTION_RECOVERY_HEADER) is True
    ):
        claim_recheck = True
    claim_handoff_token = (
        str(request_headers.get(WORKFLOW_CLAIM_HANDOFF_HEADER) or "").strip() or None
        if isinstance(request_headers, dict)
        else None
    )

    logger.info("Running workflow %s (attempt %d)", workflow_run_id, self.request.retries + 1)
    try:
        from sqlalchemy import select

        from packages.core.ai.workflow_runner import WorkflowRunner
        from packages.core.database import create_worker_session
        from packages.core.models.workflow import WorkflowRun

        async def _execute():
            session_factory = create_worker_session()
            admission = None
            if scheduled_run_id:
                admission = await _admit_scheduled_child(
                    scheduled_run_id,
                    scheduled_job_id,
                    child_kind=ScheduledDispatchKind.WORKFLOW,
                    child_id=workflow_run_id,
                )
            if admission is not None and not admission.admitted:
                child_terminalized = False
                if _suppressed_child_needs_terminalization(admission):
                    child_terminalized = (
                        await _terminalize_suppressed_scheduled_child(
                            session_factory,
                            child_kind=ScheduledDispatchKind.WORKFLOW,
                            child_id=workflow_run_id,
                            scheduled_run_id=scheduled_run_id,
                            scheduled_job_id=scheduled_job_id,
                        )
                    )
                logger.info(
                    "Scheduled workflow child suppressed run=%s job=%s "
                    "workflow_run=%s reason=%s child_terminalized=%s",
                    scheduled_run_id,
                    scheduled_job_id,
                    workflow_run_id,
                    admission.reason,
                    child_terminalized,
                )
                return {
                    "workflow_run_id": workflow_run_id,
                    "status": (
                        ScheduledRunStatus.CANCELLED.value
                        if child_terminalized
                        or admission.status
                        is ScheduledChildAdmissionStatus.CANCELLED
                        else "already_settled"
                    ),
                    "duplicate_suppressed": True,
                    "scheduled_occurrence_closed": True,
                    "reason": admission.reason,
                    "child_terminalized": child_terminalized,
                }
            execution_outcome = await WorkflowRunner(
                session_factory=session_factory,
            ).run(workflow_run_id, claim_handoff_token=claim_handoff_token)
            async with session_factory() as db:
                run = (await db.execute(
                    select(WorkflowRun).where(WorkflowRun.id == workflow_run_id)
                )).scalar_one_or_none()
                if not run:
                    raise RuntimeError("workflow run disappeared during execution")
                return {
                    "workflow_run_id": run.id,
                    "status": run.status,
                    "execution_outcome": execution_outcome,
                    **({"error": run.error} if run.error else {}),
                }

        result = _run_async(_execute())
        if result.get("execution_outcome") == "claim_held_by_live_execution":
            if claim_recheck:
                return {
                    "workflow_run_id": workflow_run_id,
                    "status": "in_progress",
                    "duplicate_suppressed": True,
                    "recheck_scheduled": False,
                }
            raise _WorkflowExecutionClaimHeld(
                f"workflow run {workflow_run_id} is already executing"
            )
        if result.get("status") == "failed":
            _finalize_scheduled_run_best_effort(
                run_id=scheduled_run_id,
                job_id_str=scheduled_job_id,
                error=result.get("error") or "workflow failed",
                celery_task=self,
            )
        elif result.get("status") in {"completed", "cancelled"}:
            _finalize_scheduled_run_best_effort(
                run_id=scheduled_run_id,
                job_id_str=scheduled_job_id,
                result=result,
                celery_task=self,
            )
        return result
    except (Ignore, Retry):
        raise
    except _WorkflowExecutionClaimHeld as exc:
        from packages.core.services.workflow_run_execution_claim import (
            WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS,
        )

        logger.info("Workflow %s duplicate delivery deferred", workflow_run_id)
        raise self.retry(
            exc=exc,
            countdown=WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS + 5,
        )
    except CreditExhaustedError as exc:
        logger.warning("Workflow %s aborted: credits exhausted", workflow_run_id)
        _finalize_scheduled_run_best_effort(
            run_id=scheduled_run_id,
            job_id_str=scheduled_job_id,
            error=f"credits_exhausted: {exc}",
            celery_task=self,
        )
    except Exception as exc:
        logger.error("Workflow %s failed: %s", workflow_run_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _finalize_scheduled_run_best_effort(
                run_id=scheduled_run_id,
                job_id_str=scheduled_job_id,
                error=str(exc),
                celery_task=self,
            )
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


async def _claim_due_workflow_continuations(*, limit: int = 100) -> list[tuple[str, str]]:
    """Lease a bounded batch of durable Workflow continuations for publish."""

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import exists, select

    from packages.core.database import create_worker_session
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.models.workflow import WorkflowRun

    now = datetime.now(timezone.utc)
    # A run with a live execution owner is already continuing; republishing
    # it only adds a heavy-queue delivery that ends as claim_held.
    has_live_owner = exists().where(
        RuntimeExecutionClaim.claim_key == "workflow-run:" + WorkflowRun.id,
        RuntimeExecutionClaim.expires_at > now,
    )
    async with create_worker_session()() as db:
        runs = list((await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.continuation_token.is_not(None),
                WorkflowRun.continuation_due_at <= now,
                WorkflowRun.continuation_next_attempt_at <= now,
                ~has_live_owner,
            )
            .order_by(
                WorkflowRun.continuation_next_attempt_at.asc(),
                WorkflowRun.id.asc(),
            )
            .limit(max(1, int(limit)))
            .with_for_update(skip_locked=True)
        )).scalars().all())
        claimed: list[tuple[str, str]] = []
        for run in runs:
            token = str(run.continuation_token or "").strip()
            if not token:
                continue
            run.continuation_next_attempt_at = now + timedelta(
                seconds=workflow_continuation_retry_seconds(run.continuation_due_at, now)
            )
            claimed.append((run.id, token))
        await db.commit()
        return claimed


async def _claim_due_workflow_terminal_effects(
    *,
    limit: int = 100,
) -> list[str]:
    """Lease terminal runs whose idempotent post-commit effects are unfinished."""

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_, select

    from packages.core.constants.workflow import WORKFLOW_RUN_TERMINAL_STATUSES
    from packages.core.database import create_worker_session
    from packages.core.models.workflow import WorkflowRun

    now = datetime.now(timezone.utc)
    async with create_worker_session()() as db:
        runs = list((await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(WORKFLOW_RUN_TERMINAL_STATUSES),
                WorkflowRun.terminal_effects_completed_at.is_(None),
                or_(
                    WorkflowRun.terminal_effects_next_attempt_at.is_(None),
                    WorkflowRun.terminal_effects_next_attempt_at <= now,
                ),
            )
            .order_by(
                WorkflowRun.terminal_effects_next_attempt_at.asc(),
                WorkflowRun.id.asc(),
            )
            .limit(max(1, int(limit)))
            .with_for_update(skip_locked=True)
        )).scalars().all())
        for run in runs:
            run.terminal_effects_next_attempt_at = now + timedelta(
                seconds=WORKFLOW_TERMINAL_EFFECT_RETRY_SECONDS
            )
        await db.commit()
        return [run.id for run in runs]


async def _pending_workflow_terminal_effect_run_ids(
    *,
    limit: int = 100,
) -> list[str]:
    """Return legacy failed-run receipts that explicitly remain unqueued."""

    from sqlalchemy import or_, select

    from packages.core.constants.workflow import (
        WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD,
    )
    from packages.core.database import create_worker_session
    from packages.core.models.workflow import WorkflowRun

    async with create_worker_session()() as db:
        rows = list((await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.status == "failed",
                or_(
                    WorkflowRun.trigger_source.is_(None),
                    WorkflowRun.trigger_source != "error",
                ),
            )
            .order_by(WorkflowRun.id.asc())
            .limit(max(1, int(limit)) * 4)
        )).scalars().all())
    pending: list[str] = []
    for run in rows:
        trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
        effects = trigger_data.get(WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD)
        if (
            isinstance(effects, dict)
            and effects.get("error_handlers_enqueued") is False
        ):
            pending.append(run.id)
            if len(pending) >= max(1, int(limit)):
                break
    return pending


@celery_app.task(name="workflow.resume_sweep")
def workflow_resume_sweep():
    """Publish due Workflow continuations from database-backed intent."""

    claimed = _run_async(_claim_due_workflow_continuations())
    published = 0
    for workflow_run_id, continuation_token in claimed:
        try:
            resume_workflow.apply_async(
                args=[workflow_run_id, continuation_token],
            )
            published += 1
        except Exception:
            logger.exception(
                "Failed to publish Workflow continuation run=%s",
                workflow_run_id,
            )
    return {"claimed": len(claimed), "published": published}


@celery_app.task(name="workflow.terminal_effect_sweep")
def workflow_terminal_effect_sweep():
    """Redeliver terminal runs until every durable effect is drained."""

    from packages.core.ai.workflow_runner import WorkflowRunner

    run_ids = _run_async(_claim_due_workflow_terminal_effects())
    published = sum(bool(WorkflowRunner.enqueue(run_id)) for run_id in run_ids)
    return {"claimed": len(run_ids), "published": published}


async def _recover_expired_workflow_claims(*, limit: int = 25) -> dict[str, int]:
    """Resume or settle runs whose execution owner disappeared.

    A run resumes from its durable step boundary when its interrupted nodes
    are replay-safe and fails with an explicit reason when they are not; the
    decision lives in ``WorkflowRunner._recover_lost_execution_claim``.
    """
    from sqlalchemy import func, select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import create_worker_session
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.models.workflow import WorkflowRun

    factory = create_worker_session()
    async with factory() as db:
        candidates = list((await db.execute(
            select(WorkflowRun.id, RuntimeExecutionClaim.claim_token)
            .join(
                RuntimeExecutionClaim,
                RuntimeExecutionClaim.claim_key == "workflow-run:" + WorkflowRun.id,
            )
            .where(
                # A pending run with an expired lease lost its owner before
                # it recorded progress; it resumes like a running one.
                WorkflowRun.status.in_(("pending", "running")),
                RuntimeExecutionClaim.expires_at <= func.now(),
            )
            .order_by(RuntimeExecutionClaim.expires_at, WorkflowRun.id)
            .limit(max(1, min(int(limit), 100)))
        )).all())
    settled = 0
    resumed = 0
    for run_id, expired_token in candidates:
        try:
            status = await WorkflowRunner._recover_lost_execution_claim(
                run_id,
                session_factory=factory,
                expired_claim_token=expired_token,
            )
            settled += int(status in {"failed", "cancelled"})
            resumed += int(status in {"pending", "running"})
        except Exception:
            logger.exception("Expired Workflow claim recovery failed run=%s", run_id)
    return {"checked": len(candidates), "settled": settled, "resumed": resumed}


@celery_app.task(name="workflow.recover_expired_claims")
def recover_expired_workflow_claims():
    return _run_async(_recover_expired_workflow_claims())


async def _redispatch_unclaimed_workflow_runs(*, limit: int = 25) -> dict[str, int]:
    """Republish open runs whose dispatch no worker ever claimed.

    Starting or retrying a run publishes one broker message. When that message
    is lost (a broker or worker restart between publish and claim), the run
    stays pending/running with no execution claim and nothing recovers it:
    ``_recover_expired_workflow_claims`` only sees runs a worker once claimed.
    A duplicate delivery is harmless, because the execution claim fences it
    and a settled run is skipped, so republishing after a quiet period is safe.
    Each republish waits twice as long as the previous one, so a run that is
    merely queued behind long work gains a few cheap duplicates, not a stream.
    Runs quiet for longer than the window predate execution claims or were
    abandoned long ago; reviving them could replay stale work.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import exists, func, or_, select
    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.constants.workflow import (
        WORKFLOW_DISPATCH_GRACE_SECONDS,
        WORKFLOW_DISPATCH_MAX_BACKOFF_SECONDS,
        WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD,
        WORKFLOW_DISPATCH_RECOVERY_WINDOW_SECONDS,
    )
    from packages.core.database import create_worker_session
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.models.workflow import WorkflowRun

    now = datetime.now(timezone.utc)
    last_activity = func.coalesce(
        WorkflowRun.updated_at, WorkflowRun.started_at, WorkflowRun.created_at,
    )
    has_claim = exists().where(
        RuntimeExecutionClaim.claim_key == "workflow-run:" + WorkflowRun.id,
    )
    published = 0
    deferred = 0
    async with create_worker_session()() as db:
        runs = list((await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(("pending", "running")),
                # Scheduled occurrences have their own dispatch recovery.
                or_(WorkflowRun.trigger_source.is_(None), WorkflowRun.trigger_source != "schedule"),
                WorkflowRun.continuation_token.is_(None),
                ~has_claim,
                last_activity <= now - timedelta(seconds=WORKFLOW_DISPATCH_GRACE_SECONDS),
                last_activity > now - timedelta(seconds=WORKFLOW_DISPATCH_RECOVERY_WINDOW_SECONDS),
            )
            .order_by(last_activity, WorkflowRun.id)
            .limit(max(1, min(int(limit), 100)))
            .with_for_update(skip_locked=True, of=WorkflowRun)
        )).scalars().all())
        due: list[str] = []
        for run in runs:
            trigger_data = dict(run.trigger_data or {})
            previous = trigger_data.get(WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD)
            attempts = int(previous.get("attempts") or 0) if isinstance(previous, dict) else 0
            quiet_for = min(
                WORKFLOW_DISPATCH_GRACE_SECONDS * (2 ** attempts),
                WORKFLOW_DISPATCH_MAX_BACKOFF_SECONDS,
            )
            active_at = run.updated_at or run.started_at or run.created_at
            if active_at is not None and active_at > now - timedelta(seconds=quiet_for):
                deferred += 1
                continue
            trigger_data[WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD] = {
                "attempts": attempts + 1,
                "last_dispatched_at": now.isoformat(),
            }
            run.trigger_data = trigger_data
            flag_modified(run, "trigger_data")
            due.append(run.id)
        await db.commit()
    for run_id in due:
        if WorkflowRunner.enqueue(run_id):
            published += 1
            logger.warning("Republished unclaimed Workflow run=%s", run_id)
    return {"checked": len(runs), "published": published, "deferred": deferred}


@celery_app.task(name="workflow.redispatch_unclaimed_runs")
def redispatch_unclaimed_workflow_runs():
    return _run_async(_redispatch_unclaimed_workflow_runs())


@celery_app.task(
    name='resume_workflow',
    bind=True,
    max_retries=2,
    soft_time_limit=10800,
    time_limit=11100,
)
def resume_workflow(
    self,
    workflow_run_id: str,
    continuation_token: str | None = None,
):
    """Resume a paused workflow timer and continue it to completion."""
    logger.info(
        "Resuming workflow %s (attempt %d)",
        workflow_run_id,
        self.request.retries + 1,
    )
    try:
        from packages.core.ai.workflow_runner import WorkflowRunner
        from packages.core.database import create_worker_session

        outcome = _run_async(WorkflowRunner.resume(
            workflow_run_id,
            execute=True,
            continuation_token=continuation_token,
            session_factory=create_worker_session(),
        ))
        return {"workflow_run_id": workflow_run_id, "status": outcome}
    except Exception as exc:
        logger.error("Workflow resume %s failed: %s", workflow_run_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=3)
def fetch_and_index_url_document(self, document_id: str, url: str):
    """Fetch content from a URL, save to filesystem, then index.

    Called when a user imports a document via URL. The API returns a
    placeholder document immediately; this task does the actual work.
    """
    logger.info(
        "Fetching URL document %s from %s (attempt %d)",
        document_id,
        url,
        self.request.retries + 1,
    )
    try:
        from packages.core.database import create_worker_session

        async def _fetch_and_index():
            import asyncio
            import os
            import tempfile
            from pathlib import Path

            from sqlalchemy import select

            from packages.core.config import get_settings
            from packages.core.models.document import Document, VectorStatus
            from packages.core.services.embedding_service import index_document
            from packages.core.services.entity_fs import (
                entity_filesystem_mutation_lock,
                finish_entity_filesystem_mutation,
                resolve_path,
                write_entity_file_atomic,
            )
            from packages.core.services.file_type_detection import (
                detect_file_type,
                mime_for_extension,
            )
            from packages.core.services.tool_cache_version import (
                bump_tool_cache_version,
            )
            from packages.core.services.text_extraction import extract_text
            from packages.core.services.web_fetch import fetch_url

            settings = get_settings()
            session_factory = create_worker_session()

            def has_inline_content(document: Document) -> bool:
                metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
                return any(
                    isinstance(metadata.get(key), str) and bool(metadata[key].strip())
                    for key in ("content", "content_text")
                )

            def file_type_from_response_mime(mime_type: str) -> str | None:
                known_types = {
                    "application/json": "json",
                    "application/ld+json": "json",
                    "application/pdf": "pdf",
                    "application/vnd.ms-excel": "xls",
                    "application/vnd.ms-powerpoint": "ppt",
                    "application/msword": "doc",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                    "application/xml": "xml",
                    "application/yaml": "yaml",
                    "text/csv": "csv",
                    "text/html": "html",
                    "text/markdown": "md",
                    "text/xml": "xml",
                    "text/yaml": "yaml",
                }
                if mime_type in known_types:
                    return known_types[mime_type]
                if mime_type.endswith("+json"):
                    return "json"
                if mime_type.endswith("+xml"):
                    return "xml"
                return None

            async with session_factory() as db:
                doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
                if not doc or doc.is_trashed:
                    return None

                entity_id = doc.entity_id
                inline_content_exists = not settings.MANOR_FS_ENABLED and has_inline_content(doc)
                if inline_content_exists and doc.vector_status == VectorStatus.READY:
                    await db.commit()
                    return True
                durable_file = (
                    resolve_path(entity_id, doc.fs_path) if settings.MANOR_FS_ENABLED and doc.fs_path else None
                )
                needs_fetch = not inline_content_exists and (not durable_file or not os.path.isfile(durable_file))
                # Do not retain a row lock while the remote server responds.
                await db.commit()

                if needs_fetch:
                    max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
                    fetched = await fetch_url(url, max_bytes=max_bytes)
                    content_type = (fetched.content_type or "").split(";", 1)[0].strip().lower()
                    fetched_content_text: str | None = None
                    fd, extraction_path = tempfile.mkstemp(
                        suffix=Path(doc.name).suffix,
                    )
                    os.close(fd)
                    try:
                        await asyncio.to_thread(
                            Path(extraction_path).write_bytes,
                            fetched.content,
                        )
                        detected_type = await asyncio.to_thread(
                            detect_file_type,
                            extraction_path,
                            declared_name=doc.name,
                        )
                        response_file_type = file_type_from_response_mime(content_type)
                        detected_from_bytes = detected_type.sniffed_extension
                        if detected_from_bytes and detected_from_bytes != "txt":
                            fetched_file_type = detected_type.extension or detected_from_bytes
                            fetched_mime_type = mime_for_extension(fetched_file_type)
                        elif detected_type.mismatch:
                            fetched_file_type = detected_type.extension
                            fetched_mime_type = detected_type.mime_type
                        else:
                            fetched_file_type = (
                                response_file_type or detected_type.extension or doc.file_type
                            )
                            fetched_mime_type = (
                                content_type
                                if response_file_type
                                else detected_type.mime_type or mime_for_extension(fetched_file_type)
                            )
                        if not settings.MANOR_FS_ENABLED:
                            fetched_content_text = await extract_text(
                                extraction_path,
                                mime_type=fetched_mime_type,
                                file_type=fetched_file_type,
                            )
                    finally:
                        await asyncio.to_thread(
                            Path(extraction_path).unlink,
                            missing_ok=True,
                        )
                    if not settings.MANOR_FS_ENABLED and not fetched_content_text:
                        raise RuntimeError("URL content could not be extracted without filesystem storage")

                    async def persist_fetched_document() -> bool:
                        current = (
                            await db.execute(
                                select(Document)
                                .where(
                                    Document.id == document_id,
                                    Document.entity_id == entity_id,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        if not current or current.is_trashed:
                            await db.rollback()
                            return False

                        if not settings.MANOR_FS_ENABLED and has_inline_content(current):
                            # An editor or another delivery persisted the durable
                            # inline result while this remote fetch was in flight.
                            await db.rollback()
                            return True

                        current_file = resolve_path(entity_id, current.fs_path) if current.fs_path else None
                        if current_file and os.path.isfile(current_file):
                            # A user edit or an earlier retry won while the
                            # remote request was in flight. Never overwrite it.
                            await db.rollback()
                            return True

                        current.mime_type = fetched_mime_type or current.mime_type
                        current.file_size = len(fetched.content)
                        if fetched_file_type:
                            expected_suffix = f".{fetched_file_type}"
                            if not current.name.lower().endswith(expected_suffix):
                                current.name = os.path.splitext(current.name)[0] + expected_suffix
                            current.file_type = fetched_file_type

                        if not settings.MANOR_FS_ENABLED:
                            current.metadata_ = {
                                **dict(current.metadata_ or {}),
                                "content_text": fetched_content_text,
                            }
                            await db.commit()
                            await bump_tool_cache_version(entity_id, "documents")
                            return True

                        entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
                        await asyncio.to_thread(os.makedirs, entity_root, exist_ok=True)
                        rel_path = current.fs_path or current.name
                        target_path = resolve_path(entity_id, rel_path)
                        if not target_path:
                            raise RuntimeError("URL document path escaped the entity root")
                        if not current.fs_path and os.path.exists(target_path):
                            base, ext = os.path.splitext(current.name)
                            rel_path = f"{base}_{document_id}{ext}"
                            target_path = resolve_path(entity_id, rel_path)
                            if not target_path:
                                raise RuntimeError("URL document path escaped the entity root")

                        previous_content = (
                            await asyncio.to_thread(Path(target_path).read_bytes)
                            if os.path.isfile(target_path)
                            else None
                        )
                        wrote_file = False
                        committed = False
                        try:
                            target = await asyncio.to_thread(
                                write_entity_file_atomic,
                                entity_id,
                                rel_path,
                                fetched.content,
                                expected_size=len(fetched.content),
                                allow_empty=False,
                            )
                            wrote_file = True
                            current.fs_path = os.path.relpath(target, entity_root)
                            await db.flush()
                            await db.commit()
                            committed = True
                            await bump_tool_cache_version(entity_id, "documents")
                            return True
                        except BaseException:
                            if not committed:
                                await db.rollback()
                                if wrote_file:
                                    if previous_content is None:
                                        try:
                                            await asyncio.to_thread(os.remove, target_path)
                                        except FileNotFoundError:
                                            pass
                                    else:
                                        await asyncio.to_thread(
                                            write_entity_file_atomic,
                                            entity_id,
                                            rel_path,
                                            previous_content,
                                            expected_size=len(previous_content),
                                            allow_empty=True,
                                        )
                            raise

                    if settings.MANOR_FS_ENABLED:
                        entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
                        async with entity_filesystem_mutation_lock(entity_root):
                            should_index = await finish_entity_filesystem_mutation(
                                persist_fetched_document(),
                            )
                    else:
                        should_index = await persist_fetched_document()
                    if not should_index:
                        return None

                async with session_factory() as db2:
                    success = await index_document(
                        db2,
                        document_id,
                        allow_ready=False,
                    )
                    await db2.commit()
                    await bump_tool_cache_version(entity_id, "documents")
                    return success

        success = _run_async(_fetch_and_index())
        if success is None:
            logger.info("Skipping deleted or trashed URL document %s", document_id)
            return {"document_id": document_id, "status": "skipped"}
        if not success:
            raise RuntimeError(f"Indexing returned False for document {document_id}")
        logger.info("Successfully fetched and indexed URL document %s", document_id)
        return {"document_id": document_id, "status": "ready"}
    except Exception as exc:
        logger.error("URL fetch failed for %s: %s", document_id, exc, exc_info=True)
        error_message = str(exc)[:500]
        # Mark as failed
        try:
            from packages.core.database import create_worker_session
            from packages.core.models.document import VectorStatus
            from packages.core.services.tool_cache_version import (
                bump_tool_cache_version,
            )

            async def _mark_failed():
                from sqlalchemy import select
                from packages.core.models.document import Document

                session_factory = create_worker_session()
                async with session_factory() as db:
                    result = await db.execute(select(Document).where(Document.id == document_id).with_for_update())
                    doc = result.scalar_one_or_none()
                    if (
                        doc
                        and not doc.is_trashed
                        and doc.vector_status
                        in {
                            VectorStatus.PENDING,
                            VectorStatus.FAILED,
                        }
                    ):
                        entity_id = doc.entity_id
                        doc.vector_status = VectorStatus.FAILED
                        meta = dict(doc.metadata_ or {})
                        meta["fetch_error"] = error_message
                        doc.metadata_ = meta
                        await db.commit()
                        await bump_tool_cache_version(entity_id, "documents")

            _run_async(_mark_failed())
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=30 * (2**self.request.retries))


@celery_app.task(
    bind=True,
    base=_PausedDocumentEmbeddingTask,
    max_retries=0,
    soft_time_limit=6_900,
    time_limit=7_200,
)
def process_document_embeddings(self, document_id: str):
    """Acknowledge pre-deploy embedding deliveries without doing work."""
    logger.info(
        "Document embedding is temporarily disabled; acknowledged queued document %s",
        document_id,
    )
    return {"document_id": document_id, "status": "disabled"}


@celery_app.task(bind=True, max_retries=1)
def send_agent_greetings(self, entity_id: str, workspace_id: str,
                         workspace_name: str, workspace_kind: str,
                         agent_data: list[dict]):
    """Post greeting messages from each subscribed agent to workspace chat.

    Dispatched by dispatch_workspace_post_commit() after the workspace
    materialization transaction commits. Each agent introduces itself with a
    short LLM-generated greeting.

    agent_data: [{subscription_id, agent_name, service_key, system_prompt}, ...]
    """
    async def _greet():
        from packages.core.ai.runtime import (
            runtime_execute_agent_greeting_completion,
        )
        from packages.core.workspace_chat.notifiers import notify_agent_greeting

        for i, agent in enumerate(agent_data):
            sub_id = agent["subscription_id"]
            name = agent.get("agent_name", "Agent")
            service = agent.get("service_key", "general")

            # Generate personalized greeting via LLM (cheap worker model)
            try:
                completion = await runtime_execute_agent_greeting_completion(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    agent_name=name,
                    service_key=service,
                    workspace_name=workspace_name,
                    workspace_kind=workspace_kind,
                    system_prompt=agent.get("system_prompt") or "",
                )
                greeting = completion.content
                greeting = greeting.strip().strip('"').strip("'")
            except Exception:
                greeting = ""

            # Fallback if LLM returned empty or failed
            if not greeting:
                service_label = service.replace("_", " ")
                greeting = (
                    f"Hi! I'm {name}. I'm here to help with {service_label} "
                    f"for this workspace. Let me know how I can assist!"
                )

            await notify_agent_greeting(
                entity_id=entity_id,
                workspace_id=workspace_id,
                subscription_id=sub_id,
                greeting=greeting,
                sequence=i,
                total=len(agent_data),
            )

    try:
        _run_async(_greet())
        logger.info("Agent greetings sent for workspace %s (%d agents)",
                     workspace_id, len(agent_data))
    except CreditExhaustedError:
        logger.warning("Agent greetings for %s skipped: credits exhausted", workspace_id)
    except Exception as exc:
        logger.error("send_agent_greetings failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(bind=True, max_retries=2)
def generate_knowledge_content(
    self, entity_id: str, workspace_id: str,
    group_id: str, group_name: str, purpose: str,
    workspace_name: str, workspace_kind: str, primary_work: str,
    starter_task_key: str | None = None,
    source_excerpt: str | None = None,
):
    """Generate starter document for an approved knowledge base."""
    logger.info("Generating knowledge content: group=%s name=%s", group_id, group_name)

    async def _generate():
        from packages.core.ai.runtime import (
            runtime_execute_knowledge_starter_document_completion,
        )
        from packages.core.database import create_worker_session
        from packages.core.models.document import DocumentGroup, DocumentGroupMember
        from packages.core.services.knowledge_starter import with_starter_document_settings
        from sqlalchemy import select

        completion = await runtime_execute_knowledge_starter_document_completion(
            entity_id=entity_id,
            workspace_id=workspace_id,
            group_name=group_name,
            purpose=purpose,
            workspace_name=workspace_name,
            workspace_kind=workspace_kind,
            primary_work=primary_work,
            source_excerpt=source_excerpt,
        )
        content = completion.content

        if not content or not content.strip():
            return

        async with create_worker_session()() as db:
            from packages.core.services.document_metadata import merge_document_metadata
            from packages.core.services.knowledge_starter import write_markdown_document

            doc = await write_markdown_document(
                db,
                entity_id=entity_id,
                name=f"{group_name}.md",
                content=content,
                source="ai_generated",
                metadata=merge_document_metadata(
                    origin={"workspace_id": workspace_id, "tool_name": "workspace_starter_doc"},
                    artifact={"role": "final"},
                    extra={"auto_generated": True, "group_id": group_id},
                ),
            )
            doc_id = doc.id
            db.add(DocumentGroupMember(document_id=doc_id, group_id=group_id))
            await db.flush()

            group = (await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.id == group_id,
                    DocumentGroup.entity_id == entity_id,
                )
            )).scalar_one_or_none()
            if group is not None:
                settings = with_starter_document_settings(
                    group.settings,
                    group_name=group.name or group_name,
                    status="ready",
                    document_id=doc_id,
                )
                if starter_task_key:
                    settings["starter_document"]["task_key"] = starter_task_key
                # The excerpt only exists to ground this generation; drop it now.
                settings["starter_document"].pop("source_excerpt", None)
                group.settings = settings

            await db.commit()
            logger.info("Generated starter doc %s for group %s", doc_id, group_id)

            try:
                process_document_embeddings.delay(doc_id)
            except Exception:
                pass

    try:
        _run_async(_generate())
    except CreditExhaustedError:
        logger.warning("Knowledge gen for group %s skipped: credits exhausted", group_id)
    except Exception as exc:
        logger.error("Knowledge gen failed for group %s: %s", group_id, exc)
        raise self.retry(exc=exc, countdown=30)
