"""AI execution Celery tasks — dispatched by API, executed by workers."""
from __future__ import annotations

import logging

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async
from packages.core.ai.llm_client import CreditExhaustedError
from packages.core.plans.service import PlanContractError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — mark plan/task as failed when Celery retries are exhausted
# ---------------------------------------------------------------------------

def _mark_plan_failed(plan_id: str, error_msg: str) -> None:
    """Mark an ExecutionPlan and its parent Task as failed after crashes."""
    try:
        async def _do():
            from packages.core.database import create_worker_session
            from packages.core.models.execution import ExecutionPlan
            from packages.core.models.task import Task
            from packages.core.services.task_state_machine import apply_task_status_transition
            from sqlalchemy import select
            from datetime import datetime, timezone
            async with create_worker_session()() as db:
                plan = (await db.execute(
                    select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
                )).scalar_one_or_none()
                event_payload = None
                if plan and plan.status not in ("completed", "failed", "cancelled"):
                    plan.status = "failed"
                    plan.completed_at = datetime.now(timezone.utc)
                    if plan.task_id:
                        task = (await db.execute(
                            select(Task).where(Task.id == plan.task_id)
                        )).scalar_one_or_none()
                        if task and task.status == "in_progress":
                            apply_task_status_transition(task, "failed")
                            task.actual_output = {
                                "plan_id": plan.id,
                                "plan_status": "failed",
                                "error_type": "PlanWorkerExhausted",
                                "error_message": error_msg,
                            }
                            event_payload = {
                                "task_id": task.id,
                                "title": task.title,
                                "plan_id": plan.id,
                                "plan_status": "failed",
                                "task_status": "failed",
                                "error_type": "PlanWorkerExhausted",
                                "error_message": error_msg,
                            }
                await db.commit()
                if plan and event_payload:
                    from packages.core.services import event_emitter
                    event_emitter.emit(
                        plan.entity_id,
                        "task.failed",
                        source="ai_tasks",
                        payload=event_payload,
                    )
        _run_async(_do())
    except Exception:
        logger.exception("Failed to mark plan %s as failed", plan_id)


def _mark_task_failed(task_id: str, error_msg: str) -> None:
    """Mark a Task as failed after planning crashes."""
    try:
        async def _do():
            from packages.core.database import create_worker_session
            from packages.core.models.task import Task
            from packages.core.services.task_state_machine import apply_task_status_transition
            from sqlalchemy import select
            async with create_worker_session()() as db:
                task = (await db.execute(
                    select(Task).where(Task.id == task_id)
                )).scalar_one_or_none()
                event_payload = None
                if task and task.status == "in_progress":
                    apply_task_status_transition(task, "failed")
                    task.actual_output = {
                        "task_status": "failed",
                        "error_type": "TaskPlanningExhausted",
                        "error_message": error_msg,
                    }
                    event_payload = {
                        "task_id": task.id,
                        "title": task.title,
                        "task_status": "failed",
                        "error_type": "TaskPlanningExhausted",
                        "error_message": error_msg,
                    }
                await db.commit()
                if task and event_payload:
                    from packages.core.services import event_emitter
                    event_emitter.emit(
                        task.entity_id,
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
) -> None:
    """Mark a scheduled_job_runs row + its parent scheduled_jobs as
    completed/skipped/error.

    Status decided in this priority order:
      - ``error`` set → "error"
      - ``result["skipped"] == True`` → "skipped"
      - otherwise → "completed"
    """
    if not run_id and not job_id_str:
        return
    from datetime import datetime, timezone
    from sqlalchemy import select
    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    if error:
        run_status = "error"
    elif result and result.get("skipped"):
        run_status = "skipped"
    else:
        run_status = "completed"
    now = datetime.now(timezone.utc)

    session_factory = create_worker_session()
    async with session_factory() as db:
        if run_id:
            run = (await db.execute(
                select(ScheduledJobRun).where(ScheduledJobRun.id == run_id)
            )).scalar_one_or_none()
            if run and run.status == "running":
                run.status = run_status
                run.completed_at = now
                if run.started_at:
                    run.duration_ms = (now - run.started_at).total_seconds() * 1000
                if error:
                    run.error = error[:1000]
                if result is not None:
                    run.result = result if isinstance(result, dict) else {"value": result}
        if job_id_str:
            job = (await db.execute(
                select(ScheduledJob).where(ScheduledJob.job_id == job_id_str)
            )).scalar_one_or_none()
            if job:
                job.last_status = run_status
                if run_status == "error":
                    job.consecutive_errors = (job.consecutive_errors or 0) + 1
                else:
                    job.consecutive_errors = 0
        await db.commit()


