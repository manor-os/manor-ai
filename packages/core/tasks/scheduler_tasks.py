"""Celery tasks for the scheduled job system.

The main tick task runs every 60 seconds (via Celery Beat) and checks
the scheduled_jobs table for due jobs. When a job is due, it dispatches
the appropriate execution task.
"""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.celery_app import celery_app
from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS

logger = logging.getLogger(__name__)


INTERVAL_SCHEDULE_KINDS = ("every", "interval")
_DEFAULT_AGENT_TASK_MAX_TURNS = DEFAULT_AGENT_MAX_TURNS
_FILE_DELIVERABLE_MIN_TURNS = DEFAULT_AGENT_MAX_TURNS
_FILE_DELIVERABLE_KINDS = frozenset(
    {
        "video",
        "mp4",
        "image",
        "png",
        "jpg",
        "jpeg",
        "pdf",
        "ppt",
        "pptx",
        "presentation",
        "slides",
        "deck",
        "spreadsheet",
        "xlsx",
        "csv",
        "document",
        "docx",
        "audio",
        "mp3",
    }
)


def _run_async(coro):
    """Bridge async to sync for Celery workers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _target_file_deliverable_kind(target: dict) -> str:
    """Read file-output intent from structured scheduler target fields only."""
    if not isinstance(target, dict):
        return ""
    candidates = [
        target.get("output_kind"),
        target.get("file_kind"),
        target.get("artifact_kind"),
    ]
    deliverable = target.get("deliverable")
    if isinstance(deliverable, dict):
        candidates.extend([
            deliverable.get("kind"),
            deliverable.get("file_kind"),
            deliverable.get("output_kind"),
        ])
    output = target.get("output")
    if isinstance(output, dict):
        candidates.extend([
            output.get("kind"),
            output.get("file_kind"),
            output.get("type"),
        ])
    generate_file = target.get("generate_file")
    if isinstance(generate_file, dict):
        candidates.append(generate_file.get("kind"))
    for candidate in candidates:
        kind = str(candidate or "").strip().lower()
        if kind in _FILE_DELIVERABLE_KINDS:
            return kind
    return ""


def _target_requests_file_deliverable(target: dict) -> bool:
    if not isinstance(target, dict):
        return False
    if target.get("requires_generated_file") is True:
        return True
    if target.get("requires_file_deliverable") is True:
        return True
    return bool(_target_file_deliverable_kind(target))


def _agent_task_max_turns_for_target(target: dict) -> int:
    explicit = target.get("max_turns")
    try:
        explicit_turns = int(explicit) if explicit is not None else None
    except (TypeError, ValueError):
        explicit_turns = None
    if explicit_turns is not None:
        return explicit_turns
    base = _DEFAULT_AGENT_TASK_MAX_TURNS
    if _target_requests_file_deliverable(target):
        return max(base, _FILE_DELIVERABLE_MIN_TURNS)
    return base


def _tighten_file_deliverable_completion(*, target: dict, done_when: str, deliverable: str) -> tuple[str, str]:
    if not _target_requests_file_deliverable(target):
        return done_when, deliverable

    suffix = (
        " The file/media deliverable is not complete until an available generation "
        "tool such as generate_file has been called, the returned file/job status "
        "has been reported, and the final answer includes the generated file path, "
        "document id, result URL, or terminal failure reason. A text-only report or "
        "prepared prompt is not sufficient."
    )
    if "text-only report" not in done_when.lower():
        done_when = (done_when.rstrip() + suffix).strip()

    kind = _target_file_deliverable_kind(target) or "file/media"
    if "generated file path" not in deliverable.lower() and "document id" not in deliverable.lower():
        deliverable = (
            deliverable.rstrip()
            + f" Include the generated {kind} file path/document id/result URL or a terminal generation failure reason."
        ).strip()
    return done_when, deliverable


@celery_app.task(bind=True, name="scheduler.tick")
def scheduler_tick(self):
    """Main scheduler tick — runs every 60s via Celery Beat.

    Scalable approach:
    1. Query only CANDIDATE jobs from DB (pre-filtered by schedule type)
    2. Check due-ness in Python for the candidates
    3. Dispatch each due job as a separate Celery task (fan-out)
    4. Each dispatch runs independently on any available worker

    This handles 100K+ jobs because:
    - DB does the heavy filtering (index on enabled + last_run_at)
    - Python only checks a small candidate set
    - Actual execution is fanned out across workers
    """
    _run_async(_async_tick())


async def _async_tick():
    from packages.core.database import create_worker_session
    from sqlalchemy import select, or_, and_
    from packages.core.models.scheduler import ScheduledJob

    now = datetime.now(timezone.utc)

    async with create_worker_session()() as db:
        # Pre-filter candidates at DB level to avoid loading 100K rows:
        # 1. Interval jobs: last_run_at is old enough OR never ran
        # 2. Cron jobs: last_run_at is NOT in the current minute (avoid re-trigger)
        # 3. One-shot jobs: run_at <= now AND never ran
        current_minute_start = now.replace(second=0, microsecond=0)

        result = await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.enabled == True,  # noqa: E712
                or_(
                    # Interval: never ran, or enough time elapsed
                    and_(
                        ScheduledJob.schedule_kind.in_(INTERVAL_SCHEDULE_KINDS),
                        or_(
                            ScheduledJob.last_run_at.is_(None),
                            ScheduledJob.last_run_at < now,  # refined in Python
                        ),
                    ),
                    # Cron: not ran this minute
                    and_(
                        ScheduledJob.schedule_kind == "cron",
                        or_(
                            ScheduledJob.last_run_at.is_(None),
                            ScheduledJob.last_run_at < current_minute_start,
                        ),
                    ),
                    # One-shot: never ran
                    and_(
                        ScheduledJob.schedule_kind == "at",
                        ScheduledJob.last_run_at.is_(None),
                    ),
                ),
            )
        )
        candidates = list(result.scalars().all())

        if not candidates:
            return

        # Fine-grained check in Python + fan-out dispatch
        dispatched = 0
        for job in candidates:
            if _is_due(job, now):
                try:
                    # Fan out: dispatch each job's execution as a separate Celery task
                    _dispatch_job_task.delay(job.id, now.isoformat())
                    # Mark as dispatched to prevent re-triggering next tick
                    job.last_run_at = now
                    job.last_status = "dispatched"
                    dispatched += 1
                except Exception as e:
                    logger.error("Failed to dispatch job %s: %s", job.job_id, e)
                    job.consecutive_errors = (job.consecutive_errors or 0) + 1
                    job.last_status = "error"

        await db.commit()
        if dispatched:
            logger.info("Scheduler tick: dispatched %d of %d candidates", dispatched, len(candidates))


@celery_app.task(bind=True, name="scheduler.dispatch_job", max_retries=2)
def _dispatch_job_task(self, job_db_id: str, now_iso: str, manual: bool = False):
    """Execute a single scheduled job — fanned out from scheduler_tick
    or invoked directly via the run_now API.

    Each job runs as its own Celery task, so 100 due jobs = 100 parallel
    tasks across the worker pool, not a single blocking loop.
    """
    try:
        _run_async(_async_dispatch_single(job_db_id, now_iso, manual=manual))
    except Exception as exc:
        logger.error("dispatch_job %s failed: %s", job_db_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=30)


async def _async_dispatch_single(
    job_db_id: str, now_iso: str, *, manual: bool = False,
):
    from packages.core.database import create_worker_session
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob

    now = datetime.fromisoformat(now_iso)

    async with create_worker_session()() as db:
        result = await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_db_id))
        job = result.scalar_one_or_none()
        if not job:
            return

        await _dispatch_job(db, job, now, manual=manual)
        await db.commit()


def _as_aware_utc(dt: datetime) -> datetime:
    """Normalize datetimes from DB/tests to aware UTC.

    Some DB drivers can return timezone columns as naive datetimes. The
    scheduler writes UTC, so naive values are treated as UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _job_zoneinfo(job):
    tz_name = getattr(job, "timezone", None) or "UTC"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Scheduled job %s has invalid timezone %r; falling back to UTC",
            getattr(job, "job_id", "?"),
            tz_name,
        )
        return timezone.utc


