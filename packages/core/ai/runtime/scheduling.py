from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.billing import (
    runtime_ensure_goal_billing_context,
    runtime_ensure_task_billing_context,
    runtime_ensure_workspace_billing_context,
)
from packages.core.ai.runtime.sources import (
    RUNTIME_BRIEFING_SOURCE,
    RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE,
    RUNTIME_GOAL_MEASUREMENT_SOURCE,
    RUNTIME_OUTCOME_EVALUATION_SOURCE,
    RUNTIME_PLAN_AND_RUN_TASK_SOURCE,
    RUNTIME_STRATEGIST_REVIEW_SOURCE,
)


RUNTIME_SCHEDULED_WORKSPACE_AI_BILLING_SOURCES: Mapping[str, str] = {
    RUNTIME_BRIEFING_SOURCE: RUNTIME_BRIEFING_SOURCE,
    RUNTIME_OUTCOME_EVALUATION_SOURCE: RUNTIME_OUTCOME_EVALUATION_SOURCE,
    RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE: RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE,
    RUNTIME_STRATEGIST_REVIEW_SOURCE: RUNTIME_STRATEGIST_REVIEW_SOURCE,
}
RUNTIME_SCHEDULED_SCOPED_AI_BILLING_SOURCES: Mapping[str, str] = {
    RUNTIME_GOAL_MEASUREMENT_SOURCE: RUNTIME_GOAL_MEASUREMENT_SOURCE,
    RUNTIME_PLAN_AND_RUN_TASK_SOURCE: RUNTIME_PLAN_AND_RUN_TASK_SOURCE,
}

SCHEDULED_FILE_OUTPUT_KINDS: tuple[str, ...] = (
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
)


@dataclass(frozen=True)
class RuntimeScheduledJobPrompt:
    """Resolved prompt contract for a scheduled AI job dispatch."""

    prompt: str
    source: str
    includes_payload_context: bool = False


def runtime_scheduled_job_prompt(
    *,
    execution_script: str | None,
    payload_message: str | None,
    name: str | None,
) -> RuntimeScheduledJobPrompt:
    """Resolve the prompt that a scheduled job should pass to AI execution."""

    if execution_script:
        prompt = execution_script
        if payload_message:
            prompt = f"{prompt}\n\n## Context\n{payload_message}"
        return RuntimeScheduledJobPrompt(
            prompt=prompt,
            source="execution_script",
            includes_payload_context=bool(payload_message),
        )

    return RuntimeScheduledJobPrompt(
        prompt=payload_message or name or "",
        source="payload_message" if payload_message else "name",
        includes_payload_context=False,
    )


def runtime_scheduled_skill_prompt(
    *,
    skill_system_prompt: str | None,
    input_prompt: str | None,
) -> str:
    """Resolve the agent prompt for scheduled skill execution."""

    base_prompt = skill_system_prompt or ""
    if input_prompt:
        return f"{base_prompt}\n\n## Input\n{input_prompt}"
    return base_prompt


def _coerce_scheduled_execution_target(
    execution_target: Mapping[str, Any] | None = None,
    *,
    output_kind: str = "",
    file_kind: str = "",
    artifact_kind: str = "",
    requires_generated_file: bool | None = None,
    requires_file_deliverable: bool | None = None,
    max_turns: Any = None,
) -> dict[str, Any]:
    """Build scheduler execution_target from explicit structured fields only."""

    target = dict(execution_target or {})
    kind = str(output_kind or file_kind or artifact_kind or "").strip().lower()
    if kind:
        target["output_kind"] = kind
    if requires_generated_file is not None:
        target["requires_generated_file"] = bool(requires_generated_file)
    if requires_file_deliverable is not None:
        target["requires_file_deliverable"] = bool(requires_file_deliverable)
    if max_turns not in (None, ""):
        try:
            target["max_turns"] = int(max_turns)
        except (TypeError, ValueError):
            pass
    return target


