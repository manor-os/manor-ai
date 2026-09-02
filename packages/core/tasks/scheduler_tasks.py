"""Celery tasks for the scheduled job system.

The main tick task runs every 60 seconds (via Celery Beat) and checks
the scheduled_jobs table for due jobs. When a job is due, it dispatches
the appropriate execution task.
"""
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.celery_app import celery_app
from packages.core.cron import cron_field_matches
from packages.core.constants.execution import (
    DEFAULT_AGENT_MAX_TURNS,
    SCHEDULED_DISPATCH_RECOVERY_DELAY_SECONDS,
    SCHEDULED_DISPATCH_RECOVERY_MAX_RETRIES,
    SCHEDULED_EXECUTION_RECOVERY_HEADER,
    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS,
    SCHEDULED_RECOVERY_RETRY_SECONDS,
    ScheduledChildExecutionState,
    ScheduledDispatchKind,
    ScheduledDispatchRecoveryAction,
    ScheduledDispatchState,
    ScheduledRecoveryKind,
    SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS,
    SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS,
    SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
    SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
    ScheduledResultProjectionState,
    ScheduledRunStatus,
)
from packages.core.ai.llm_client import CreditExhaustedError
from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    workspace_autonomy_skip_reason,
    workspace_autonomy_state,
)
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


INTERVAL_SCHEDULE_KINDS = ("every", "interval")

# Missed-run scan (M1 ledger gap): only consider interval jobs of >= 1 hour —
# sub-hourly jobs recover on the very next tick, so "missed" events for them
# would be pure noise.
_MISSED_RUN_MIN_EVERY_SECONDS = 3600
# Cron missed-run detection: how far back _previous_cron_occurrence will
# step (minute by minute) looking for the last matching minute, and how
# long after that occurrence we still consider a run merely "late" rather
# than missed (the tick itself runs every 60s, dispatch is async).
_CRON_LOOKBACK_MINUTES = 1440  # 24h
_MISSED_CRON_GRACE_SECONDS = 300  # 5 min
_DISPATCH_TERMINAL_PERSISTENCE_RETRIES = 10
_LEGACY_CLOCK_PENDING_MAX_RETRIES = 20
_LEGACY_CLOCK_PENDING_RETRY_SECONDS = 30
_PREPARED_DISPATCH_RECOVERY_AGE = timedelta(minutes=5)
_PREPARED_DISPATCH_RECOVERY_BATCH = 50
_TICK_CANDIDATE_PAGE_SIZE = 500
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
_SCHEDULED_OCCURRENCE_PREFIX = "scheduled:v2:"

_SCHEDULED_JOB_BILLING_SOURCE = "scheduled_job"
_SCHEDULED_JOB_MODEL_ROLES = {
    "agent": "primary",
    "orchestrator_prompt": "primary",
    "skill": "primary",
    "workflow": "workflow_runner",
    "briefing": "briefing",
    "chat_insight_extraction": "chat_insight_extraction",
    "strategist_review": "strategist",
}


class PreparedScheduledDispatch(TypedDict):
    """Serializable queue handoff persisted on ``ScheduledJobRun.result``."""

    kind: str
    args: list[Any]
    kwargs: dict[str, Any]


class ScheduledDispatchLedgerContext(TypedDict):
    """Immutable ledger attribution captured when a handoff is prepared."""

    occurred_at: str
    revision: int | None
    experiment_id: str | None


@dataclass(frozen=True)
class ScheduledOccurrenceDispatch:
    run_id: str
    published: bool


class PreparedScheduledDispatchRecovery(TypedDict):
    """Factory result describing the only safe recovery for one handoff."""

    action: str
    dispatch: PreparedScheduledDispatch


class _ScheduledOccurrenceClockPending(RuntimeError):
    """A legacy Beat message arrived before its dispatch clock committed."""


_DURABLE_CHILD_EXECUTION_RECOVERY_KINDS = frozenset(
    {
        ScheduledDispatchKind.AGENT_TASK,
        ScheduledDispatchKind.WORKFLOW,
    }
)


async def runtime_assert_credit_available(*args, **kwargs):
    """Lazy Runtime import so scheduler module loading stays lightweight."""
    from packages.core.ai.runtime import runtime_assert_credit_available as _assert

    return await _assert(*args, **kwargs)


def _scheduled_job_model_role(job) -> str:
    """Resolve the model role used by a scheduled job before dispatch."""
    target = getattr(job, "execution_target", None) or {}
    if (getattr(job, "execution_type", None) or "agent") in {
        "agent", "orchestrator_prompt", "skill",
    }:
        complexity = str(target.get("complexity") or "").strip().lower()
        if complexity in {"primary", "worker"}:
            return complexity
    return _SCHEDULED_JOB_MODEL_ROLES.get(
        getattr(job, "execution_type", None) or "agent",
        "primary",
    )


def _scheduled_job_requires_credit_gate(job) -> bool:
    """Return whether dispatch should stop when tenant credits are exhausted."""
    return (getattr(job, "execution_type", None) or "agent") in {
        "agent",
        "orchestrator_prompt",
        "skill",
        "workflow",
        "briefing",
        "strategist_review",
        "chat_insight_extraction",
    }


async def _preflight_scheduled_job_credits(
    job,
    *,
    db=None,
    workspace_id: str | None = None,
) -> None:
    """Block a scheduled AI dispatch before it creates downstream work.

    The worker still performs its normal per-call preflight. This boundary
    check prevents exhausted tenants from creating workflow runs, resetting
    tasks, or enqueueing Celery retries that can never make progress.
    """
    entity_id = str(getattr(job, "entity_id", None) or "").strip()
    if not entity_id or not _scheduled_job_requires_credit_gate(job):
        return

    user_id = getattr(job, "user_id", None)
    byok = False
    try:
        from packages.core.ai.llm_client import metadata_has_native_byok
        from packages.core.services.model_resolver import resolve_llm_metadata_for_user

        metadata = await resolve_llm_metadata_for_user(
            _scheduled_job_model_role(job),
            user_id=user_id,
            entity_id=entity_id,
            db=db,
        )
        byok = metadata_has_native_byok(metadata)
    except Exception:
        # The credit gate itself fails closed. If metadata resolution is
        # unavailable, leave byok=False so a cloud tenant cannot bypass it.
        logger.debug("Unable to resolve scheduled-job BYOK metadata", exc_info=True)

    await runtime_assert_credit_available(
        entity_id,
        source=_SCHEDULED_JOB_BILLING_SOURCE,
        user_id=user_id,
        workspace_id=workspace_id or getattr(job, "workspace_id", None),
        byok=byok,
        session_factory=_scheduled_job_credit_session_factory(),
    )