def _parse_run_at(run_at: str, job) -> datetime:
    """Parse a one-shot run_at value into UTC.

    UI datetime pickers send local wall-clock values like
    ``2026-05-01T09:00``. Interpret those in the job timezone instead of UTC.
    Values that already include an offset keep their explicit offset.
    """
    dt = datetime.fromisoformat(run_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_job_zoneinfo(job))
    return dt.astimezone(timezone.utc)


def _is_due(job, now: datetime) -> bool:
    """Check if a scheduled job is due for execution."""
    now_utc = _as_aware_utc(now)

    # One-shot jobs — fire if run_at <= now AND either never ran or run_at changed since last run
    if job.schedule_kind == "at" and job.run_at:
        try:
            run_at = _parse_run_at(job.run_at, job)
            if run_at > now_utc:
                return False  # not yet
            if not job.last_run_at:
                return True  # never ran
            # Re-trigger if run_at was changed to a time after last_run
            return run_at > _as_aware_utc(job.last_run_at)
        except (ValueError, TypeError):
            return False

    # Interval jobs (every_seconds)
    if job.schedule_kind in INTERVAL_SCHEDULE_KINDS and job.every_seconds:
        if not job.last_run_at:
            return True
        elapsed = (now_utc - _as_aware_utc(job.last_run_at)).total_seconds()
        return elapsed >= job.every_seconds

    # Cron jobs
    if job.schedule_kind == "cron" and job.cron_expr:
        tz = _job_zoneinfo(job)
        local_now = now_utc.astimezone(tz)
        local_last_run = (
            _as_aware_utc(job.last_run_at).astimezone(tz)
            if job.last_run_at
            else None
        )
        return _cron_matches(job.cron_expr, local_now, local_last_run)

    return False