async def runtime_create_scheduled_job_action(
    *,
    entity_id: str,
    name: str = "",
    schedule_kind: str = "cron",
    payload_message: str = "",
    cron_expr: str = "",
    every_seconds: int | float = 0,
    run_at: str = "",
    agent_id: str = "",
    timezone_str: str = "UTC",
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    default_delivery_mode: str | None = None,
    execution_target: Mapping[str, Any] | None = None,
    output_kind: str = "",
    file_kind: str = "",
    artifact_kind: str = "",
    requires_generated_file: bool | None = None,
    requires_file_deliverable: bool | None = None,
    max_turns: Any = None,
) -> str:
    """Create a scheduled job through the Runtime action boundary."""

    import uuid

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import create_scheduled_job

    job_id = f"agent-{uuid.uuid4().hex[:12]}"
    job_type = (
        "cron"
        if schedule_kind == "cron"
        else "interval"
        if schedule_kind == "every"
        else "once"
    )
    resolved_execution_target = _coerce_scheduled_execution_target(
        execution_target,
        output_kind=output_kind,
        file_kind=file_kind,
        artifact_kind=artifact_kind,
        requires_generated_file=requires_generated_file,
        requires_file_deliverable=requires_file_deliverable,
        max_turns=max_turns,
    )

    async with async_session() as db:
        job = await create_scheduled_job(
            db,
            entity_id,
            job_id,
            name,
            job_type=job_type,
            schedule_kind=schedule_kind,
            cron_expr=cron_expr or None,
            every_seconds=float(every_seconds) if every_seconds else None,
            run_at=run_at or None,
            timezone_str=timezone_str,
            payload_message=payload_message,
            agent_id=agent_id or None,
            workspace_id=workspace_id,
            default_delivery_mode=default_delivery_mode,
            conversation_id=conversation_id,
            user_id=user_id,
            execution_target=resolved_execution_target,
        )
        await db.commit()

        if payload_message and agent_id:
            try:
                from packages.core.tasks.ai_tasks import generate_job_skill

                generate_job_skill.delay(job.id, payload_message, name)
            except Exception:
                pass

    return (
        f"Created scheduled job '{name}' "
        f"(id={job.job_id}, {schedule_kind}: {cron_expr or every_seconds or run_at})"
    )


async def runtime_list_scheduled_jobs_action(*, entity_id: str) -> str:
    """List scheduled jobs through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import list_scheduled_jobs

    async with async_session() as db:
        jobs, _total = await list_scheduled_jobs(db, entity_id)

    if not jobs:
        return "No scheduled jobs found."

    lines = [f"Found {len(jobs)} scheduled job(s):\n"]
    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        schedule = (
            job.cron_expr
            or (f"every {job.every_seconds}s" if job.every_seconds else job.run_at or "?")
        )
        lines.append(
            f"- [{job.job_id}] {job.name or '(unnamed)'} | "
            f"{job.schedule_kind}: {schedule} | {status} | "
            f"last: {job.last_run_at or 'never'}"
        )
    return "\n".join(lines)


async def runtime_query_scheduled_jobs_action(
    *,
    entity_id: str,
    query: str = "",
    workspace_id: str = "",
    enabled_only: bool = False,
    limit: int = 50,
) -> str:
    """Return structured scheduled-job data through the Runtime boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import list_scheduled_jobs

    resolved_limit = max(1, min(int(limit or 50), 200))
    resolved_workspace_id = str(workspace_id or "").strip() or None
    normalized_query = str(query or "").strip().casefold()
    async with async_session() as db:
        jobs, _total = await list_scheduled_jobs(
            db,
            entity_id,
            enabled_only=enabled_only,
            workspace_id=resolved_workspace_id,
            limit=200,
        )

    items = []
    for job in jobs:
        if normalized_query and normalized_query not in (
            f"{job.name or ''} {job.job_id}".casefold()
        ):
            continue
        items.append(
            {
                "job_id": job.job_id,
                "name": job.name,
                "schedule_kind": job.schedule_kind,
                "cron_expr": job.cron_expr,
                "every_seconds": job.every_seconds,
                "run_at": job.run_at,
                "timezone": job.timezone,
                "enabled": bool(job.enabled),
                "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
                "last_status": job.last_status,
                "consecutive_errors": int(job.consecutive_errors or 0),
                "workspace_id": job.workspace_id,
                "agent_id": job.agent_id,
            }
        )
        if len(items) >= resolved_limit:
            break
    return json.dumps({"automations": items, "total": len(items)})