def _scheduled_job_credit_session_factory():
    """Return a disposable DB boundary for the scheduler credit preflight."""
    from packages.core.database import create_worker_session

    return create_worker_session()


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
    1. Query the indexed durable ``next_run_at`` clock in bounded pages
    2. Claim exact occurrences transactionally
    3. Dispatch each due job as a separate Celery task (fan-out)
    4. Recover committed-but-unpublished handoffs from durable run state

    This handles 100K+ jobs because:
    - DB does the heavy filtering (index on enabled + next_run_at)
    - Each transaction locks only a bounded candidate page
    - Actual execution is fanned out across workers
    """
    _run_async(_async_tick())


async def _claim_due_scheduled_job_skill_generations(
    *,
    limit: int = 100,
) -> list[tuple[str, str, str, int]]:
    """Lease a bounded batch of durable revision-scoped generation intents."""

    from sqlalchemy import or_, select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob

    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(
        seconds=SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
    )
    async with create_worker_session()() as db:
        jobs = list((await db.execute(
            select(ScheduledJob)
            .where(
                ScheduledJob.enabled.is_(True),
                ScheduledJob.skill_generation_revision.is_not(None),
                or_(
                    ScheduledJob.skill_generation_next_attempt_at.is_(None),
                    ScheduledJob.skill_generation_next_attempt_at <= now,
                ),
            )
            .order_by(
                ScheduledJob.skill_generation_next_attempt_at.asc(),
                ScheduledJob.id.asc(),
            )
            .limit(max(1, int(limit)))
            .with_for_update(skip_locked=True)
        )).scalars().all())
        claimed: list[tuple[str, str, str, int]] = []
        for job in jobs:
            revision = int(job.skill_generation_revision or 0)
            if (
                revision <= 0
                or revision != int(job.revision or 1)
                or not job.payload_message
                or not job.agent_id
            ):
                job.skill_generation_revision = None
                job.skill_generation_next_attempt_at = None
                continue
            job.skill_generation_next_attempt_at = retry_at
            claimed.append(
                (
                    job.id,
                    str(job.payload_message),
                    str(job.name or ""),
                    revision,
                )
            )
        await db.commit()
        return claimed


async def _retry_scheduled_job_skill_generation_soon(
    job_id: str,
    revision: int,
) -> None:
    """Release a failed broker handoff without touching a newer intent."""

    from packages.core.database import create_worker_session
    from packages.core.services.scheduler_service import (
        defer_scheduled_job_skill_generation,
    )

    async with create_worker_session()() as db:
        await defer_scheduled_job_skill_generation(
            db,
            job_id=job_id,
            revision=revision,
            next_attempt_at=datetime.now(timezone.utc)
            + timedelta(seconds=SCHEDULED_RECOVERY_RETRY_SECONDS),
        )
        await db.commit()


@celery_app.task(name="scheduler.skill_generation_sweep")
def scheduled_job_skill_generation_sweep():
    """Publish durable Skill-generation intents after API/broker failures."""

    from packages.core.tasks.ai_tasks import generate_job_skill

    claimed = _run_async(_claim_due_scheduled_job_skill_generations())
    published = 0
    for job_id, payload_message, job_name, revision in claimed:
        try:
            generate_job_skill.apply_async(
                args=[job_id, payload_message, job_name, revision]
            )
            published += 1
        except Exception:
            logger.exception(
                "Failed to publish scheduled Skill generation job=%s "
                "revision=%s",
                job_id,
                revision,
            )
            _run_async(
                _retry_scheduled_job_skill_generation_soon(job_id, revision)
            )
    return {"claimed": len(claimed), "published": published}


async def _async_tick():
    from packages.core.database import create_worker_session
    from sqlalchemy import and_, or_, select
    from packages.core.models.scheduler import ScheduledJob

    now = datetime.now(timezone.utc)

    async with create_worker_session()() as db:
        # Recovery owns independent commit/rollback boundaries. Run it before
        # appending missed-run facts so one disappearing recovery candidate
        # cannot roll back unrelated ledger evidence.
        recovery_handled = await _queue_stale_prepared_dispatch_recovery(db, now)
        missed = await _maybe_scan_missed_runs(db, now)

        # Commit missed-run facts before dispatch paging. Candidate reads never
        # lock or skip rows: each id is reloaded and revalidated under its own
        # row lock by ``_prepare_scheduler_parent_dispatch``. Consequently a
        # config writer already holding the row lock is waited out instead of
        # silently dropping the only matching cron minute.
        await db.commit()
        # Keyset paging bounds scheduler memory and DB transaction duration
        # without imposing a per-tick cap that would starve cron jobs beyond
        # the first page. Broker I/O happens only after the per-job claim has
        # committed, so it holds no database lock.
        dispatched = 0
        candidate_count = 0
        cursor: tuple[datetime, str] | None = None
        while True:
            query = select(ScheduledJob).where(
                ScheduledJob.enabled == True,  # noqa: E712
                ScheduledJob.next_run_at.is_not(None),
                ScheduledJob.next_run_at <= now,
            )
            if cursor is not None:
                cursor_next_run_at, cursor_id = cursor
                query = query.where(
                    or_(
                        ScheduledJob.next_run_at > cursor_next_run_at,
                        and_(
                            ScheduledJob.next_run_at == cursor_next_run_at,
                            ScheduledJob.id > cursor_id,
                        ),
                    )
                )
            candidates = list((await db.execute(
                query.order_by(
                    ScheduledJob.next_run_at.asc(),
                    ScheduledJob.id.asc(),
                ).limit(_TICK_CANDIDATE_PAGE_SIZE)
            )).scalars().all())
            if not candidates:
                break
            last_candidate = candidates[-1]
            cursor = (
                _as_aware_utc(
                    getattr(last_candidate, "next_run_at", None) or now
                ),
                last_candidate.id,
            )
            due_job_ids = [job.id for job in candidates]
            await db.commit()
            candidate_count += len(due_job_ids)

            for job_db_id in due_job_ids:
                prepared = await _prepare_scheduler_parent_dispatch(job_db_id, now)
                if prepared is None:
                    continue
                dispatch, run_id = prepared
                try:
                    _publish_scheduled_dispatch(dispatch)
                    await _mark_scheduled_dispatch_published(run_id, dispatch)
                    dispatched += 1
                except Exception:
                    logger.exception(
                        "Failed to publish durable scheduler parent job=%s run=%s",
                        job_db_id,
                        run_id,
                    )

        if dispatched or missed or recovery_handled:
            logger.info(
                "Scheduler tick: dispatched %d of %d due candidates",
                dispatched,
                candidate_count,
            )


async def _prepare_scheduler_parent_dispatch(
    job_db_id: str,
    now: datetime,
    *,
    occurrence_key: str | None = None,
) -> tuple[PreparedScheduledDispatch, str] | None:
    """Commit one exact tick-to-dispatcher handoff before broker publish."""

    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )
    from packages.core.services.scheduler_service import claim_job_run

    async with create_worker_session()() as db:
        entity_id = (await db.execute(
            select(ScheduledJob.entity_id).where(ScheduledJob.id == job_db_id)
        )).scalar_one_or_none()
        if entity_id is None:
            await db.rollback()
            return None
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
        job = (await db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job_db_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if job is None or not job.enabled or not _is_due(job, now):
            await db.rollback()
            return None
        current_occurrence_key = _scheduled_occurrence_key(job, now)
        if occurrence_key is not None and occurrence_key != current_occurrence_key:
            await db.rollback()
            return None
        occurrence_key = current_occurrence_key

        trigger_type = (
            "cron"
            if job.schedule_kind == "cron"
            else job.schedule_kind or "scheduled"
        )
        run, claimed = await claim_job_run(
            db,
            job.job_id,
            status=ScheduledRunStatus.RUNNING.value,
            idempotency_key=occurrence_key,
            trigger_type=trigger_type,
            started_at=now,
        )
        if not claimed:
            parent_dispatch = _scheduler_parent_dispatch_from_run(
                run,
                job_db_id=getattr(job, "id", None),
                now=now,
                occurrence_key=occurrence_key,
            )
            if parent_dispatch is None:
                await db.rollback()
                return None
            await db.rollback()
            return parent_dispatch, run.id

        dispatch = _prepared_scheduled_dispatch(
            ScheduledDispatchKind.SCHEDULER_PARENT,
            args=[job.id, now.isoformat()],
            kwargs={"occurrence_key": occurrence_key},
        )
        run.result = {
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch_ledger": ScheduledDispatchLedgerContext(
                occurred_at=now.isoformat(),
                revision=getattr(job, "revision", None),
                experiment_id=None,
            ),
        }
        job.last_run_at = now
        job.last_status = "dispatched"
        from packages.core.schedule_clock import refresh_scheduled_job_next_run_at

        refresh_scheduled_job_next_run_at(
            job,
            now=now,
            inclusive=False,
        )
        await db.commit()
        return dispatch, run.id


async def dispatch_job_occurrence(
    *,
    job_db_id: str,
    occurrence_key: str,
    trigger_type: str = "manual",
    trigger_detail: str | None = None,
) -> ScheduledOccurrenceDispatch | None:
    """Commit and publish one explicit idempotent scheduler occurrence."""

    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )
    from packages.core.services.scheduler_service import claim_job_run

    clean_key = str(occurrence_key or "").strip()
    if not clean_key:
        raise ValueError("occurrence_key is required")
    now = datetime.now(timezone.utc)
    dispatch: PreparedScheduledDispatch | None = None
    run_id = ""
    async with create_worker_session()() as db:
        entity_id = (await db.execute(
            select(ScheduledJob.entity_id).where(ScheduledJob.id == job_db_id)
        )).scalar_one_or_none()
        if entity_id is None:
            await db.rollback()
            return None
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
        job = (await db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job_db_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if job is None or not job.enabled:
            await db.rollback()
            return None

        run, claimed = await claim_job_run(
            db,
            job.job_id,
            status=ScheduledRunStatus.RUNNING.value,
            idempotency_key=clean_key,
            trigger_type=str(trigger_type or "manual")[:20],
            started_at=now,
        )
        run_id = run.id
        if not claimed:
            result = run.result if isinstance(run.result, dict) else {}
            if result.get("dispatch_status") != ScheduledDispatchState.PREPARED.value:
                await db.rollback()
                return ScheduledOccurrenceDispatch(run_id=run_id, published=False)
            dispatch = _scheduler_parent_dispatch_from_run(run)
            await db.rollback()
            if dispatch is None:
                return ScheduledOccurrenceDispatch(run_id=run_id, published=False)
        else:
            dispatch = _prepared_scheduled_dispatch(
                ScheduledDispatchKind.SCHEDULER_PARENT,
                args=[job.id, now.isoformat()],
                kwargs={
                    "manual": True,
                    "occurrence_key": clean_key,
                },
            )
            run.result = {
                "dispatch": dispatch,
                "dispatch_status": ScheduledDispatchState.PREPARED.value,
                "dispatch_ledger": ScheduledDispatchLedgerContext(
                    occurred_at=now.isoformat(),
                    revision=getattr(job, "revision", None),
                    experiment_id=None,
                ),
                "explicit_trigger": {
                    "type": str(trigger_type or "manual")[:80],
                    "detail": str(trigger_detail or "")[:200],
                },
            }
            await db.commit()

    try:
        _publish_scheduled_dispatch(dispatch)
    except Exception:
        logger.exception(
            "Failed to publish explicit scheduled occurrence job=%s run=%s key=%s",
            job_db_id,
            run_id,
            clean_key,
        )
        return ScheduledOccurrenceDispatch(run_id=run_id, published=False)

    try:
        await _mark_scheduled_dispatch_published(run_id, dispatch)
    except Exception:
        logger.exception(
            "Failed to project explicit scheduled occurrence publication "
            "job=%s run=%s key=%s",
            job_db_id,
            run_id,
            clean_key,
        )
    return ScheduledOccurrenceDispatch(run_id=run_id, published=True)


async def _mark_scheduled_job_dispatched_if_current(
    db,
    candidate,
    now: datetime,
) -> bool:
    """Claim one due clock without overwriting a concurrent reschedule."""

    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJob

    expected_occurrence_key = _scheduled_occurrence_key(candidate, now)
    current = (await db.execute(
        select(ScheduledJob)
        .where(ScheduledJob.id == candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        current is None
        or not current.enabled
        or _scheduled_occurrence_key(current, now) != expected_occurrence_key
        or not _is_due(current, now)
    ):
        return False
    current.last_run_at = now
    current.last_status = "dispatched"
    from packages.core.schedule_clock import refresh_scheduled_job_next_run_at

    refresh_scheduled_job_next_run_at(
        current,
        now=now,
        inclusive=False,
    )
    await db.flush()
    return True


async def _queue_stale_prepared_dispatch_recovery(db, now: datetime) -> int:
    """Process one fair, bounded batch of due durable recovery handoffs."""
    from sqlalchemy import Numeric, and_, case, cast, or_, select

    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        RECOVERY_EPOCH_PATTERN,
        RECOVERY_MAX_EPOCH_SECONDS,
        RESULT_PROJECTION_KEY,
        scheduled_recovery_next_attempt_at_key,
    )

    cutoff = now - _PREPARED_DISPATCH_RECOVERY_AGE
    execution_cutoff = now - timedelta(
        seconds=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
    )
    now_epoch = now.astimezone(timezone.utc).timestamp()

    def recovery_due(kind: ScheduledRecoveryKind):
        next_attempt_at = ScheduledJobRun.result[
            scheduled_recovery_next_attempt_at_key(kind)
        ].astext
        numeric_deadline = case(
            (
                next_attempt_at.op("~")(RECOVERY_EPOCH_PATTERN),
                cast(next_attempt_at, Numeric),
            ),
            else_=None,
        )
        return or_(
            next_attempt_at.is_(None),
            next_attempt_at.op("!~")(RECOVERY_EPOCH_PATTERN),
            numeric_deadline > RECOVERY_MAX_EPOCH_SECONDS,
            numeric_deadline <= now_epoch,
        )

    rows = list((await db.execute(
        select(ScheduledJobRun)
        .where(
            ScheduledJobRun.created_at <= cutoff,
            or_(
                and_(
                    ScheduledJobRun.result["dispatch_status"].astext
                    == ScheduledDispatchState.PREPARED.value,
                    or_(
                        ScheduledJobRun.result[
                            "scheduled_execution_state"
                        ].astext.is_(None),
                        ScheduledJobRun.result[
                            "scheduled_execution_state"
                        ].astext
                        != ScheduledChildExecutionState.SETTLEMENT_PENDING.value,
                    ),
                    recovery_due(ScheduledRecoveryKind.DISPATCH),
                ),
                and_(
                    ScheduledJobRun.result["scheduled_execution_state"].astext
                    == ScheduledChildExecutionState.SETTLEMENT_PENDING.value,
                    recovery_due(ScheduledRecoveryKind.SETTLEMENT),
                ),
                and_(
                    ScheduledJobRun.status == ScheduledRunStatus.RUNNING.value,
                    ScheduledJobRun.created_at <= execution_cutoff,
                    ScheduledJobRun.result["dispatch_status"].astext
                    == ScheduledDispatchState.PUBLISHED.value,
                    or_(
                        ScheduledJobRun.result[
                            "scheduled_execution_state"
                        ].astext.is_(None),
                        ScheduledJobRun.result[
                            "scheduled_execution_state"
                        ].astext
                        == ScheduledChildExecutionState.ADMITTED.value,
                    ),
                    recovery_due(ScheduledRecoveryKind.EXECUTION),
                ),
                and_(
                    ScheduledJobRun.result.has_key(  # type: ignore[attr-defined]  # noqa: W601
                        RESULT_PROJECTION_KEY
                    ),
                    or_(
                        ScheduledJobRun.result[RESULT_PROJECTION_KEY]["state"]
                        .astext.is_(None),
                        ScheduledJobRun.result[RESULT_PROJECTION_KEY]["state"]
                        .astext.notin_(
                            {
                                ScheduledResultProjectionState.DELIVERED.value,
                                ScheduledResultProjectionState.QUARANTINED.value,
                            }
                        ),
                    ),
                    recovery_due(ScheduledRecoveryKind.RESULT_PROJECTION),
                ),
            ),
        )
        .order_by(
            ScheduledJobRun.created_at.asc(),
            ScheduledJobRun.id.asc(),
        )
        .limit(_PREPARED_DISPATCH_RECOVERY_BATCH)
    )).scalars().all())
    handled = 0
    for candidate in rows:
        from packages.core.services.scheduled_run_lifecycle import (
            RESULT_PROJECTION_KEY,
            defer_scheduled_recovery,
            normalize_scheduled_recovery_deadline,
            scheduled_recovery_is_due,
            scheduled_result_projection,
            scheduled_run_settlement,
        )

        _job, run = await _lock_scheduled_recovery_candidate(db, candidate)
        if run is None:
            await db.rollback()
            continue
        if _job is None:
            quarantined = await _quarantine_locked_scheduled_recovery(
                db,
                None,
                run,
                reason="missing_scheduled_job",
            )
            await db.commit()
            handled += int(quarantined)
            continue

        for recovery_kind in ScheduledRecoveryKind:
            normalize_scheduled_recovery_deadline(
                run,
                kind=recovery_kind,
            )

        result = run.result if isinstance(run.result, dict) else {}
        projection = scheduled_result_projection(run)
        projection_due = scheduled_recovery_is_due(
            run,
            kind=ScheduledRecoveryKind.RESULT_PROJECTION,
            now=now,
        )
        if RESULT_PROJECTION_KEY in result and projection_due:
            if projection is None:
                quarantined = await _quarantine_locked_scheduled_recovery(
                    db,
                    _job,
                    run,
                    reason="invalid_result_projection_payload",
                )
                await db.commit()
                handled += int(quarantined)
                continue
            if projection.state is ScheduledResultProjectionState.PENDING:
                if (
                    projection.recovery_attempts
                    >= SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS
                ):
                    quarantined = await _quarantine_locked_scheduled_recovery(
                        db,
                        _job,
                        run,
                        reason="result_projection_retry_exhausted",
                    )
                    await db.commit()
                    handled += int(quarantined)
                    continue
                defer_scheduled_recovery(
                    run,
                    kind=ScheduledRecoveryKind.RESULT_PROJECTION,
                    now=now,
                    retry_after=timedelta(
                        seconds=SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS
                    ),
                )
                await db.flush()
                await db.commit()
                handled += 1
                try:
                    from packages.core.tasks.ai_tasks import (
                        _enqueue_scheduled_agent_result_projection,
                    )

                    _enqueue_scheduled_agent_result_projection(run.id)
                except Exception:
                    logger.exception(
                        "Failed to queue scheduled result projection run=%s",
                        run.id,
                    )
                continue

        settlement = scheduled_run_settlement(run)
        if (
            result.get("scheduled_execution_state")
            == ScheduledChildExecutionState.SETTLEMENT_PENDING.value
            and scheduled_recovery_is_due(
                run,
                kind=ScheduledRecoveryKind.SETTLEMENT,
                now=now,
            )
        ):
            if settlement is None:
                quarantined = await _quarantine_locked_scheduled_recovery(
                    db,
                    _job,
                    run,
                    reason="invalid_settlement_payload",
                )
                await db.commit()
                handled += int(quarantined)
                continue
            defer_scheduled_recovery(
                run,
                kind=ScheduledRecoveryKind.SETTLEMENT,
                now=now,
                retry_after=timedelta(
                    seconds=SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS
                ),
            )
            await db.flush()
            await db.commit()
            handled += 1
            try:
                from packages.core.tasks.ai_tasks import (
                    _enqueue_scheduled_run_settlement,
                )

                _enqueue_scheduled_run_settlement(
                    run_id=run.id,
                    job_id_str=run.job_id,
                    settlement=settlement,
                )
            except Exception:
                logger.exception(
                    "Failed to queue scheduler settlement recovery run=%s",
                    run.id,
                )
            continue
        execution_due = scheduled_recovery_is_due(
            run,
            kind=ScheduledRecoveryKind.EXECUTION,
            now=now,
        )
        if (
            run.status == ScheduledRunStatus.RUNNING.value
            and result.get("dispatch_status")
            == ScheduledDispatchState.PUBLISHED.value
            and result.get("scheduled_execution_state")
            in {None, ScheduledChildExecutionState.ADMITTED.value}
            and execution_due
        ):
            recovery = _prepared_scheduled_dispatch_recovery(run)
            if recovery is None:
                quarantined = await _quarantine_locked_scheduled_recovery(
                    db,
                    _job,
                    run,
                    reason="invalid_execution_dispatch_payload",
                )
                await db.commit()
                handled += int(quarantined)
                continue
            if (
                recovery["action"]
                == ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value
            ):
                from packages.core.services.workflow_run_execution_claim import (
                    scheduled_run_has_live_execution_claim,
                )

                if await scheduled_run_has_live_execution_claim(db, run.id):
                    defer_scheduled_recovery(
                        run,
                        kind=ScheduledRecoveryKind.EXECUTION,
                        now=now,
                        retry_after=timedelta(
                            seconds=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
                        ),
                    )
                    await db.flush()
                    await db.commit()
                    handled += 1
                    continue
                quarantined = await _quarantine_locked_scheduled_recovery(
                    db,
                    _job,
                    run,
                    reason=ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value,
                )
                await db.commit()
                handled += int(quarantined)
                continue
            defer_scheduled_recovery(
                run,
                kind=ScheduledRecoveryKind.EXECUTION,
                now=now,
                retry_after=timedelta(
                    seconds=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
                ),
            )
            await db.flush()
            await db.commit()
            handled += 1
            try:
                _recover_prepared_scheduled_dispatch_task.delay(run.id)
            except Exception:
                logger.exception(
                    "Failed to queue published scheduler execution "
                    "recovery run=%s",
                    run.id,
                )
            continue
        dispatch_due = scheduled_recovery_is_due(
            run,
            kind=ScheduledRecoveryKind.DISPATCH,
            now=now,
        )
        recovery = (
            _prepared_scheduled_dispatch_recovery(run)
            if dispatch_due
            else None
        )
        if recovery is None:
            if (
                dispatch_due
                and result.get("dispatch_status")
                == ScheduledDispatchState.PREPARED.value
                and result.get("scheduled_execution_state")
                != ScheduledChildExecutionState.SETTLEMENT_PENDING.value
            ):
                quarantined = await _quarantine_locked_scheduled_recovery(
                    db,
                    _job,
                    run,
                    reason="invalid_dispatch_payload",
                )
                handled += int(quarantined)
            await db.commit()
            continue
        defer_scheduled_recovery(
            run,
            kind=ScheduledRecoveryKind.DISPATCH,
            now=now,
            retry_after=timedelta(
                seconds=SCHEDULED_DISPATCH_RECOVERY_DELAY_SECONDS
            ),
        )
        await db.flush()
        await db.commit()
        handled += 1
        try:
            _recover_prepared_scheduled_dispatch_task.delay(run.id)
        except Exception:
            logger.exception(
                "Failed to queue prepared scheduler dispatch recovery run=%s",
                run.id,
            )
    return handled


async def _lock_scheduled_recovery_candidate(db, candidate):
    """Reload one recovery candidate under the scheduler lock hierarchy."""
    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduler_service import lock_scheduled_job_and_run

    job, run = await lock_scheduled_job_and_run(
        db,
        job_id=getattr(candidate, "job_id", None),
        run_id=getattr(candidate, "id", None),
    )
    if run is not None:
        return job, run

    # Legacy/corrupt rows may outlive their parent definition. There is no
    # parent lock to take in that case, but the orphan must still leave the
    # fixed oldest-first recovery batch instead of starving valid work.
    run = (await db.execute(
        select(ScheduledJobRun)
        .where(
            ScheduledJobRun.id == getattr(candidate, "id", None),
            ScheduledJobRun.job_id == getattr(candidate, "job_id", None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return None, run


def _scheduled_recovery_reason_applies(run, reason: str) -> bool:
    """Revalidate a quarantine decision against the locked current row."""
    from packages.core.services.scheduled_run_lifecycle import (
        RESULT_PROJECTION_KEY,
        scheduled_child_execution_state,
        scheduled_result_projection,
        scheduled_run_settlement,
    )

    result = run.result if isinstance(run.result, dict) else {}
    if reason == "invalid_result_projection_payload":
        return (
            RESULT_PROJECTION_KEY in result
            and scheduled_result_projection(run) is None
        )
    if reason == "result_projection_retry_exhausted":
        projection = scheduled_result_projection(run)
        return (
            projection is not None
            and projection.state is ScheduledResultProjectionState.PENDING
            and projection.recovery_attempts
            >= SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS
        )
    if reason == "invalid_settlement_payload":
        return (
            scheduled_child_execution_state(run)
            is ScheduledChildExecutionState.SETTLEMENT_PENDING
            and scheduled_run_settlement(run) is None
        )
    if reason == "invalid_dispatch_payload":
        return (
            result.get("dispatch_status")
            == ScheduledDispatchState.PREPARED.value
            and scheduled_child_execution_state(run)
            is not ScheduledChildExecutionState.SETTLEMENT_PENDING
            and _prepared_scheduled_dispatch_recovery(run) is None
        )
    if reason == "invalid_execution_dispatch_payload":
        return (
            getattr(run, "status", None) == ScheduledRunStatus.RUNNING.value
            and result.get("dispatch_status")
            == ScheduledDispatchState.PUBLISHED.value
            and scheduled_child_execution_state(run)
            in {None, ScheduledChildExecutionState.ADMITTED}
            and _prepared_scheduled_dispatch_recovery(run) is None
        )
    if reason == ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value:
        recovery = _prepared_scheduled_dispatch_recovery(run)
        return (
            recovery is not None
            and recovery["action"]
            == ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value
        )
    if reason == "missing_scheduled_job":
        projection = scheduled_result_projection(run)
        projection_requires_recovery = (
            RESULT_PROJECTION_KEY in result
            and (
                projection is None
                or projection.state
                not in {
                    ScheduledResultProjectionState.DELIVERED,
                    ScheduledResultProjectionState.QUARANTINED,
                }
            )
        )
        return (
            result.get("dispatch_status")
            in {
                ScheduledDispatchState.PREPARED.value,
                ScheduledDispatchState.PUBLISHED.value,
            }
            or scheduled_child_execution_state(run)
            is ScheduledChildExecutionState.SETTLEMENT_PENDING
            or projection_requires_recovery
        )
    return False


async def _quarantine_scheduled_recovery(db, candidate, *, reason: str) -> bool:
    """Remove one malformed durable handoff from the bounded recovery queue."""
    job, run = await _lock_scheduled_recovery_candidate(db, candidate)
    if run is None:
        return False
    return await _quarantine_locked_scheduled_recovery(db, job, run, reason=reason)


async def _quarantine_locked_scheduled_recovery(
    db,
    job,
    run,
    *,
    reason: str,
) -> bool:
    """Quarantine a handoff only while its locked failure still applies."""
    from packages.core.services.scheduled_run_lifecycle import (
        EXECUTION_STATE_KEY,
        RESULT_PROJECTION_KEY,
        RESULT_PROJECTION_QUARANTINE_KEY,
        SETTLEMENT_KEY,
        ScheduledRunOutcome,
        apply_scheduled_run_outcome,
        scheduled_child_execution_state,
        scheduled_result_projection,
    )
    from packages.core.services.scheduler_service import (
        notify_scheduled_job_auto_paused,
        reconcile_scheduled_job_run_projection,
    )

    if not _scheduled_recovery_reason_applies(run, reason):
        return False

    result = dict(run.result or {})
    execution_state = scheduled_child_execution_state(run)
    terminalize = (
        run.status == ScheduledRunStatus.RUNNING.value
        and (
            reason == ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value
            or reason in {"invalid_settlement_payload", "missing_scheduled_job"}
            or (
                reason in {
                    "invalid_dispatch_payload",
                    "invalid_execution_dispatch_payload",
                }
                and execution_state is not ScheduledChildExecutionState.ADMITTED
            )
        )
    )
    if terminalize:
        apply_scheduled_run_outcome(
            run,
            ScheduledRunOutcome.from_execution(error=reason),
        )
        result = dict(run.result or {})

    if (
        reason in {
            "invalid_dispatch_payload",
            "invalid_execution_dispatch_payload",
            ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value,
        }
        or (
            reason == "missing_scheduled_job"
            and result.get("dispatch_status")
            in {
                ScheduledDispatchState.PREPARED.value,
                ScheduledDispatchState.PUBLISHED.value,
            }
        )
    ):
        result["dispatch_status"] = ScheduledDispatchState.QUARANTINED.value
    if reason == "invalid_settlement_payload" or (
        reason == "missing_scheduled_job"
        and execution_state is ScheduledChildExecutionState.SETTLEMENT_PENDING
    ):
        result[EXECUTION_STATE_KEY] = ScheduledChildExecutionState.SETTLED.value
        result.pop(SETTLEMENT_KEY, None)
    projection = scheduled_result_projection(run)
    quarantine_projection = reason in {
        "invalid_result_projection_payload",
        "result_projection_retry_exhausted",
    } or (
        reason == "missing_scheduled_job"
        and RESULT_PROJECTION_KEY in result
        and (
            projection is None
            or projection.state
            not in {
                ScheduledResultProjectionState.DELIVERED,
                ScheduledResultProjectionState.QUARANTINED,
            }
        )
    )
    if quarantine_projection:
        raw_projection = result.pop(RESULT_PROJECTION_KEY, None)
        result[RESULT_PROJECTION_QUARANTINE_KEY] = {
            "state": ScheduledResultProjectionState.QUARANTINED.value,
            "error": reason,
            "payload": raw_projection,
        }
    result["scheduled_recovery_error"] = reason
    run.result = result
    await db.flush()

    if not terminalize or job is None:
        return True
    auto_paused = await reconcile_scheduled_job_run_projection(
        db,
        job,
        finalized_run_id=run.id,
    )
    if auto_paused:
        await notify_scheduled_job_auto_paused(
            db,
            job,
            failure_key=run.id,
        )
    from packages.core.ledger.adapters import record_automation_run_finished

    await record_automation_run_finished(
        db,
        job,
        run_id=run.id,
        status=ScheduledRunStatus.ERROR.value,
    )
    return True


async def _maybe_scan_missed_runs(db, now: datetime) -> int:
    """Best-effort missed-run projection over the indexed due clock."""
    now_utc = _as_aware_utc(now)
    try:
        async with db.begin_nested():
            emitted = await _scan_missed_runs(db, now_utc)
    except Exception:  # noqa: BLE001 — ledger scan must never break the tick
        logger.warning("missed-run scan failed (ignored)", exc_info=True)
        return 0
    return emitted


async def _scan_missed_runs(db, now: datetime) -> int:
    """Emit ``automation_run_missed`` for enabled jobs whose scheduled period
    elapsed with no run (the tick was down, dispatch kept failing, or the
    worker pool stalled).

    The same indexed ``next_run_at`` clock used for dispatch is the expected
    occurrence timestamp, so no per-job cron search is required here.

    Returns how many NEW missed events were recorded (per-period idempotency
    lives in the adapter's ``sj:{id}:missed:{period_key}`` key).
    """
    from sqlalchemy import select

    from packages.core.ledger.adapters import record_automation_run_missed
    from packages.core.models.scheduler import ScheduledJob

    now_utc = _as_aware_utc(now)
    threshold = now_utc - timedelta(seconds=_MISSED_RUN_MIN_EVERY_SECONDS)
    rows = (
        await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.enabled == True,  # noqa: E712
                ScheduledJob.schedule_kind.in_(INTERVAL_SCHEDULE_KINDS),
                ScheduledJob.every_seconds >= _MISSED_RUN_MIN_EVERY_SECONDS,
                ScheduledJob.next_run_at.is_not(None),
                ScheduledJob.next_run_at <= threshold,
            )
            .order_by(ScheduledJob.next_run_at.asc(), ScheduledJob.id.asc())
            .limit(_TICK_CANDIDATE_PAGE_SIZE)
        )
    ).scalars().all()

    emitted = 0
    for job in rows:
        try:
            every = float(job.every_seconds or 0)
        except (TypeError, ValueError):
            continue
        if every < _MISSED_RUN_MIN_EVERY_SECONDS:
            continue
        expected_by = _as_aware_utc(job.next_run_at)
        if (now_utc - expected_by).total_seconds() < every:
            continue  # late, but not a full missed period yet
        event = await record_automation_run_missed(db, job, expected_by=expected_by)
        if event is not None:
            emitted += 1

    emitted += await _scan_missed_cron_runs(db, now_utc)
    return emitted


async def _scan_missed_cron_runs(db, now_utc: datetime) -> int:
    """Record bounded cron misses directly from the indexed due clock."""
    from sqlalchemy import select

    from packages.core.ledger.adapters import record_automation_run_missed
    from packages.core.models.scheduler import ScheduledJob

    grace = timedelta(seconds=_MISSED_CRON_GRACE_SECONDS)
    rows = (
        await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.enabled == True,  # noqa: E712
                ScheduledJob.schedule_kind == "cron",
                ScheduledJob.next_run_at.is_not(None),
                ScheduledJob.next_run_at <= now_utc - grace,
            )
            .order_by(ScheduledJob.next_run_at.asc(), ScheduledJob.id.asc())
            .limit(_TICK_CANDIDATE_PAGE_SIZE)
        )
    ).scalars().all()

    emitted = 0
    for job in rows:
        expected_by = _as_aware_utc(job.next_run_at)
        event = await record_automation_run_missed(
            db,
            job,
            expected_by=expected_by,
        )
        if event is not None:
            emitted += 1
    return emitted


def _previous_cron_occurrence(
    cron_expr: str,
    now: datetime,
    *,
    lookback_minutes: int = _CRON_LOOKBACK_MINUTES,
) -> datetime | None:
    """Last minute at or before ``now - 1min`` that ``cron_expr`` matches.

    Deliberately naive: there is no croniter dependency, so we step
    backwards one minute at a time reusing :func:`_cron_matches` (which
    answers "does this expression fire in this minute"). That is at most
    ``lookback_minutes`` pure-python field comparisons — 1440 (24h) by
    default, which costs microseconds and runs at most once per 10-minute
    missed-scan.

    The bound is the semantic limit too: expressions that fire less often
    than daily (weekly / monthly crons) return ``None`` rather than an
    occurrence, so they are never reported as missed. Widening the window
    is a matter of raising ``lookback_minutes`` at the call site.

    Returns ``None`` for a malformed expression (not 5 fields) or when no
    minute in the window matches. ``now`` must be in the job's own
    timezone — cron fields are wall-clock.
    """
    if len(str(cron_expr or "").strip().split()) != 5:
        return None
    cursor = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    for _ in range(max(0, int(lookback_minutes))):
        if _cron_matches(cron_expr, cursor, None):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


async def _record_dispatch_exhaustion(
    *,
    job_db_id: str,
    now_iso: str,
    manual: bool,
    occurrence_key: str | None,
    error: str,
) -> None:
    """Persist a terminal occurrence when dispatch retries are exhausted."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.reusable_resource_locks import (
        reusable_resource_lifecycle_lease,
    )
    from packages.core.services.scheduler_service import (
        claim_job_run,
        notify_scheduled_job_auto_paused,
        reconcile_scheduled_job_run_projection,
    )

    started_at = datetime.fromisoformat(now_iso)
    completed_at = datetime.now(timezone.utc)
    async with create_worker_session()() as db:
        entity_id = (await db.execute(
            select(ScheduledJob.entity_id).where(ScheduledJob.id == job_db_id)
        )).scalar_one_or_none()
        if entity_id is None:
            return
        async with reusable_resource_lifecycle_lease(db, entity_id=entity_id):
            job = (await db.execute(
                select(ScheduledJob)
                .where(ScheduledJob.id == job_db_id)
                .with_for_update()
            )).scalar_one_or_none()
        if job is None:
            return
        if (
            not manual
            and occurrence_key
            and _is_scheduled_occurrence_key(job, occurrence_key)
            and not _scheduled_occurrence_is_current(job, started_at, occurrence_key)
        ):
            existing_parent = (await db.execute(
                select(ScheduledJobRun)
                .where(
                    ScheduledJobRun.job_id == job.job_id,
                    ScheduledJobRun.idempotency_key == occurrence_key,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if _scheduler_parent_dispatch_from_run(existing_parent) is None:
                await db.rollback()
                return

        resolved_occurrence_key = occurrence_key or _scheduled_occurrence_key(
            job,
            started_at,
        )
        trigger_type = (
            "manual"
            if manual
            else "cron"
            if job.schedule_kind == "cron"
            else job.schedule_kind or "scheduled"
        )
        run, claimed = await claim_job_run(
            db,
            job.job_id,
            status="error",
            idempotency_key=resolved_occurrence_key,
            trigger_type=trigger_type,
            started_at=started_at,
        )
        if not claimed:
            run = (await db.execute(
                select(ScheduledJobRun)
                .where(ScheduledJobRun.id == run.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one()
            if (
                _recover_prepared_scheduled_dispatch(run) is None
                and _scheduler_parent_dispatch_from_run(run) is None
            ):
                # A published or terminal occurrence remains authoritative.
                await db.rollback()
                return

        run.status = "error"
        run.error = str(error or "scheduler dispatch failed")[:1000]
        run.completed_at = completed_at
        run.duration_ms = max(
            0.0,
            (completed_at - _as_aware_utc(started_at)).total_seconds() * 1000,
        )

        if (
            job.last_run_at is None
            or _as_aware_utc(started_at) > _as_aware_utc(job.last_run_at)
        ):
            job.last_run_at = started_at
        await db.flush()
        auto_paused = await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        if auto_paused:
            await notify_scheduled_job_auto_paused(
                db,
                job,
                failure_key=run.id,
            )

        from packages.core.ledger.adapters import record_automation_run_finished

        await record_automation_run_finished(
            db,
            job,
            run_id=run.id,
            status="error",
        )
        await db.commit()


@celery_app.task(bind=True, name="scheduler.dispatch_job", max_retries=2)
def _dispatch_job_task(
    self,
    job_db_id: str,
    now_iso: str,
    manual: bool = False,
    occurrence_key: str | None = None,
    _terminal_failure_error: str | None = None,
    _terminal_persistence_retry_limit: int | None = None,
):
    """Execute a single scheduled job — fanned out from scheduler_tick
    or invoked directly via the run_now API.

    Each job runs as its own Celery task, so 100 due jobs = 100 parallel
    tasks across the worker pool, not a single blocking loop.
    """
    if manual and not occurrence_key:
        delivery_id = str(getattr(self.request, "id", "") or now_iso)
        occurrence_key = f"manual:{delivery_id}"
    terminal_exception: Exception | None = None
    terminal_failure_error = _terminal_failure_error
    terminal_persistence_retry_limit = _terminal_persistence_retry_limit
    if terminal_failure_error is None:
        dispatch_exception: Exception | None = None
        try:
            _run_async(
                _async_dispatch_single(
                    job_db_id,
                    now_iso,
                    manual=manual,
                    occurrence_key=occurrence_key,
                )
            )
            return
        except _ScheduledOccurrenceClockPending as exc:
            if self.request.retries < _LEGACY_CLOCK_PENDING_MAX_RETRIES:
                raise self.retry(
                    exc=exc,
                    countdown=_LEGACY_CLOCK_PENDING_RETRY_SECONDS,
                    max_retries=_LEGACY_CLOCK_PENDING_MAX_RETRIES,
                )
            logger.warning(
                "Legacy dispatch clock still pending after %s retries; "
                "revalidating and consuming the occurrence directly job=%s",
                _LEGACY_CLOCK_PENDING_MAX_RETRIES,
                job_db_id,
            )
            try:
                _run_async(
                    _async_dispatch_single(
                        job_db_id,
                        now_iso,
                        manual=manual,
                        occurrence_key=occurrence_key,
                        allow_legacy_uncommitted_clock=True,
                    )
                )
                return
            except Exception as fallback_exc:
                dispatch_exception = fallback_exc
                terminal_persistence_retry_limit = (
                    _LEGACY_CLOCK_PENDING_MAX_RETRIES
                    + _DISPATCH_TERMINAL_PERSISTENCE_RETRIES
                )
        except Exception as exc:
            dispatch_exception = exc
            max_retries = self.max_retries
            if max_retries is None or self.request.retries < max_retries:
                raise self.retry(exc=exc, countdown=30)
        if dispatch_exception is not None:
            logger.error(
                "dispatch_job %s failed: %s",
                job_db_id,
                dispatch_exception,
                exc_info=(
                    type(dispatch_exception),
                    dispatch_exception,
                    dispatch_exception.__traceback__,
                ),
            )
            terminal_exception = dispatch_exception
            terminal_failure_error = (
                str(dispatch_exception) or "scheduler dispatch failed"
            )

    try:
        _run_async(_record_dispatch_exhaustion(
            job_db_id=job_db_id,
            now_iso=now_iso,
            manual=manual,
            occurrence_key=occurrence_key,
            error=terminal_failure_error,
        ))
    except Exception as persistence_exc:
        logger.exception(
            "Failed to persist exhausted dispatch for scheduled job %s",
            job_db_id,
        )
        persistence_retry_limit = (
            terminal_persistence_retry_limit
            or (self.max_retries or 0) + _DISPATCH_TERMINAL_PERSISTENCE_RETRIES
        )
        if self.request.retries < persistence_retry_limit:
            retry_kwargs = dict(getattr(self.request, "kwargs", {}) or {})
            retry_kwargs["_terminal_failure_error"] = terminal_failure_error
            if terminal_persistence_retry_limit is not None:
                retry_kwargs["_terminal_persistence_retry_limit"] = (
                    persistence_retry_limit
                )
            raise self.retry(
                exc=persistence_exc,
                countdown=60,
                max_retries=persistence_retry_limit,
                kwargs=retry_kwargs,
            )
        raise

    if terminal_exception is not None:
        raise terminal_exception
    raise RuntimeError(terminal_failure_error)


def _prepared_scheduled_dispatch(
    kind: ScheduledDispatchKind,
    *,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> PreparedScheduledDispatch:
    return {
        "kind": kind.value,
        "args": list(args),
        "kwargs": dict(kwargs or {}),
    }


def _scheduled_dispatch_from_result(
    result: object,
) -> PreparedScheduledDispatch | None:
    """Build a validated dispatch from its durable result envelope."""

    if not isinstance(result, dict):
        return None
    dispatch = result.get("dispatch")
    if not isinstance(dispatch, dict):
        return None
    try:
        kind = ScheduledDispatchKind(dispatch.get("kind"))
    except (TypeError, ValueError):
        return None
    args = dispatch.get("args")
    kwargs = dispatch.get("kwargs")
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        return None
    return _prepared_scheduled_dispatch(kind, args=args, kwargs=kwargs)


def _scheduler_parent_dispatch_from_run(
    run,
    *,
    job_db_id: str | None = None,
    now: datetime | None = None,
    occurrence_key: str | None = None,
    require_running: bool = True,
) -> PreparedScheduledDispatch | None:
    """Return a valid tick-to-dispatcher handoff in either publish state."""

    result = getattr(run, "result", None)
    if (
        (
            require_running
            and getattr(run, "status", None)
            != ScheduledRunStatus.RUNNING.value
        )
        or not isinstance(result, dict)
        or result.get("dispatch_status") not in {
            ScheduledDispatchState.PREPARED.value,
            ScheduledDispatchState.PUBLISHED.value,
        }
    ):
        return None
    dispatch = _scheduled_dispatch_from_result(result)
    if (
        dispatch is None
        or dispatch["kind"] != ScheduledDispatchKind.SCHEDULER_PARENT.value
    ):
        return None
    args = dispatch["args"]
    kwargs = dispatch["kwargs"]
    if (
        len(args) != 2
        or not str(args[0] or "").strip()
        or not str(args[1] or "").strip()
        or not str(kwargs.get("occurrence_key") or "").strip()
    ):
        return None
    try:
        dispatch_time = datetime.fromisoformat(str(args[1]))
    except (TypeError, ValueError):
        return None
    if job_db_id is not None and str(args[0]) != str(job_db_id):
        return None
    if now is not None and _as_aware_utc(dispatch_time) != _as_aware_utc(now):
        return None
    if (
        occurrence_key is not None
        and str(kwargs.get("occurrence_key")) != occurrence_key
    ):
        return None
    return dispatch


def _recover_prepared_scheduled_dispatch(run) -> PreparedScheduledDispatch | None:
    """Return a validated, unpublished queue handoff from a claimed run."""

    recovery = _prepared_scheduled_dispatch_recovery(run)
    if (
        recovery is None
        or recovery["action"]
        != ScheduledDispatchRecoveryAction.REPUBLISH_AND_PROJECT.value
    ):
        return None
    return recovery["dispatch"]


def _prepared_scheduled_dispatch_recovery(
    run,
) -> PreparedScheduledDispatchRecovery | None:
    """Choose a recovery action without reopening terminal business work."""

    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return None
    dispatch_status = result.get("dispatch_status")
    if dispatch_status not in {
        ScheduledDispatchState.PREPARED.value,
        ScheduledDispatchState.PUBLISHED.value,
    }:
        return None
    from packages.core.services.scheduled_run_lifecycle import (
        EXECUTION_STATE_KEY,
        scheduled_child_execution_state,
    )

    if (
        scheduled_child_execution_state(run)
        is ScheduledChildExecutionState.SETTLEMENT_PENDING
    ):
        return None
    try:
        run_status = ScheduledRunStatus(str(getattr(run, "status", "") or ""))
    except ValueError:
        return None
    execution_state = scheduled_child_execution_state(run)
    dispatch = _scheduled_dispatch_from_result(result)
    if dispatch is None:
        return None
    try:
        dispatch_kind = ScheduledDispatchKind(dispatch["kind"])
    except ValueError:
        return None
    if dispatch_status == ScheduledDispatchState.PUBLISHED.value:
        if (
            run_status is not ScheduledRunStatus.RUNNING
            or execution_state
            in {
                ScheduledChildExecutionState.SETTLEMENT_PENDING,
                ScheduledChildExecutionState.SETTLED,
            }
        ):
            return None
        if (
            execution_state is ScheduledChildExecutionState.ADMITTED
            and dispatch_kind not in _DURABLE_CHILD_EXECUTION_RECOVERY_KINDS
        ):
            # Generic scheduled bodies have no child-level terminal guard.
            # Once admitted, their provider/UI effects are ambiguous after a
            # worker loss, so replaying the original envelope is unsafe.
            action = ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS
        else:
            action = ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION
    elif run_status is ScheduledRunStatus.RUNNING:
        if EXECUTION_STATE_KEY not in result:
            action = ScheduledDispatchRecoveryAction.REPUBLISH_AND_PROJECT
        elif execution_state in {
            ScheduledChildExecutionState.ADMITTED,
            ScheduledChildExecutionState.SETTLED,
        }:
            action = ScheduledDispatchRecoveryAction.PROJECT_ONLY
        else:
            # Unknown/present execution states are malformed, not evidence that
            # business work has not started. The sweep quarantines this handoff.
            return None
    else:
        action = ScheduledDispatchRecoveryAction.PROJECT_ONLY
    if (
        dispatch["kind"] == ScheduledDispatchKind.SCHEDULER_PARENT.value
        and _scheduler_parent_dispatch_from_run(
            run,
            require_running=False,
        ) is None
    ):
        return None
    return PreparedScheduledDispatchRecovery(
        action=action.value,
        dispatch=dispatch,
    )


def _scheduled_dispatch_task(kind: ScheduledDispatchKind):
    """Resolve one durable dispatch kind to its Celery task."""

    from packages.core.tasks.ai_tasks import (
        run_agent_task,
        run_chat_insight_extraction,
        run_goal_measurement,
        run_morning_briefing,
        run_outcome_evaluation,
        run_strategist_review,
        run_workflow,
        run_workspace_stat_collection,
    )

    return {
        ScheduledDispatchKind.SCHEDULER_PARENT: _dispatch_job_task,
        ScheduledDispatchKind.AGENT_TASK: run_agent_task,
        ScheduledDispatchKind.WORKFLOW: run_workflow,
        ScheduledDispatchKind.GOAL_MEASUREMENT: run_goal_measurement,
        ScheduledDispatchKind.WORKSPACE_STAT_COLLECTION: (
            run_workspace_stat_collection
        ),
        ScheduledDispatchKind.STRATEGIST_REVIEW: run_strategist_review,
        ScheduledDispatchKind.MORNING_BRIEFING: run_morning_briefing,
        ScheduledDispatchKind.OUTCOME_EVALUATION: run_outcome_evaluation,
        ScheduledDispatchKind.CHAT_INSIGHT_EXTRACTION: (
            run_chat_insight_extraction
        ),
    }[kind]


def _publish_scheduled_dispatch(
    dispatch: PreparedScheduledDispatch,
    *,
    headers: dict[str, Any] | None = None,
) -> None:
    """Publish a previously committed scheduler handoff."""

    kind = ScheduledDispatchKind(dispatch["kind"])
    publish_options = {"headers": headers} if headers else {}
    _scheduled_dispatch_task(kind).apply_async(
        args=dispatch["args"],
        kwargs=dispatch["kwargs"],
        **publish_options,
    )


def _execution_recovery_dispatch(
    dispatch: PreparedScheduledDispatch,
) -> tuple[PreparedScheduledDispatch, dict[str, Any]]:
    """Build a non-recursive recovery delivery without mutating its ledger."""

    kind = ScheduledDispatchKind(dispatch["kind"])
    kwargs = dict(dispatch["kwargs"])
    headers: dict[str, Any] = {}
    if kind is ScheduledDispatchKind.AGENT_TASK:
        # The scheduler sweep is already the durable delayed recheck. If the
        # child claim is still held, this delivery must not create a second
        # lease-length retry chain of its own.
        kwargs["claim_recheck"] = True
    elif kind is ScheduledDispatchKind.WORKFLOW:
        # Old Workflow workers do not accept the claim_recheck keyword. A
        # custom header is ignored by them and understood by upgraded workers.
        headers[SCHEDULED_EXECUTION_RECOVERY_HEADER] = True
    return (
        PreparedScheduledDispatch(
            kind=dispatch["kind"],
            args=list(dispatch["args"]),
            kwargs=kwargs,
        ),
        headers,
    )


async def _mark_scheduled_dispatch_published(
    run_id: str,
    dispatch: PreparedScheduledDispatch,
) -> None:
    """Project a broker-accepted durable handoff and its ledger event."""

    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    async with create_worker_session()() as db:
        run = (await db.execute(
            select(ScheduledJobRun)
            .where(ScheduledJobRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        result = dict(run.result or {}) if run is not None else {}
        current_dispatch = _scheduled_dispatch_from_result(result)
        dispatch_status = result.get("dispatch_status")
        if (
            run is None
            or current_dispatch != dispatch
            or dispatch_status not in {
                ScheduledDispatchState.PREPARED.value,
                ScheduledDispatchState.PUBLISHED.value,
            }
        ):
            await db.rollback()
            return
        result["dispatch_status"] = ScheduledDispatchState.PUBLISHED.value
        run.result = result
        from packages.core.services.scheduled_run_lifecycle import (
            defer_scheduled_recovery,
        )

        defer_scheduled_recovery(
            run,
            kind=ScheduledRecoveryKind.EXECUTION,
            now=datetime.now(timezone.utc),
            retry_after=timedelta(
                seconds=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
            ),
        )
        job = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.job_id == run.job_id)
        )).scalar_one_or_none()
        if job is not None:
            if dispatch["kind"] == ScheduledDispatchKind.SCHEDULER_PARENT.value:
                await db.commit()
                return
            ledger_context = result.get("dispatch_ledger")
            if not isinstance(ledger_context, dict):
                ledger_context = {}
            raw_occurred_at = ledger_context.get("occurred_at")
            try:
                occurred_at = datetime.fromisoformat(str(raw_occurred_at))
            except (TypeError, ValueError):
                occurred_at = run.started_at or datetime.now(timezone.utc)
            raw_revision = ledger_context.get("revision")
            revision = raw_revision if isinstance(raw_revision, int) else None
            raw_experiment_id = ledger_context.get("experiment_id")
            experiment_id = (
                raw_experiment_id
                if isinstance(raw_experiment_id, str) and raw_experiment_id
                else None
            )
            from packages.core.ledger.adapters import record_automation_dispatched

            await record_automation_dispatched(
                db,
                job,
                run_id=run.id,
                now=occurred_at,
                revision=revision,
                experiment_id=experiment_id,
            )
        await db.commit()


async def _load_prepared_scheduled_dispatch(
    run_id: str,
) -> PreparedScheduledDispatchRecovery | None:
    """Reload one durable handoff without changing its execution state."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.scheduler import ScheduledJobRun

    async with create_worker_session()() as db:
        run = (await db.execute(
            select(ScheduledJobRun).where(ScheduledJobRun.id == run_id)
        )).scalar_one_or_none()
        return (
            _prepared_scheduled_dispatch_recovery(run)
            if run is not None
            else None
        )


@celery_app.task(
    bind=True,
    name="scheduler.recover_prepared_dispatch",
    max_retries=SCHEDULED_DISPATCH_RECOVERY_MAX_RETRIES,
)
def _recover_prepared_scheduled_dispatch_task(self, run_id: str):
    """Recover a handoff without replaying a terminal child."""
    try:
        recovery = _run_async(_load_prepared_scheduled_dispatch(run_id))
        if recovery is None:
            return {"run_id": run_id, "status": "already_projected"}
        action = ScheduledDispatchRecoveryAction(recovery["action"])
        dispatch = recovery["dispatch"]
        if action is ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS:
            # This delivery may have been queued while the handoff was still
            # PREPARED. Only the age-gated periodic sweep may terminalize an
            # admitted generic body after its execution lease window.
            return {
                "run_id": run_id,
                "status": "awaiting_execution_reconciliation",
                "recovery_action": action.value,
            }
        if action in {
            ScheduledDispatchRecoveryAction.REPUBLISH_AND_PROJECT,
            ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION,
        }:
            if action is ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION:
                publish_dispatch, publish_headers = (
                    _execution_recovery_dispatch(dispatch)
                )
            else:
                publish_dispatch, publish_headers = dispatch, {}
            if publish_headers:
                _publish_scheduled_dispatch(
                    publish_dispatch,
                    headers=publish_headers,
                )
            else:
                _publish_scheduled_dispatch(publish_dispatch)
        _run_async(_mark_scheduled_dispatch_published(run_id, dispatch))
        return {
            "run_id": run_id,
            "status": "published",
            "recovery_action": action.value,
        }
    except Exception as exc:
        logger.exception("Prepared scheduler dispatch recovery failed run=%s", run_id)
        raise self.retry(exc=exc, countdown=SCHEDULED_RECOVERY_RETRY_SECONDS)


async def _cancel_scheduler_parent_dispatch(
    db,
    job,
    *,
    occurrence_key: str | None,
    reason: str,
) -> bool:
    """Terminalize only an unconsumed durable parent occurrence."""

    if not occurrence_key:
        return False
    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunOutcome,
        apply_scheduled_run_outcome,
    )
    from packages.core.services.scheduler_service import (
        reconcile_scheduled_job_run_projection,
    )

    run = (await db.execute(
        select(ScheduledJobRun)
        .where(
            ScheduledJobRun.job_id == job.job_id,
            ScheduledJobRun.idempotency_key == occurrence_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if run is None or _scheduler_parent_dispatch_from_run(run) is None:
        return False
    apply_scheduled_run_outcome(run, ScheduledRunOutcome.cancelled(reason))
    await reconcile_scheduled_job_run_projection(
        db,
        job,
        finalized_run_id=run.id,
    )
    from packages.core.ledger.adapters import record_automation_run_finished

    await record_automation_run_finished(
        db,
        job,
        run_id=run.id,
        status=ScheduledRunStatus.CANCELLED.value,
    )
    await db.flush()
    return True


async def _async_dispatch_single(
    job_db_id: str,
    now_iso: str,
    *,
    manual: bool = False,
    occurrence_key: str | None = None,
    allow_legacy_uncommitted_clock: bool = False,
):
    from packages.core.database import create_worker_session
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.reusable_resource_locks import (
        reusable_resource_lifecycle_lease,
    )

    now = datetime.fromisoformat(now_iso)

    dispatch: PreparedScheduledDispatch | None = None
    run_id: str | None = None
    async with create_worker_session()() as db:
        entity_id = (await db.execute(
            select(ScheduledJob.entity_id).where(ScheduledJob.id == job_db_id)
        )).scalar_one_or_none()
        if entity_id is None:
            return
        async with reusable_resource_lifecycle_lease(db, entity_id=entity_id):
            result = await db.execute(
                select(ScheduledJob)
                .where(ScheduledJob.id == job_db_id)
                .with_for_update()
            )
            job = result.scalar_one_or_none()
        if not job:
            return
        if not job.enabled:
            if occurrence_key and occurrence_key.startswith(
                _SCHEDULED_OCCURRENCE_PREFIX
            ):
                cancelled = await _cancel_scheduler_parent_dispatch(
                    db,
                    job,
                    occurrence_key=occurrence_key,
                    reason="scheduled_job_disabled",
                )
                if cancelled:
                    await db.commit()
            return
        stale_occurrence = (
            occurrence_key is not None
            and _is_scheduled_occurrence_key(job, occurrence_key)
            and not _scheduled_occurrence_is_current(job, now, occurrence_key)
        )
        if not manual and stale_occurrence:
            legacy_clock_pending = _legacy_scheduled_occurrence_clock_pending(
                job,
                now,
                occurrence_key,
            )
            if legacy_clock_pending and not allow_legacy_uncommitted_clock:
                # Old Beat versions publish first and persist last_run_at in
                # the surrounding transaction afterwards. Waiting on the Job
                # row cannot close that ordering window when a new worker
                # acquired the lock first. A dedicated rolling-upgrade retry
                # budget re-reads the clock before bounded fallback.
                raise _ScheduledOccurrenceClockPending(
                    f"legacy dispatch clock not committed for {job.job_id}"
                )
            if not legacy_clock_pending:
                logger.info(
                    "Ignored stale scheduled occurrence after config change "
                    "job=%s key=%s",
                    job.job_id,
                    occurrence_key,
                )
                if occurrence_key.startswith(_SCHEDULED_OCCURRENCE_PREFIX):
                    cancelled = await _cancel_scheduler_parent_dispatch(
                        db,
                        job,
                        occurrence_key=occurrence_key,
                        reason="scheduled_job_revision_changed",
                    )
                    if cancelled:
                        await db.commit()
                return
            logger.warning(
                "Consuming legacy scheduled occurrence after dispatch-clock "
                "wait exhausted job=%s key=%s",
                job.job_id,
                occurrence_key,
            )

        prepared = await _dispatch_job(
            db,
            job,
            now,
            manual=manual,
            occurrence_key=occurrence_key,
        )
        if prepared is not None:
            dispatch, run_id = prepared
        await db.commit()

    if dispatch is None or run_id is None:
        return
    _publish_scheduled_dispatch(dispatch)
    try:
        await _mark_scheduled_dispatch_published(run_id, dispatch)
    except Exception:
        logger.exception(
            "Scheduled dispatch %s was published but not projected",
            run_id,
        )
        # Do not replay the parent dispatcher: its business handoff was already
        # accepted. A recovery task backed by the periodic PREPARED sweep marks
        # this exact run without creating a new occurrence. It republishes only
        # while the occurrence is still running; terminal runs are projection-only.
        try:
            _recover_prepared_scheduled_dispatch_task.apply_async(
                args=[run_id],
                countdown=SCHEDULED_RECOVERY_RETRY_SECONDS,
            )
        except Exception:
            logger.exception(
                "Could not queue dispatch projection recovery run=%s; "
                "the scheduler sweep will retry it",
                run_id,
            )


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


def _legacy_scheduled_occurrence_key(job, now: datetime) -> str:
    """Return the occurrence key emitted before revision-bound keys existed."""

    now_utc = _as_aware_utc(now)
    if getattr(job, "delete_after_run", False):
        return f"delete-after:{job.job_id}"
    if getattr(job, "schedule_kind", None) == "cron":
        minute = now_utc.replace(second=0, microsecond=0)
        return f"cron:{minute.isoformat()}"
    if getattr(job, "schedule_kind", None) == "at" and getattr(job, "run_at", None):
        try:
            return f"at:{_parse_run_at(job.run_at, job).isoformat()}"
        except (TypeError, ValueError):
            pass
    if (
        getattr(job, "schedule_kind", None) in INTERVAL_SCHEDULE_KINDS
        and getattr(job, "every_seconds", None)
    ):
        interval_seconds = max(float(job.every_seconds), 0.001)
        bucket = math.floor(now_utc.timestamp() / interval_seconds)
        return f"interval:{interval_seconds:g}:{bucket}"
    minute = now_utc.replace(second=0, microsecond=0)
    return f"scheduled:{minute.isoformat()}"


def _scheduled_occurrence_key(job, now: datetime) -> str:
    """Return a rolling-compatible occurrence identity bound to a revision.

    The prefix intentionally does not match any legacy one-shot prefix. Older
    workers therefore treat the whole value as an opaque idempotency key,
    while current workers can reject a queued message after a revision change.
    """

    revision = int(getattr(job, "revision", 1) or 1)
    legacy_key = _legacy_scheduled_occurrence_key(job, now)
    return f"{_SCHEDULED_OCCURRENCE_PREFIX}{revision}:{legacy_key}"


def _is_scheduled_occurrence_key(job, occurrence_key: str) -> bool:
    """Recognize keys emitted by current and legacy scheduler ticks."""

    legacy_delete_after_key = f"delete-after:{job.job_id}"
    return (
        occurrence_key.startswith(_SCHEDULED_OCCURRENCE_PREFIX)
        or occurrence_key.startswith(("at:", "cron:", "interval:", "scheduled:"))
        or occurrence_key == legacy_delete_after_key
        or occurrence_key.startswith(f"{legacy_delete_after_key}:at:")
    )


def _scheduled_occurrence_is_current(
    job,
    now: datetime,
    occurrence_key: str,
) -> bool:
    """Validate a queued scheduled message against its persisted clock/config."""

    last_run_at = getattr(job, "last_run_at", None)
    if (
        last_run_at is None
        or _as_aware_utc(last_run_at) != _as_aware_utc(now)
    ):
        return False
    if occurrence_key.startswith(_SCHEDULED_OCCURRENCE_PREFIX):
        return occurrence_key == _scheduled_occurrence_key(job, now)

    legacy_delete_after_key = f"delete-after:{job.job_id}"
    if occurrence_key.startswith(f"{legacy_delete_after_key}:at:"):
        if (
            not getattr(job, "delete_after_run", False)
            or getattr(job, "schedule_kind", None) != "at"
            or not getattr(job, "run_at", None)
        ):
            return False
        try:
            run_at = _parse_run_at(job.run_at, job)
        except (TypeError, ValueError):
            return False
        return occurrence_key == f"{legacy_delete_after_key}:at:{run_at.isoformat()}"
    return occurrence_key == _legacy_scheduled_occurrence_key(job, now)


def _legacy_scheduled_occurrence_clock_pending(
    job,
    now: datetime,
    occurrence_key: str,
) -> bool:
    """Detect the old-Beat/new-worker publish-before-clock race.

    A legacy key has no revision, so it is retried only while its identity
    still matches the current schedule *and* that schedule remains due. A
    versioned key with a mismatched clock/revision is conclusively stale.
    """

    if occurrence_key.startswith(_SCHEDULED_OCCURRENCE_PREFIX):
        return False
    expected = _legacy_scheduled_occurrence_key(job, now)
    interim_delete_after = False
    delete_after_prefix = f"delete-after:{job.job_id}:at:"
    if occurrence_key.startswith(delete_after_prefix):
        if (
            not getattr(job, "delete_after_run", False)
            or getattr(job, "schedule_kind", None) != "at"
            or not getattr(job, "run_at", None)
        ):
            return False
        try:
            interim_delete_after = occurrence_key == (
                f"delete-after:{job.job_id}:at:"
                f"{_parse_run_at(job.run_at, job).isoformat()}"
            )
        except (TypeError, ValueError):
            return False
    if occurrence_key != expected and not interim_delete_after:
        return False
    return _is_due(job, now)


def _is_one_shot_occurrence_key(job, occurrence_key: str) -> bool:
    """Return whether a queued key needs one-shot schedule revalidation."""

    delete_after_prefix = f"delete-after:{job.job_id}"
    return (
        occurrence_key.startswith("at:")
        or occurrence_key == delete_after_prefix
        or occurrence_key.startswith(f"{delete_after_prefix}:at:")
    )


def _scheduled_one_shot_occurrence_is_current(
    job,
    now: datetime,
    occurrence_key: str,
) -> bool:
    """Validate one-shot identity, including the pre-versioned legacy key."""

    legacy_delete_after_key = f"delete-after:{job.job_id}"
    if occurrence_key == legacy_delete_after_key:
        last_run_at = getattr(job, "last_run_at", None)
        return bool(
            getattr(job, "delete_after_run", False)
            and last_run_at is not None
            and _as_aware_utc(last_run_at) == _as_aware_utc(now)
        )
    if occurrence_key.startswith(f"{legacy_delete_after_key}:at:"):
        if (
            not getattr(job, "delete_after_run", False)
            or getattr(job, "schedule_kind", None) != "at"
            or not getattr(job, "run_at", None)
        ):
            return False
        try:
            run_at = _parse_run_at(job.run_at, job)
        except (TypeError, ValueError):
            return False
        return occurrence_key == f"{legacy_delete_after_key}:at:{run_at.isoformat()}"
    return occurrence_key == _legacy_scheduled_occurrence_key(job, now)


def _is_due(job, now: datetime) -> bool:
    """Check if a scheduled job is due for execution."""
    now_utc = _as_aware_utc(now)

    if job.delete_after_run and job.last_run_at is not None:
        return False

    next_run_at = getattr(job, "next_run_at", None)
    if next_run_at is not None:
        return _as_aware_utc(next_run_at) <= now_utc

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

    field_ranges = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    for field_val, pattern, (minimum, maximum) in zip(fields, parts, field_ranges):
        if not cron_field_matches(
            field_val,
            pattern,
            minimum=minimum,
            maximum=maximum,
        ):
            return False

    # Don't re-trigger if already run this minute
    if last_run:
        if last_run.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
            return False

    return True
async def _dispatch_job(
    db,
    job,
    now: datetime,
    *,
    manual: bool = False,
    occurrence_key: str | None = None,
):
    """Dispatch the appropriate execution for a job."""
    from packages.core.ai.runtime import (
        runtime_scheduled_job_prompt,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.services.scheduler_service import (
        claim_job_run,
        notify_scheduled_job_auto_paused,
        reconcile_scheduled_job_run_projection,
    )

    if manual:
        trigger_type = "manual"
    elif job.schedule_kind == "cron":
        trigger_type = "cron"
    else:
        trigger_type = job.schedule_kind or "scheduled"

    if occurrence_key:
        resolved_occurrence_key = occurrence_key
    elif manual:
        # Production entrypoints always supply a request/delivery key.  Keep
        # direct internal callers independent by treating each call as a new
        # operator request.
        resolved_occurrence_key = f"manual:{generate_ulid()}"
    else:
        resolved_occurrence_key = _scheduled_occurrence_key(job, now)

    # Atomically claim the occurrence before creating Tasks, Workflow runs, or
    # outbound messages. A redelivered Celery message returns here; a distinct
    # same-day occurrence has a distinct key and proceeds normally.
    run, claimed = await claim_job_run(
        db, job.job_id, status="running",
        idempotency_key=resolved_occurrence_key,
        trigger_type=trigger_type,
        started_at=now,
    )
    if not claimed:
        parent_dispatch = _scheduler_parent_dispatch_from_run(
            run,
            job_db_id=getattr(job, "id", None),
            now=now,
            occurrence_key=resolved_occurrence_key,
        )
        if parent_dispatch is not None:
            logger.info(
                "Consuming durable scheduler parent job=%s key=%s run=%s",
                job.job_id,
                resolved_occurrence_key,
                run.id,
            )
            # The parent dispatch already owns this occurrence. Continue in
            # the same transaction and replace its envelope with the exact
            # child handoff (or a terminal outcome).
            claimed = True
        else:
            prepared_dispatch = _recover_prepared_scheduled_dispatch(run)
            if prepared_dispatch is not None:
                logger.info(
                    "Recovering unpublished scheduler occurrence job=%s key=%s run=%s",
                    job.job_id,
                    resolved_occurrence_key,
                    run.id,
                )
                return prepared_dispatch, run.id
            logger.info(
                "Ignored duplicate scheduler occurrence job=%s key=%s run=%s",
                job.job_id,
                resolved_occurrence_key,
                run.id,
            )
            return
    run_id = run.id

    # Update job state
    job.last_run_at = now
    job.last_status = "running"
    from packages.core.schedule_clock import refresh_scheduled_job_next_run_at

    refresh_scheduled_job_next_run_at(
        job,
        now=now,
        inclusive=False,
    )
    await db.flush()

    async def _mark_run_error(reason: str) -> None:
        """Surface a misconfiguration as a failed run instead of leaving
        the row stuck in 'running' forever."""
        run.status = "error"
        run.error = reason
        run.completed_at = datetime.now(timezone.utc)
        if run.started_at:
            run.duration_ms = (run.completed_at - run.started_at).total_seconds() * 1000
        auto_paused = await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        if auto_paused:
            await notify_scheduled_job_auto_paused(
                db,
                job,
                failure_key=run.id,
            )
        from packages.core.ledger.adapters import record_automation_run_finished

        await record_automation_run_finished(
            db,
            job,
            run_id=run.id,
            status="error",
        )
        await db.flush()

    async def _mark_run_skipped(reason: str) -> None:
        """Skip workspace-scoped automation when the workspace is offline."""
        completed_at = datetime.now(timezone.utc)
        run.status = "skipped"
        run.result = {"skipped": True, "reason": reason}
        run.completed_at = completed_at
        if run.started_at:
            run.duration_ms = (completed_at - run.started_at).total_seconds() * 1000
        await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        await db.flush()

    async def _mark_run_completed(result: dict) -> None:
        """Close a dispatch whose durable output is the created Task itself."""
        completed_at = datetime.now(timezone.utc)
        run.status = "completed"
        run.result = result
        run.completed_at = completed_at
        if run.started_at:
            run.duration_ms = (
                completed_at - run.started_at
            ).total_seconds() * 1000
        await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        from packages.core.ledger.adapters import record_automation_run_finished

        await record_automation_run_finished(
            db,
            job,
            run_id=run.id,
            status="completed",
        )
        await db.flush()

    async def _disable_job() -> None:
        """Close a no-longer-valid system schedule with a revision fence."""

        if not job.enabled:
            return
        from packages.core.services.scheduler_service import (
            ScheduledJobMutationFactory,
        )

        await ScheduledJobMutationFactory.apply_locked(
            db,
            job,
            {"enabled": False},
            causation_id=run.id,
        )

    entity_id = str(getattr(job, "entity_id", None) or "").strip()
    if not entity_id and _scheduled_job_requires_credit_gate(job):
        await _mark_run_error("scheduled job missing entity_id")
        logger.warning(
            "Scheduled job %s blocked before fan-out: missing entity_id",
            job.job_id,
        )
        return

    # M13: resolve the per-run effective config. While the owning experiment
    # is running its overlay patch is shallow-merged over execution_target
    # for THIS run only (the stored config is untouched, no revision bump);
    # a stale overlay (experiment stopped/deleted) is ignored.
    from packages.core.experiments import effective_dispatch_config
    target, experiment_id, experiment_patch = await effective_dispatch_config(
        db, job.execution_target,
    )
    from packages.core.services.scheduler_service import (
        scheduled_job_workspace_scope,
    )

    try:
        workspace_id = scheduled_job_workspace_scope(job.workspace_id, target)
    except ValueError as exc:
        await _mark_run_error(str(exc))
        return
    if workspace_id:
        from sqlalchemy import or_, select
        from packages.core.models.workspace import Workspace

        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == job.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is None:
            await _mark_run_skipped("workspace_not_found")
            return
        from packages.core.services.workspace_readiness import (
            evaluate_workspace_blocking_setup,
        )

        setup_status = await evaluate_workspace_blocking_setup(db, workspace)
        if setup_status is not None and setup_status.blocks_work:
            from packages.core.constants.blueprints import installed_blueprint_job_id

            allowed_setup_job_ids = {
                installed_blueprint_job_id(
                    check.get("setup_job_id"),
                    workspace.id,
                )
                for check in setup_status.details.get("incomplete_checks") or []
                if str(check.get("setup_job_id") or "").strip()
            }
            if str(job.job_id or "").strip() not in allowed_setup_job_ids:
                await _mark_run_skipped("workspace_setup_incomplete")
                return
        from packages.core.workspaces import is_sandbox_workspace

        if is_sandbox_workspace(workspace):
            await _mark_run_skipped("workspace_simulation")
            return
        autonomy_state = workspace_autonomy_state(workspace)
        if autonomy_state not in {
            WorkspaceAutonomyState.RUNNING,
            WorkspaceAutonomyState.DISABLED,
        }:
            await _mark_run_skipped(
                workspace_autonomy_skip_reason(workspace) or "workspace_unavailable"
            )
            return

    try:
        await _preflight_scheduled_job_credits(
            job,
            db=db,
            workspace_id=workspace_id,
        )
    except CreditExhaustedError as exc:
        await _mark_run_error(f"credits_exhausted: {exc}")
        logger.warning(
            "Scheduled job %s blocked before fan-out: credits exhausted",
            job.job_id,
        )
        return

    # An active experiment patch may override the run's payload_message /
    # execution_script (config-level keys) without touching the job row.
    payload_message = job.payload_message
    execution_script = job.execution_script
    if experiment_patch:
        if "payload_message" in experiment_patch:
            payload_message = experiment_patch["payload_message"]
        if "execution_script" in experiment_patch:
            execution_script = experiment_patch["execution_script"]
    prompt = runtime_scheduled_job_prompt(
        execution_script=execution_script,
        payload_message=payload_message,
        name=job.name,
    ).prompt

    # ── Dispatch based on execution_type ──
    exec_type = job.execution_type or "agent"
    prepared_dispatch: PreparedScheduledDispatch | None = None
    terminal_dispatch_result: dict | None = None

    if exec_type == "workflow":
        # Linked to a WorkflowDefinition — most deterministic
        # Create a new WorkflowRun and dispatch
        workflow_id = (job.execution_target or {}).get("workflow_id") or job.goal_id
        if not workflow_id:
            await _mark_run_error(
                "exec_type='workflow' but no workflow_id in execution_target"
            )
            return
        trigger_data = dict(target)
        trigger_data.update({
            "payload_message": prompt,
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run_id,
            # The scheduler's claimed occurrence is the run's business
            # period identity. Strategist briefing uses it to distinguish a
            # completed 7am run from a duplicate launch in the same period.
            "period_key": resolved_occurrence_key,
        })
        if workspace_id:
            trigger_data["workspace_id"] = workspace_id
        if job.conversation_id:
            trigger_data.setdefault("conversation_id", job.conversation_id)
        if job.manor_task_id:
            trigger_data.setdefault("task_id", job.manor_task_id)
        from packages.core.services.workflow_service import (
            get_binding,
            start_workflow,
            start_workflow_from_binding,
        )

        try:
            binding_id = target.get("binding_id")
            binding = await get_binding(db, binding_id, job.entity_id or "") if binding_id else None
            if binding_id and binding is None:
                raise ValueError("Workspace workflow binding not found")
            if binding and binding.workflow_id != workflow_id:
                raise ValueError("Scheduled workflow does not match its workspace binding")
            if binding and workspace_id and binding.workspace_id != workspace_id:
                raise ValueError("Scheduled workflow binding belongs to another workspace")
            if binding:
                wrun = await start_workflow_from_binding(
                    db,
                    binding,
                    variables={"payload_message": prompt},
                    trigger_data=trigger_data,
                    started_by=job.user_id,
                    trigger_source="schedule",
                )
            else:
                wrun = await start_workflow(
                    db,
                    entity_id=job.entity_id or "",
                    workflow_id=workflow_id,
                    variables={"payload_message": prompt},
                    trigger_data=trigger_data,
                    started_by=job.user_id,
                    workspace_id=workspace_id,
                    trigger_source="schedule",
                )
        except ValueError as exc:
            await _mark_run_error(str(exc))
            return
        await db.flush()
        prepared_dispatch = _prepared_scheduled_dispatch(
            ScheduledDispatchKind.WORKFLOW,
            args=[wrun.id, run_id, job.job_id],
        )
        logger.info("Prepared workflow run %s for job %s", wrun.id, job.job_id)

    elif exec_type == "skill":
        # Linked to a Skill — bind the full runtime bundle to the Task. The
        # TaskRunner forces invoke_skill on its first turn, so scripts,
        # references, requirements and tool policy are not flattened to text.
        skill_id = (job.execution_target or {}).get("skill_id")
        if not skill_id:
            await _mark_run_error(
                "exec_type='skill' but no skill_id in execution_target"
            )
            return
        from sqlalchemy import or_, select
        from packages.core.models.skill import Skill
        result = await db.execute(select(Skill).where(
            Skill.id == skill_id,
            Skill.status == "active",
            or_(Skill.entity_id == job.entity_id, Skill.entity_id.is_(None)),
        ))
        skill = result.scalar_one_or_none()
        if not skill:
            await _mark_run_error(f"Skill {skill_id} not found")
            return
        from packages.core.ai.runtime.skills import runtime_skill_is_eligible

        eligible, eligibility_code = await runtime_skill_is_eligible(
            db,
            skill,
            entity_id=str(job.entity_id or ""),
            workspace_id=workspace_id,
            user_id=job.user_id,
            enforce_user_access=bool(job.user_id),
        )
        if not eligible:
            await _mark_run_error(
                f"Skill {skill_id} is not eligible for this scheduled run "
                f"({eligibility_code})"
            )
            return
        skill_input = str(payload_message or job.name or "").strip()
        prepared_dispatch = await _dispatch_agent_task(
            db,
            job,
            now,
            skill_input,
            run_id,
            scheduled_skill_id=skill.id,
        )

    elif exec_type == "agent" or exec_type == "orchestrator_prompt":
        # Free-form AI agent execution
        if job.agent_id:
            prepared_dispatch = await _dispatch_agent_task(
                db,
                job,
                now,
                prompt,
                run_id,
            )
        elif prompt:
            # No agent — create a manual task
            from packages.core.services.task_service import create_task
            task = await create_task(
                db, job.entity_id or "",
                title=f"[Auto] {job.name or 'Scheduled Task'}",
                description=prompt, task_type="ai_generated",
                workspace_id=job.workspace_id,
                creator_id=job.user_id,
                details={
                    "scheduled_job_id": job.job_id,
                    "scheduled_run_id": run_id,
                },
                creation_logged_by_system=True,
                creation_log_metadata={
                    "entity_id": job.entity_id,
                    "workspace_id": job.workspace_id,
                    "scheduled_job_id": job.job_id,
                    "scheduled_run_id": run_id,
                },
            )
            job.manor_task_id = task.id
            await db.flush()
            terminal_dispatch_result = {
                "task_id": task.id,
                "dispatch_mode": "manual_task_created",
            }
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
            await _disable_job()
            return
        if not should_install_measurement_schedule(goal):
            await _mark_run_skipped(measurement_schedule_skip_reason(goal))
            await _disable_job()
            return
        if (
            workspace_id
            and workspace_autonomy_state(workspace) is not WorkspaceAutonomyState.RUNNING
        ):
            await _mark_run_skipped(
                workspace_autonomy_skip_reason(workspace) or "workspace_unavailable"
            )
            await _disable_job()
            return
        prepared_dispatch = _prepared_scheduled_dispatch(
            ScheduledDispatchKind.GOAL_MEASUREMENT,
            args=[goal_id],
            kwargs={"run_id": run_id, "job_id_str": job.job_id},
        )

    elif exec_type == "workspace_stat_collection":
        from sqlalchemy import select
        target = job.execution_target or {}
        stat_id = target.get("stat_id")
        if not stat_id:
            await _mark_run_error(
                "exec_type='workspace_stat_collection' but no stat_id in execution_target"
            )
            return
        from packages.core.models.workspace_stat import WorkspaceStat
        from packages.core.stats.scheduling import should_schedule
        stat = (await db.execute(
            select(WorkspaceStat).where(WorkspaceStat.id == stat_id)
        )).scalar_one_or_none()
        if stat is None:
            await _mark_run_skipped("stat_not_found")
            await _disable_job()
            return
        if not should_schedule(stat):
            await _mark_run_skipped("stat_collection_not_scheduled")
            await _disable_job()
            return
        prepared_dispatch = _prepared_scheduled_dispatch(
            ScheduledDispatchKind.WORKSPACE_STAT_COLLECTION,
            args=[stat_id],
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
        finalise_kwargs = {"run_id": run_id, "job_id_str": job.job_id}
        if exec_type == "strategist_review":
            from packages.core.strategist import ReviewTrigger, ReviewTriggerKind
            prepared_dispatch = _prepared_scheduled_dispatch(
                ScheduledDispatchKind.STRATEGIST_REVIEW,
                args=[workspace_id],
                kwargs={
                    **finalise_kwargs,
                    **ReviewTrigger(
                        kind=ReviewTriggerKind.SCHEDULED,
                        detail="workspace cadence",
                    ).celery_kwargs(),
                },
            )
        elif exec_type == "briefing":
            briefing_timezone = target.get("timezone") or job.timezone
            prepared_dispatch = _prepared_scheduled_dispatch(
                ScheduledDispatchKind.MORNING_BRIEFING,
                args=[workspace_id],
                kwargs={**finalise_kwargs, "timezone_name": briefing_timezone},
            )
        elif exec_type == "outcome_evaluation":
            prepared_dispatch = _prepared_scheduled_dispatch(
                ScheduledDispatchKind.OUTCOME_EVALUATION,
                args=[workspace_id],
                kwargs=finalise_kwargs,
            )
        else:  # chat_insight_extraction
            prepared_dispatch = _prepared_scheduled_dispatch(
                ScheduledDispatchKind.CHAT_INSIGHT_EXTRACTION,
                args=[workspace_id],
                kwargs=finalise_kwargs,
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

    if prepared_dispatch is not None:
        run.result = {
            "dispatch": prepared_dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch_ledger": ScheduledDispatchLedgerContext(
                occurred_at=now.isoformat(),
                revision=getattr(job, "revision", None),
                experiment_id=experiment_id,
            ),
        }

    # Immediate/terminal work is already accepted here. Queue handoffs record
    # this fact only in _mark_scheduled_dispatch_published after broker accept.
    if prepared_dispatch is None:
        from packages.core.ledger.adapters import record_automation_dispatched

        await record_automation_dispatched(
            db, job, run_id=run_id, now=now,
            revision=getattr(job, "revision", None),
            experiment_id=experiment_id,
        )

    if terminal_dispatch_result is not None:
        await _mark_run_completed(terminal_dispatch_result)

    logger.info("Dispatched job %s (type=%s, task=%s)", job.job_id, exec_type, job.manor_task_id)
    if prepared_dispatch is not None:
        return prepared_dispatch, run_id
    return None


async def _dispatch_agent_task(
    db,
    job,
    now,
    prompt: str,
    run_id: str | None = None,
    *,
    scheduled_skill_id: str | None = None,
):
    """Create one Task per scheduled occurrence and prepare its queue handoff."""

    # A scheduled occurrence is immutable runtime identity. Reusing the
    # job-level Task lets a later overlapping occurrence overwrite the first
    # delivery's run metadata and terminalize the wrong ScheduledJobRun.
    task_id = None if run_id else job.manor_task_id

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
    required_plan_steps = target.get("required_plan_steps")
    if not isinstance(required_plan_steps, list):
        required_plan_steps = None

    details_base = {
        "scheduled_job_id": job.job_id,
        "scheduled_run_id": run_id,
        "execution_script": (
            None if scheduled_skill_id else job.execution_script or None
        ),
        "payload_message": job.payload_message or None,
        "default_delivery_mode": job.default_delivery_mode or None,
        "conversation_id": job.conversation_id or None,
        "done_when": done_when,
        "deliverable": deliverable,
        "max_turns": max_turns,
        "model_role": target.get("complexity", "primary"),
    }
    if scheduled_skill_id:
        details_base["scheduled_skill_id"] = scheduled_skill_id
    if required_plan_steps is not None:
        details_base["required_plan_steps"] = required_plan_steps

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
            creation_logged_by_system=True,
            creation_log_metadata={
                "entity_id": job.entity_id,
                "workspace_id": job.workspace_id,
                "scheduled_job_id": job.job_id,
                "scheduled_run_id": run_id,
            },
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

    return _prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=[task_id, job.agent_id],
        kwargs={
            "scheduled_run_id": run_id,
            "scheduled_job_id": job.job_id,
        },
    )