def _cron_matches(expr: str, now: datetime, last_run) -> bool:
    """Simple cron expression matcher.

    Supports: minute hour day_of_month month day_of_week
    Each field can be: * (any), number, comma-list, range, */N (every N)

    Only triggers if we haven't run in the current matching minute.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    fields = [now.minute, now.hour, now.day, now.month, now.weekday()]
    # Note: cron uses 0=Sunday, Python weekday() uses 0=Monday
    # Adjust: convert Python weekday to cron (0=Sun): (weekday + 1) % 7
    fields[4] = (fields[4] + 1) % 7

    for field_val, pattern in zip(fields, parts):
        if not _cron_field_matches(field_val, pattern):
            return False

    # Don't re-trigger if already run this minute
    if last_run:
        if last_run.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
            return False

    return True


def _cron_field_matches(field_val: int, pattern: str) -> bool:
    pattern = pattern.strip()
    if pattern == "*":
        return True

    for part in pattern.split(","):
        part = part.strip()
        if not part:
            return False
        try:
            if "/" in part:
                base, raw_step = part.split("/", 1)
                step = int(raw_step)
                if step <= 0:
                    return False
                if base == "*":
                    if field_val % step == 0:
                        return True
                    continue
                if "-" in base:
                    start, end = [int(v) for v in base.split("-", 1)]
                    if start <= field_val <= end and (field_val - start) % step == 0:
                        return True
                    continue
                if field_val == int(base):
                    return True
                continue
            if "-" in part:
                start, end = [int(v) for v in part.split("-", 1)]
                if start <= field_val <= end:
                    return True
                continue
            if field_val == int(part):
                return True
        except ValueError:
            return False

    return False


async def _dispatch_job(db, job, now: datetime, *, manual: bool = False):
    """Dispatch the appropriate execution for a job."""
    from packages.core.ai.runtime import (
        runtime_scheduled_job_prompt,
        runtime_scheduled_skill_prompt,
    )
    from packages.core.services.scheduler_service import create_job_run

    if manual:
        trigger_type = "manual"
    elif job.schedule_kind == "cron":
        trigger_type = "cron"
    else:
        trigger_type = job.schedule_kind or "scheduled"

    # Record the run
    run = await create_job_run(
        db, job.job_id, status="running",
        trigger_type=trigger_type,
        started_at=now,
    )
    run_id = run.id

    # Update job state
    job.last_run_at = now
    job.last_status = "running"
    job.consecutive_errors = 0
    await db.flush()

    async def _mark_run_error(reason: str) -> None:
        """Surface a misconfiguration as a failed run instead of leaving
        the row stuck in 'running' forever."""
        run.status = "error"
        run.error = reason
        run.completed_at = datetime.now(timezone.utc)
        if run.started_at:
            run.duration_ms = (run.completed_at - run.started_at).total_seconds() * 1000
        job.last_status = "error"
        job.consecutive_errors = (job.consecutive_errors or 0) + 1
        await db.flush()

    async def _mark_run_skipped(reason: str) -> None:
        """Skip workspace-scoped automation when the workspace is offline."""
        completed_at = datetime.now(timezone.utc)
        run.status = "skipped"
        run.result = {"skipped": True, "reason": reason}
        run.completed_at = completed_at
        if run.started_at:
            run.duration_ms = (completed_at - run.started_at).total_seconds() * 1000
        job.last_status = "skipped"
        await db.flush()

    target = job.execution_target or {}
    workspace_id = target.get("workspace_id") or job.workspace_id
    if workspace_id:
        from sqlalchemy import select
        from packages.core.models.workspace import Workspace

        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is None:
            await _mark_run_skipped("workspace_not_found")
            return
        if workspace.status != "active":
            await _mark_run_skipped(f"workspace_{workspace.status}")
            return

    prompt = runtime_scheduled_job_prompt(
        execution_script=job.execution_script,
        payload_message=job.payload_message,
        name=job.name,
    ).prompt

    # ── Dispatch based on execution_type ──
    exec_type = job.execution_type or "agent"

    if exec_type == "workflow":
        # Linked to a WorkflowDefinition — most deterministic
        # Create a new WorkflowRun and dispatch
        workflow_id = (job.execution_target or {}).get("workflow_id") or job.goal_id
        if not workflow_id:
            await _mark_run_error(
                "exec_type='workflow' but no workflow_id in execution_target"
            )
            return
        from sqlalchemy import select
        from packages.core.models.workflow import WorkflowRun as WRun
        from packages.core.models.base import generate_ulid as _ulid
        trigger_data = dict(target)
        trigger_data.update({
            "payload_message": prompt,
            "scheduled_job_id": job.job_id,
        })
        if workspace_id:
            trigger_data["workspace_id"] = workspace_id
        if job.conversation_id:
            trigger_data.setdefault("conversation_id", job.conversation_id)
        if job.manor_task_id:
            trigger_data.setdefault("task_id", job.manor_task_id)
        wrun = WRun(
            id=_ulid(), workflow_id=workflow_id,
            entity_id=job.entity_id or "",
            status="pending",
            variables={"payload_message": prompt},
            trigger_data=trigger_data,
            started_by=job.user_id,
        )
        db.add(wrun)
        await db.flush()
        from packages.core.tasks.ai_tasks import run_workflow
        run_workflow.delay(wrun.id)
        logger.info("Dispatched workflow run %s for job %s", wrun.id, job.job_id)

    elif exec_type == "skill":
        # Linked to a Skill — frozen prompt+tool chain
        # Load skill, create task with skill's system_prompt as the procedure
        skill_id = (job.execution_target or {}).get("skill_id")
        if not skill_id:
            await _mark_run_error(
                "exec_type='skill' but no skill_id in execution_target"
            )
            return
        from sqlalchemy import select
        from packages.core.models.skill import Skill
        result = await db.execute(select(Skill).where(Skill.id == skill_id))
        skill = result.scalar_one_or_none()
        if not skill:
            await _mark_run_error(f"Skill {skill_id} not found")
            return
        full_prompt = runtime_scheduled_skill_prompt(
            skill_system_prompt=skill.system_prompt,
            input_prompt=prompt,
        )
        await _dispatch_agent_task(db, job, now, full_prompt, run_id)

    elif exec_type == "agent" or exec_type == "orchestrator_prompt":
        # Free-form AI agent execution
        if job.agent_id:
            await _dispatch_agent_task(db, job, now, prompt, run_id)
        elif prompt:
            # No agent — create a manual task
            from packages.core.services.task_service import create_task
            await create_task(
                db, job.entity_id or "",
                title=f"[Auto] {job.name or 'Scheduled Task'}",
                description=prompt, task_type="ai_generated",
                details={"scheduled_job_id": job.job_id},
            )
            await db.flush()
        else:
            await _mark_run_error(
                "exec_type='agent' but no agent_id and no payload_message — "
                "nothing to dispatch"
            )
            return

    elif exec_type == "goal_measurement":
        target = job.execution_target or {}
        goal_id = target.get("goal_id") or job.goal_id
        if not goal_id:
            await _mark_run_error(
                "exec_type='goal_measurement' but no goal_id in "
                "execution_target or job.goal_id"
            )
            return
        from sqlalchemy import select
        from packages.core.goals.scheduling import (
            measurement_schedule_skip_reason,
            should_install_measurement_schedule,
        )
        from packages.core.models.goal import Goal
        goal = (await db.execute(select(Goal).where(Goal.id == goal_id))).scalar_one_or_none()
        if goal is None:
            await _mark_run_skipped("goal_not_found")
            job.enabled = False
            return
        if not should_install_measurement_schedule(goal):
            await _mark_run_skipped(measurement_schedule_skip_reason(goal))
            job.enabled = False
            return
        from packages.core.tasks.ai_tasks import run_goal_measurement
        run_goal_measurement.apply_async(
            args=[goal_id],
            kwargs={"run_id": run_id, "job_id_str": job.job_id},
        )

    elif exec_type in ("strategist_review", "briefing", "outcome_evaluation", "chat_insight_extraction"):
        # All four workspace-scoped scheduled tasks share the same
        # dispatch shape: extract workspace_id, validate, fan out to the
        # matching Celery task with run_id + job_id_str so the task can
        # finalise the row on completion (otherwise it'd stay 'running'
        # forever in the DB).
        if not workspace_id:
            await _mark_run_error(
                f"exec_type={exec_type!r} but no workspace_id in "
                "execution_target or job.workspace_id"
            )
            return
        from packages.core.tasks.ai_tasks import (
            run_strategist_review, run_morning_briefing,
            run_outcome_evaluation, run_chat_insight_extraction,
        )
        finalise_kwargs = {"run_id": run_id, "job_id_str": job.job_id}
        if exec_type == "strategist_review":
            run_strategist_review.apply_async(
                args=[workspace_id, "scheduled"], kwargs=finalise_kwargs,
            )
        elif exec_type == "briefing":
            briefing_timezone = target.get("timezone") or job.timezone
            run_morning_briefing.apply_async(
                args=[workspace_id],
                kwargs={**finalise_kwargs, "timezone_name": briefing_timezone},
            )
        elif exec_type == "outcome_evaluation":
            run_outcome_evaluation.apply_async(
                args=[workspace_id], kwargs=finalise_kwargs,
            )
        else:  # chat_insight_extraction
            run_chat_insight_extraction.apply_async(
                args=[workspace_id], kwargs=finalise_kwargs,
            )

    elif exec_type == "goal" and job.goal_id:
        # Legacy "goal" exec_type predates the goal-driven runtime
        # rewrite. The old GoalRunner that consumed it is gone — surface
        # this as a config error so the operator migrates the row.
        await _mark_run_error(
            f"Legacy exec_type='goal' (goal_id={job.goal_id}) is no longer "
            "supported. Migrate to 'goal_measurement' or delete this job."
        )
        return

    else:
        await _mark_run_error(f"Unknown exec_type={exec_type!r}")
        return

    # Handle delete_after_run (one-shot jobs)
    if job.delete_after_run:
        job.enabled = False

    logger.info("Dispatched job %s (type=%s, task=%s)", job.job_id, exec_type, job.manor_task_id)


async def _dispatch_agent_task(db, job, now, prompt: str, run_id: str | None = None):
    """Create or reset a task and dispatch the agent."""
    from packages.core.tasks.ai_tasks import run_agent_task

    task_id = job.manor_task_id

    target = job.execution_target or {}
    done_when = str(
        target.get("done_when")
        or target.get("completion_criteria")
        or ""
    ).strip()
    if not done_when:
        done_when = (
            "The scheduled automation has completed the requested work and "
            "reported the concrete result. If the prompt requests a side "
            "effect such as sending email, that side effect must have been "
            "attempted through an available tool and the final answer must "
            "state whether it succeeded."
        )
    deliverable = str(target.get("deliverable") or "").strip()
    if not deliverable:
        deliverable = (
            "A concise final report with the queried results, completion "
            "times when relevant, and any delivery/send status."
        )
    done_when, deliverable = _tighten_file_deliverable_completion(
        target=target,
        done_when=done_when,
        deliverable=deliverable,
    )
    max_turns = _agent_task_max_turns_for_target(target)

    details_base = {
        "scheduled_job_id": job.job_id,
        "scheduled_run_id": run_id,
        "execution_script": job.execution_script or None,
        "payload_message": job.payload_message or None,
        "default_delivery_mode": job.default_delivery_mode or None,
        "conversation_id": job.conversation_id or None,
        "done_when": done_when,
        "deliverable": deliverable,
        "max_turns": max_turns,
        "model_role": target.get("complexity", "primary"),
    }

    if not task_id:
        from packages.core.services.task_service import create_task
        task = await create_task(
            db, job.entity_id or "",
            title=f"[Auto] {job.name or 'Scheduled Task'}",
            description=prompt, task_type="ai_generated",
            workspace_id=job.workspace_id,
            creator_id=job.user_id,
            agent_id=job.agent_id,
            details=details_base,
            conversation_id=job.conversation_id,
        )
        await db.flush()
        task_id = task.id
        job.manor_task_id = task_id
        await db.flush()
        logger.info("Created task %s for job %s", task_id, job.job_id)
    else:
        from packages.core.services.task_service import update_task
        await update_task(db, task_id, job.entity_id or "",
            status="pending", description=prompt,
            workspace_id=job.workspace_id,
            creator_id=job.user_id,
            conversation_id=job.conversation_id,
            details={**details_base, "run_at": now.isoformat()},
        )
        await db.flush()

    run_agent_task.delay(task_id, job.agent_id)