def _compact_error(error_msg: str, *, limit: int = 500) -> str:
    """Keep user-facing failure cards readable without losing the root cause."""
    compact = " ".join(str(error_msg or "Unknown error").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


async def _post_strategist_failure_card(
    *,
    workspace_id: str,
    trigger: str,
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
            "Strategist review failed after automatic retries.\n\n"
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
                "kind": "retry_strategist_review",
                "workspace_id": workspace.id,
                "trigger": trigger,
                "run_id": run_id,
                "job_id": job_id_str,
                "error": reason,
                "options": ["retry"],
            },
        )
        await db.commit()


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
    """Execute a scheduled-task body with the standard 3-exit-path
    finalize pattern: success / credits-exhausted / retry-exhausted.

    Reduces 12 lines of boilerplate per task to one call. ``coro_factory``
    is called with no args and must return a coroutine — the lambda
    pattern lets callers close over their own imports/state without a
    nested ``async def _go``.
    """
    try:
        result = _run_async(coro_factory())
        _run_async(_finalize_scheduled_run(
            run_id=run_id, job_id_str=job_id_str,
            result=result if isinstance(result, dict) else None,
        ))
        return result
    except CreditExhaustedError as exc:
        try:
            _retry_once_on_credit_exhausted(self, exc, countdown=120)
        except Exception:
            raise
        logger.warning("%s %s skipped: credits exhausted", label, workspace_id)
        _run_async(_finalize_scheduled_run(
            run_id=run_id, job_id_str=job_id_str,
            error=f"credits_exhausted: {exc}",
        ))
        if on_terminal_error:
            _run_async(on_terminal_error(f"credits_exhausted: {exc}"))
    except Exception as exc:
        logger.error("%s %s failed: %s", label, workspace_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _run_async(_finalize_scheduled_run(
                run_id=run_id, job_id_str=job_id_str, error=str(exc),
            ))
            if on_terminal_error:
                try:
                    _run_async(on_terminal_error(str(exc)))
                except Exception:
                    logger.exception("Failed to post terminal failure card for %s %s", label, workspace_id)
        raise self.retry(exc=exc, countdown=countdown_on_retry)


@celery_app.task(bind=True, max_retries=0)
def internal_worker_tick(self):
    """Heartbeat for the in-process InternalWorker(s).

    Celery beat fires this every ``INTERNAL_WORKER_TICK_SECONDS``. For
    each active internal worker (one per entity), the task does a
    Dispatcher checkout and fans out per-lease execution to its own
    Celery task — so a slow LLM call doesn't block the next tick.
    """
    try:
        from packages.core.workers.internal import tick_all_internal_workers
        leased = _run_async(tick_all_internal_workers())
        if leased:
            logger.info("internal_worker_tick: %d leases issued", leased)
    except Exception:
        logger.exception("internal_worker_tick failed")


@celery_app.task(bind=True, max_retries=2, soft_time_limit=1800, time_limit=2100)
def execute_lease(self, lease_id: str):
    """Execute one lease via the InternalWorker. Reports completion /
    failure / needs_human back to the Dispatcher.

    Per-lease task because LLM / browser steps can take seconds — we
    don't want one slow lease blocking the tick or starving sibling
    leases. Celery's per-task retry policy handles transient infra
    blips (DB hiccup); business-level failures land in fail_lease via
    the InternalWorker itself."""
    logger.info("execute_lease %s (attempt %d)", lease_id, self.request.retries + 1)
    try:
        from packages.core.workers.internal import execute_lease_inproc
        return _run_async(execute_lease_inproc(lease_id))
    except CreditExhaustedError as exc:
        _retry_once_on_credit_exhausted(self, exc, countdown=120)
        logger.warning("execute_lease %s aborted: credits exhausted", lease_id)
    except Exception as exc:
        logger.error("execute_lease %s infrastructure failure: %s", lease_id, exc)
        raise self.retry(exc=exc, countdown=30)


@celery_app.task(bind=True, max_retries=0)
def cleanup_expired_leases(self):
    """Reclaim leases past their TTL. Beat-driven."""
    try:
        from packages.core.dispatcher import Dispatcher
        from packages.core.database import create_worker_session

        async def _go():
            async with create_worker_session()() as db:
                n = await Dispatcher().expire_leases(db)
                await db.commit()
                return n

        n = _run_async(_go())
        if n:
            logger.info("cleanup_expired_leases: reclaimed %d", n)
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
            )

    return _run_scheduled(
        self, "Briefing", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=300,
    )