async def runtime_cancel_scheduled_job_action(
    *,
    entity_id: str,
    job_id: str,
) -> str:
    """Cancel a scheduled job through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import delete_scheduled_job

    async with async_session() as db:
        deleted = await delete_scheduled_job(db, job_id, entity_id)
        if deleted:
            await db.commit()

    if deleted:
        return f"Deleted scheduled job '{job_id}'."
    return f"Job '{job_id}' not found."


async def runtime_toggle_scheduled_job_action(
    *,
    entity_id: str,
    job_id: str,
    enabled: bool = True,
) -> str:
    """Enable or disable a scheduled job through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import toggle_scheduled_job

    async with async_session() as db:
        job = await toggle_scheduled_job(db, job_id, entity_id, enabled)
        if job:
            await db.commit()

    if job:
        return f"Job '{job_id}' is now {'enabled' if enabled else 'disabled'}."
    return f"Job '{job_id}' not found."


async def runtime_run_scheduled_job_now_action(
    *,
    entity_id: str,
    job_id: str,
) -> str:
    """Dispatch an immediate run for a scheduled job through Runtime."""

    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.scheduler import ScheduledJob

    async with async_session() as db:
        result = await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.job_id == job_id,
                ScheduledJob.entity_id == entity_id,
            )
        )
        job = result.scalar_one_or_none()

    if not job:
        return f"Job '{job_id}' not found."

    from packages.core.tasks.scheduler_tasks import _dispatch_job_task

    _dispatch_job_task.delay(job.id, datetime.now(timezone.utc).isoformat(), manual=True)
    return f"Dispatched immediate run for job '{job_id}'."


async def runtime_ensure_scheduled_workspace_ai_billing_context(
    db: Any,
    workspace_id: str,
    *,
    execution_type: str,
) -> str:
    """Bind billing context for a workspace-scoped scheduled AI job."""

    source = RUNTIME_SCHEDULED_WORKSPACE_AI_BILLING_SOURCES[execution_type]
    await runtime_ensure_workspace_billing_context(
        db,
        workspace_id,
        source=source,
    )
    return source


async def runtime_ensure_morning_briefing_billing_context(
    db: Any,
    workspace_id: str,
) -> str:
    """Bind billing context for the morning briefing scheduled job."""

    return await runtime_ensure_scheduled_workspace_ai_billing_context(
        db,
        workspace_id,
        execution_type=RUNTIME_BRIEFING_SOURCE,
    )


async def runtime_ensure_outcome_evaluation_billing_context(
    db: Any,
    workspace_id: str,
) -> str:
    """Bind billing context for the outcome evaluation scheduled job."""

    return await runtime_ensure_scheduled_workspace_ai_billing_context(
        db,
        workspace_id,
        execution_type=RUNTIME_OUTCOME_EVALUATION_SOURCE,
    )


async def runtime_ensure_chat_insight_extraction_billing_context(
    db: Any,
    workspace_id: str,
) -> str:
    """Bind billing context for the chat insight extraction scheduled job."""

    return await runtime_ensure_scheduled_workspace_ai_billing_context(
        db,
        workspace_id,
        execution_type=RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE,
    )


async def runtime_ensure_strategist_review_billing_context(
    db: Any,
    workspace_id: str,
) -> str:
    """Bind billing context for the Strategist review scheduled job."""

    return await runtime_ensure_scheduled_workspace_ai_billing_context(
        db,
        workspace_id,
        execution_type=RUNTIME_STRATEGIST_REVIEW_SOURCE,
    )


async def runtime_ensure_goal_measurement_billing_context(
    db: Any,
    goal_id: str,
) -> Any:
    """Bind billing context for a scheduled goal measurement."""

    return await runtime_ensure_goal_billing_context(
        db,
        goal_id,
        source=RUNTIME_SCHEDULED_SCOPED_AI_BILLING_SOURCES[
            RUNTIME_GOAL_MEASUREMENT_SOURCE
        ],
    )


async def runtime_ensure_plan_and_run_task_billing_context(
    db: Any,
    task_id: str,
) -> Any:
    """Bind billing context for the plan-and-run task planner job."""

    return await runtime_ensure_task_billing_context(
        db,
        task_id,
        source=RUNTIME_SCHEDULED_SCOPED_AI_BILLING_SOURCES[
            RUNTIME_PLAN_AND_RUN_TASK_SOURCE
        ],
        model_role="primary",
    )