@celery_app.task(bind=True, max_retries=2)
def run_outcome_evaluation(
    self, workspace_id: str,
    *, run_id: str | None = None, job_id_str: str | None = None,
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
            result = await evaluate_workspace_outcomes(db, workspace_id)
            await db.commit()
            return result

    return _run_scheduled(
        self, "Outcome evaluation", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=900,
    )


@celery_app.task(bind=True, max_retries=0, name="billing.refresh_plans_cache")
def refresh_plans_cache(self):
    """Reload subscription_plans into the in-process PLANS cache.

    Sibling API/worker processes won't see admin-side plan edits until
    their own cache refreshes — this beat task closes the gap so the
    drift window is bounded by the schedule (5 min).
    """
    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.constants.plans import load_plans_into_cache
        async with create_worker_session()() as db:
            return await load_plans_into_cache(db)

    try:
        n = _run_async(_go())
        logger.debug("refresh_plans_cache: %d plan(s) loaded", n or 0)
    except Exception:
        logger.warning("refresh_plans_cache failed", exc_info=True)




@celery_app.task(bind=True, max_retries=2)
def run_chat_insight_extraction(
    self, workspace_id: str,
    *, run_id: str | None = None, job_id_str: str | None = None,
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
            result = await extract_chat_insights(db, workspace_id)
            await db.commit()
            return result

    return _run_scheduled(
        self, "Chat extraction", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=900,
    )


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


@celery_app.task(bind=True, max_retries=2)
def run_strategist_review(
    self, workspace_id: str, trigger: str = "scheduled",
    *, run_id: str | None = None, job_id_str: str | None = None,
):
    """Trigger one Strategist review cycle.

    Cadence comes from a ScheduledJob row tagged
    ``execution_type='strategist_review'`` (see strategist.scheduling).
    Manual triggers route through the same task via ``.delay()``.
    """
    logger.info(
        "Strategist review for workspace %s (attempt %d)",
        workspace_id, self.request.retries + 1,
    )

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.strategist import run_review
        from packages.core.ai.runtime import runtime_ensure_strategist_review_billing_context
        async with create_worker_session()() as db:
            await runtime_ensure_strategist_review_billing_context(
                db,
                workspace_id,
            )
            skip_open = not trigger.startswith("user_feedback")
            return await run_review(db, workspace_id, trigger=trigger, skip_if_open_proposals=skip_open)

    async def _notify_failure(error_msg: str):
        await _post_strategist_failure_card(
            workspace_id=workspace_id,
            trigger=trigger,
            error_msg=error_msg,
            run_id=run_id,
            job_id_str=job_id_str,
        )

    return _run_scheduled(
        self, "Strategist review", workspace_id, _go,
        run_id=run_id, job_id_str=job_id_str, countdown_on_retry=300,
        on_terminal_error=_notify_failure,
    )


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
        from packages.core.ai.runtime import runtime_ensure_goal_measurement_billing_context
        from packages.core.goals.measurement import measure_goal
        # Set billing context in same event loop as the LLM call.
        async with create_worker_session()() as db:
            await runtime_ensure_goal_measurement_billing_context(
                db,
                goal_id,
            )
        return await measure_goal(goal_id)

    return _run_scheduled(
        self,
        "Goal measurement",
        goal_id,
        _go,
        run_id=run_id,
        job_id_str=job_id_str,
        countdown_on_retry=300,
    )


@celery_app.task(bind=True, max_retries=3)
def run_plan(self, plan_id: str):
    """Drive an ExecutionPlan one cycle (Demo A v0 pre-Worker shape).

    Replaces the old ``run_goal`` task. Delegates to PlanExecutor which
    materialises the DAG, dispatches the next runnable step in-process
    (no Worker/Dispatcher abstraction yet — that lands in M3), and
    re-enqueues itself for the next cycle when more steps remain.
    """
    logger.info("Running plan %s (attempt %d)", plan_id, self.request.retries + 1)
    try:
        # Billing context is set inside PlanExecutor.run_cycle() after
        # loading the plan's entity_id — same event loop as the LLM calls.
        from packages.core.plans.executor import PlanExecutor
        result = _run_async(PlanExecutor().run_cycle(plan_id))
        # Re-enqueue ourselves for the next cycle when the executor
        # asks for it. Sleep steps come back with a delay; otherwise
        # we cycle immediately so multi-step plans don't sit idle.
        next_action = (result or {}).get("next_action")
        if next_action == "schedule_self":
            delay = (result or {}).get("delay_seconds") or 0
            run_plan.apply_async(args=[plan_id], countdown=max(0, int(delay)))
        return result
    except CreditExhaustedError as exc:
        # Don't retry — credits won't refill between attempts
        logger.warning("Plan %s halted: credits exhausted", plan_id)
        _mark_plan_failed(plan_id, f"Credits exhausted: {exc}")
    except Exception as exc:
        logger.error("Plan %s failed: %s", plan_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _mark_plan_failed(plan_id, f"Plan execution crashed after {self.max_retries + 1} attempts: {exc}")
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=2, soft_time_limit=900, time_limit=1080)
def plan_and_run_task(self, task_id: str):
    """Plan a task → persist as ExecutionPlan → dispatch first cycle.

    Triggered when a Task with ``owner_subscription_id`` transitions
    to ``in_progress`` (see ``task_service.update_task``). Splits cleanly
    in two so each can fail / retry without losing the other:

      1. ``plan_task(task_id)`` → ExecutionPlan row + materialised steps
      2. ``run_plan.delay(plan.id)`` → executor cycle

    If the plan needs approval (high-risk steps), step 2 is skipped —
    the user approves via the API which then dispatches.
    """
    logger.info("Planning task %s (attempt %d)", task_id, self.request.retries + 1)

    async def _go():
        from packages.core.database import create_worker_session
        from packages.core.plans.planner import plan_task
        from packages.core.ai.runtime import runtime_ensure_plan_and_run_task_billing_context

        async with create_worker_session()() as db:
            await runtime_ensure_plan_and_run_task_billing_context(
                db,
                task_id,
            )
            plan = await plan_task(db, task_id, execution_mode="live")
            await db.commit()
            return plan.id, plan.status

    try:
        plan_id, status = _run_async(_go())
        if status not in ("pending_approval", "needs_attention"):
            run_plan.delay(plan_id)
        return {"plan_id": plan_id, "status": status}
    except CreditExhaustedError as exc:
        logger.warning("Planning task %s halted: credits exhausted", task_id)
        _mark_task_failed(task_id, f"Credits exhausted: {exc}")
    except PlanContractError as exc:
        # Contract gaps are deterministic — a blind retry would just reproduce
        # the same unresolvable plan. Fail the task immediately with the gap
        # reason so it surfaces to the operator instead of churning retries.
        logger.error("Planning task %s failed contract enforcement: %s", task_id, exc)
        _mark_task_failed(task_id, f"Plan contract gaps: {exc}")
        return {"plan_id": None, "status": "failed"}
    except Exception as exc:
        logger.error("Planning task %s failed: %s", task_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            _mark_task_failed(task_id, f"Planning failed after {self.max_retries + 1} attempts: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=3, soft_time_limit=1500, time_limit=1800)
def run_agent_task(self, task_id: str, agent_id: str | None = None):
    """Dispatch an agent to work on a task ticket.

    Called when a task is assigned to an agent. The agent will execute
    its tool loop until the task is complete or max turns are reached.
    """
    logger.info(
        "Running agent task: task=%s agent=%s (attempt %d)",
        task_id,
        agent_id,
        self.request.retries + 1,
    )
    try:
        from packages.core.ai.task_runner import TaskRunner
        from packages.core.database import create_worker_session
        session_factory = create_worker_session()
        result = _run_async(TaskRunner(session_factory=session_factory).run(task_id, agent_id))
        logger.info(
            "Agent task completed: task=%s status=%s turns=%s",
            task_id,
            result.get("status"),
            result.get("turns_used"),
        )

        # Update scheduled job run status if this task was triggered by a scheduled job
        _update_job_run_status(session_factory, task_id, result)

        return result
    except CreditExhaustedError:
        logger.warning("Agent task %s aborted: credits exhausted", task_id)
    except Exception as exc:
        logger.error("Agent task %s failed: %s", task_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


def _update_job_run_status(session_factory, task_id: str, result: dict):
    """Update the ScheduledJobRun and ScheduledJob status after agent execution."""
    import asyncio

    async def _update():
        from datetime import datetime, timezone
        from sqlalchemy import select
        from packages.core.models.task import Task
        from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

        async with session_factory() as db:
            # Load task to get scheduled_run_id from details
            tr = await db.execute(select(Task).where(Task.id == task_id))
            task = tr.scalar_one_or_none()
            if not task:
                return
            run_id = (task.details or {}).get("scheduled_run_id")
            job_id_str = (task.details or {}).get("scheduled_job_id")
            if not run_id and not job_id_str:
                return

            status = "success" if result.get("status") == "completed" else "error"
            duration_ms = result.get("duration_ms")
            error_msg = None if status == "success" else result.get("response", "")[:500]

            # Update the run record
            if run_id:
                rr = await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.id == run_id))
                run = rr.scalar_one_or_none()
                if run:
                    run.status = status
                    run.duration_ms = duration_ms
                    run.error = error_msg
                    run.completed_at = datetime.now(timezone.utc)
                    run.result = result if isinstance(result, dict) else {"value": result}

            # Update the job's last_status
            if job_id_str:
                jr = await db.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id_str))
                job = jr.scalar_one_or_none()
                if job:
                    job.last_status = status
                    if status == "error":
                        job.consecutive_errors = (job.consecutive_errors or 0) + 1
                    else:
                        job.consecutive_errors = 0

            await db.commit()

    try:
        asyncio.run(_update())
    except Exception as e:
        logger.warning("Failed to update job run status for task %s: %s", task_id, e)


@celery_app.task(bind=True, max_retries=2)
def generate_job_skill(self, job_id: str, payload_message: str, job_name: str = ""):
    """Auto-generate a Skill for a scheduled job via LLM.

    Uses skill_generator.generate_skill() to create a proper Skill entity
    in the DB, then links it to the ScheduledJob via execution_target.skill_id.
    The Skill's system_prompt becomes the frozen procedure for every run.
    """
    logger.info("Generating skill for job %s", job_id)
    try:
        from packages.core.database import create_worker_session

        async def _generate():
            from sqlalchemy import select
            from packages.core.models.scheduler import ScheduledJob
            from packages.core.services.skill_generator import generate_skill

            session_factory = create_worker_session()
            async with session_factory() as db:
                # Load the job to get entity_id
                result = await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))
                job = result.scalar_one_or_none()
                if not job:
                    logger.warning("Job %s not found for skill generation", job_id)
                    return

                # Generate a Skill via LLM (creates a real Skill entity in DB)
                prompt = f"Scheduled automation: {job_name or job.name or 'Scheduled Task'}\n\n{payload_message}"
                skill = await generate_skill(
                    prompt=prompt,
                    entity_id=job.entity_id or "",
                    db=db,
                    category="automation",
                    tags=["auto-generated", "scheduled-job", job.job_id],
                    config_overrides={
                        "source": "scheduled_job",
                        "generation_source": "llm-generated",
                        "scheduled_job_id": job.job_id,
                        "scheduled_job_pk": job.id,
                        "workspace_id": job.workspace_id,
                        "agent_id": job.agent_id,
                        "automation_name": job.name,
                    },
                )

                # Link skill to the job + store LLM-determined complexity
                complexity = (skill.config or {}).get("complexity", "primary")
                target = dict(job.execution_target or {})
                target["skill_id"] = skill.id
                target["complexity"] = complexity
                job.execution_target = target
                job.execution_type = "skill"
                job.execution_script = skill.system_prompt

                await db.commit()
                logger.info(
                    "Generated skill %s (%s) for job %s",
                    skill.id, skill.name, job_id,
                )

        _run_async(_generate())
    except CreditExhaustedError:
        logger.warning("Skill generation for job %s skipped: credits exhausted", job_id)
    except Exception as exc:
        logger.error("Skill generation failed for job %s: %s", job_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(name='run_workflow', bind=True, max_retries=2)
def run_workflow(self, workflow_run_id: str):
    """Execute a workflow run.

    Called when a workflow is started. Delegates to WorkflowRunner which
    processes steps sequentially (or in parallel where specified), handles
    condition branching, and pauses on wait steps.  Each invocation runs
    the workflow to completion or pause.
    """
    logger.info("Running workflow %s (attempt %d)", workflow_run_id, self.request.retries + 1)
    try:
        from packages.core.ai.workflow_runner import WorkflowRunner
        _run_async(WorkflowRunner().run(workflow_run_id))
    except CreditExhaustedError:
        logger.warning("Workflow %s aborted: credits exhausted", workflow_run_id)
    except Exception as exc:
        logger.error("Workflow %s failed: %s", workflow_run_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=3)
def fetch_and_index_url_document(self, document_id: str, url: str):
    """Fetch content from a URL, save to filesystem, then index.

    Called when a user imports a document via URL. The API returns a
    placeholder document immediately; this task does the actual work.
    """
    logger.info(
        "Fetching URL document %s from %s (attempt %d)",
        document_id, url, self.request.retries + 1,
    )
    try:
        from packages.core.database import create_worker_session

        async def _fetch_and_index():
            import os
            from sqlalchemy import select
            from packages.core.models.document import Document, VectorStatus
            from packages.core.services.web_fetch import fetch_url
            from packages.core.services.embedding_service import index_document
            from packages.core.config import get_settings

            settings = get_settings()
            session_factory = create_worker_session()
            async with session_factory() as db:
                result = await db.execute(select(Document).where(Document.id == document_id))
                doc = result.scalar_one_or_none()
                if not doc:
                    raise RuntimeError(f"Document {document_id} not found")

                # Mark as processing
                doc.vector_status = VectorStatus.PROCESSING
                await db.flush()

                # Fetch URL
                max_bytes = settings.MANOR_MAX_UPLOAD_MB * 1024 * 1024
                fetched = await fetch_url(url, max_bytes=max_bytes)
                ct = fetched.content_type

                # Update document metadata from actual response
                if ct:
                    doc.mime_type = ct.split(";")[0].strip() or doc.mime_type
                doc.file_size = len(fetched.content)

                # Infer file type from content-type if needed
                if "pdf" in ct:
                    if not doc.name.lower().endswith(".pdf"):
                        doc.name = os.path.splitext(doc.name)[0] + ".pdf"
                    doc.file_type = "pdf"

                # Save to filesystem
                if settings.MANOR_FS_ENABLED:
                    from packages.core.services.entity_fs import write_entity_file_atomic

                    entity_root = os.path.join(settings.MANOR_FS_ROOT, doc.entity_id)
                    os.makedirs(entity_root, exist_ok=True)
                    import time as _time
                    base, ext = os.path.splitext(doc.name)
                    rel_path = doc.name
                    if os.path.exists(os.path.join(entity_root, rel_path)):
                        rel_path = f"{base}_{int(_time.time())}{ext}"
                    target = write_entity_file_atomic(
                        doc.entity_id,
                        rel_path,
                        fetched.content,
                        expected_size=len(fetched.content),
                        allow_empty=False,
                    )
                    doc.fs_path = os.path.relpath(target, entity_root)

                await db.flush()
                await db.commit()

                # Now index (generates embedding)
                async with session_factory() as db2:
                    success = await index_document(db2, document_id)
                    await db2.commit()
                    return success

        success = _run_async(_fetch_and_index())
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

            async def _mark_failed():
                from sqlalchemy import select
                from packages.core.models.document import Document
                session_factory = create_worker_session()
                async with session_factory() as db:
                    result = await db.execute(select(Document).where(Document.id == document_id))
                    doc = result.scalar_one_or_none()
                    if doc:
                        doc.vector_status = VectorStatus.FAILED
                        meta = dict(doc.metadata_ or {})
                        meta["fetch_error"] = error_message
                        doc.metadata_ = meta
                        await db.commit()

            _run_async(_mark_failed())
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=3)
def process_document_embeddings(self, document_id: str):
    """Generate embeddings for a document (RAG pipeline).

    Called when a document is uploaded or updated. Chunks the document,
    generates embeddings via configured model, and stores them in pgvector.
    """
    logger.info(
        "Processing embeddings for document %s (attempt %d)",
        document_id,
        self.request.retries + 1,
    )
    try:
        from packages.core.services.embedding_service import index_document
        from packages.core.database import create_worker_session

        async def _index():
            session_factory = create_worker_session()
            async with session_factory() as db:
                success = await index_document(db, document_id)
                await db.commit()
                return success

        success = _run_async(_index())
        if not success:
            raise RuntimeError(f"Indexing returned False for document {document_id}")
        logger.info("Successfully indexed document %s", document_id)
        return {"document_id": document_id, "status": "ready"}
    except Exception as exc:
        logger.error("Embedding failed for %s: %s", document_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@celery_app.task(bind=True, max_retries=1)
def send_agent_greetings(self, entity_id: str, workspace_id: str,
                         workspace_name: str, workspace_kind: str,
                         agent_data: list[dict]):
    """Post greeting messages from each subscribed agent to workspace chat.

    Dispatched by finalize_setup() after workspace creation. Each agent
    introduces itself with a short LLM-generated greeting.

    agent_data: [{subscription_id, agent_name, service_key, system_prompt}, ...]
    """
    import asyncio

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
            )

            # Stagger messages slightly so they don't all arrive at once
            if i < len(agent_data) - 1:
                await asyncio.sleep(1.5)

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
):
    """Generate starter document for an approved knowledge base."""
    logger.info("Generating knowledge content: group=%s name=%s", group_id, group_name)

    async def _generate():
        from packages.core.ai.runtime import (
            runtime_execute_knowledge_starter_document_completion,
        )
        from packages.core.database import create_worker_session
        from packages.core.models.document import DocumentGroup, DocumentGroupMember
        from packages.core.services.document_service import create_document
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
        )
        content = completion.content

        if not content or not content.strip():
            return

        async with create_worker_session()() as db:
            from packages.core.services.document_metadata import merge_document_metadata

            doc = await create_document(
                db,
                entity_id,
                name=f"{group_name}.md",
                file_type="md",
                mime_type="text/markdown",
                source="ai_generated",
                metadata=merge_document_metadata(
                    origin={"workspace_id": workspace_id, "tool_name": "workspace_starter_doc"},
                    artifact={"role": "final"},
                    extra={"auto_generated": True, "group_id": group_id},
                ),
            )
            doc.vector_status = "pending"
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
                group.settings = settings

            try:
                import os
                import time as _time
                from packages.core.config import get_settings
                from packages.core.services.entity_fs import write_entity_file_atomic
                from packages.core.services.knowledge_visibility import is_user_visible_path

                settings = get_settings()
                if settings.MANOR_FS_ENABLED:
                    entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
                    os.makedirs(entity_root, exist_ok=True)
                    filename = os.path.basename(f"{group_name}.md") or "Knowledge Starter.md"
                    if not is_user_visible_path(filename):
                        filename = "Knowledge Starter.md"
                    target = os.path.normpath(os.path.join(entity_root, filename))
                    entity_root_norm = os.path.normpath(entity_root)
                    if os.path.commonpath([entity_root_norm, target]) != entity_root_norm:
                        raise ValueError("Generated knowledge filename escaped entity root")
                    if os.path.exists(target):
                        base, ext = os.path.splitext(filename)
                        target = os.path.join(entity_root, f"{base}_{int(_time.time())}{ext}")
                    content_bytes = content.strip().encode("utf-8")
                    target = write_entity_file_atomic(
                        entity_id,
                        os.path.relpath(target, entity_root),
                        content_bytes,
                        expected_size=len(content_bytes),
                        allow_empty=False,
                    )
                    doc.fs_path = os.path.relpath(target, entity_root)
                    doc.file_size = len(content_bytes)
                else:
                    doc.metadata_ = {**(doc.metadata_ or {}), "content_text": content.strip()[:50000]}
            except Exception:
                logger.warning("Failed to write starter doc %s to filesystem", doc_id, exc_info=True)
                doc.metadata_ = {**(doc.metadata_ or {}), "content_text": content.strip()[:50000]}

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
