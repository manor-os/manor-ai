from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.ai.runtime.billing import (
    runtime_ensure_goal_billing_context,
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
from packages.core.services.scheduler_service import RuntimeScheduledJobAgentSelection


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

SCHEDULED_JOB_CREATE_KINDS: tuple[str, ...] = ("cron", "every", "at")
# ``interval`` and ``once`` are legacy persisted aliases. Keep them queryable
# without advertising them for new jobs.
SCHEDULED_JOB_QUERY_KINDS: tuple[str, ...] = (
    *SCHEDULED_JOB_CREATE_KINDS,
    "interval",
    "once",
)


@dataclass(frozen=True)
class RuntimeScheduledJobPrompt:
    """Resolved prompt contract for a scheduled AI job dispatch."""

    prompt: str
    source: str
    includes_payload_context: bool = False


async def _resolve_runtime_scheduled_job_agent_id(
    db: Any,
    *,
    entity_id: str,
    requested_agent_id: str = "",
    caller_agent_id: str = "",
    caller_agent_subscription_id: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> RuntimeScheduledJobAgentSelection:
    """Compatibility facade for the Scheduler-owned locked resolver."""

    from packages.core.services.scheduler_service import (
        RuntimeScheduledJobAgentRequest,
        resolve_runtime_scheduled_job_agent,
    )

    return await resolve_runtime_scheduled_job_agent(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        request=RuntimeScheduledJobAgentRequest(
            requested_agent_id=requested_agent_id,
            caller_agent_id=caller_agent_id,
            caller_agent_subscription_id=caller_agent_subscription_id,
        ),
    )


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
    caller_agent_id: str = "",
    caller_agent_subscription_id: str | None = None,
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

    try:
        if schedule_kind not in SCHEDULED_JOB_CREATE_KINDS:
            raise ValueError(
                "schedule_kind must be one of: "
                + ", ".join(SCHEDULED_JOB_CREATE_KINDS)
            )
        ZoneInfo(str(timezone_str or "UTC"))
        if schedule_kind == "cron":
            from packages.core.cron import validate_cron_expression

            cron_expr = validate_cron_expression(cron_expr)
        elif schedule_kind == "every":
            every_seconds = float(every_seconds)
            if every_seconds <= 0:
                raise ValueError("every_seconds must be greater than zero")
        else:
            datetime.fromisoformat(str(run_at or ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        return json.dumps({
            "ok": False,
            "error": "invalid_automation_schedule",
            "message": str(exc) or "invalid schedule",
        })

    import uuid

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import (
        RuntimeScheduledJobAgentRequest,
        create_scheduled_job,
    )

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
        if workspace_id and user_id:
            from packages.core.services.workspace_access import user_can_write_workspace_id

            if not await user_can_write_workspace_id(
                db,
                workspace_id=workspace_id,
                entity_id=entity_id,
                user_id=user_id,
                role=None,
            ):
                return json.dumps({
                    "ok": False,
                    "error": "Workspace not found or not writable",
                })
        job = await create_scheduled_job(
            db,
            entity_id,
            job_id,
            name,
            job_type=job_type,
            schedule_kind=schedule_kind,
            cron_expr=cron_expr or None,
            every_seconds=float(every_seconds) if schedule_kind == "every" else None,
            run_at=run_at or None,
            timezone_str=timezone_str,
            payload_message=payload_message,
            agent_id=None,
            workspace_id=workspace_id,
            default_delivery_mode=default_delivery_mode,
            conversation_id=conversation_id,
            user_id=user_id,
            execution_target=resolved_execution_target,
            require_workspace=bool(workspace_id),
            runtime_agent_request=RuntimeScheduledJobAgentRequest(
                requested_agent_id=agent_id,
                caller_agent_id=caller_agent_id,
                caller_agent_subscription_id=caller_agent_subscription_id,
            ),
        )
        agent_selection = job._runtime_agent_selection
        await db.commit()

        if payload_message and agent_selection.agent_id and job.enabled:
            try:
                from packages.core.tasks.ai_tasks import generate_job_skill

                generate_job_skill.delay(
                    job.id,
                    payload_message,
                    name,
                    int(job.revision or 1),
                )
                from packages.core.constants.execution import (
                    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS,
                )
                from packages.core.services.scheduler_service import (
                    defer_scheduled_job_skill_generation,
                )

                await defer_scheduled_job_skill_generation(
                    db,
                    job_id=job.id,
                    revision=int(job.revision or 1),
                    next_attempt_at=datetime.now(timezone.utc)
                    + timedelta(
                        seconds=(
                            SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
                        )
                    ),
                )
                await db.commit()
            except Exception:
                pass

    return json.dumps({
        "ok": True,
        "created": True,
        "kind": "automation",
        "automation": {
            "id": str(job.id),
            "job_id": str(job.job_id),
            "name": str(job.name),
            "schedule_kind": str(job.schedule_kind or schedule_kind),
            "cron_expr": job.cron_expr,
            "every_seconds": job.every_seconds,
            "run_at": job.run_at,
            "timezone": job.timezone,
            "enabled": bool(job.enabled),
            "agent_id": str(job.agent_id or agent_selection.agent_id),
            "agent_selection": agent_selection.source,
        },
        "url": f"/jobs?job={job.job_id}",
        "message": (
            f"Created scheduled job '{name}' "
            f"(id={job.job_id}, {schedule_kind}: {cron_expr or every_seconds or run_at})"
        ),
    }, ensure_ascii=False, default=str)


def runtime_workspace_chat_automation_scope(
    runtime_kwargs: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Return trusted Workspace Chat scope, never a model-selected workspace."""

    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    context = runtime_tool_call_context_from_kwargs(dict(runtime_kwargs or {}))
    if getattr(context.runtime_envelope, "surface", None) != ChatSurface.WORKSPACE_CHAT:
        return None
    if not context.workspace_id or not context.user_id:
        raise PermissionError("Workspace automation scope is unavailable")
    return context.workspace_id, context.user_id


async def _runtime_scoped_job_writable(
    db: Any,
    *,
    job: Any,
    entity_id: str,
    scope: tuple[str, str] | None,
) -> bool:
    if scope is None:
        return True
    if job is None or job.workspace_id != scope[0]:
        return False

    from packages.core.services.workspace_access import user_can_write_workspace_id

    if not await user_can_write_workspace_id(
        db, workspace_id=scope[0], entity_id=entity_id, user_id=scope[1],
    ):
        raise PermissionError("Workspace write access is required to manage automations")
    return True


async def _collaborator_job_workspace(db, *, entity_id: str, user_id: str | None, workspace_id: str | None) -> str | None:
    """The only Workspace a collaborator-involved listing may show ('' for none), else None."""
    from packages.core.services.collaborator_steering import collaborators_acting_now
    from packages.core.services.runtime_authorization.collaborator import is_runtime_collaborator

    if collaborators_acting_now(entity_id) or (
        user_id and await is_runtime_collaborator(str(user_id), entity_id, db=db)
    ):
        return str(workspace_id or "")
    return None


async def runtime_list_scheduled_jobs_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    runtime_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """List scheduled jobs through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import list_scheduled_jobs
    from packages.core.services.workspace_access import readable_workspace_ids_for_user

    scope = runtime_workspace_chat_automation_scope(runtime_kwargs)
    if not user_id and not workspace_id and not scope:
        return json.dumps({"ok": False, "error": "user or Workspace context is required"})

    async with async_session() as db:
        readable_workspace_ids = (
            await readable_workspace_ids_for_user(
                db,
                entity_id=entity_id,
                user_id=user_id,
                role=None,
            )
            if user_id
            else {scope[0] if scope else str(workspace_id)}
        )
        if scope:
            # None means tenant-wide read: the chat's own Workspace is readable.
            readable_workspace_ids = (
                {scope[0]}
                if readable_workspace_ids is None
                else readable_workspace_ids & {scope[0]}
            )
        listed_workspace_id = scope[0] if scope else (str(workspace_id) if not user_id and workspace_id else None)
        pinned = await _collaborator_job_workspace(db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id)
        if pinned is not None:
            # A collaborator's run lists its own Workspace's Jobs only.
            if not pinned or (listed_workspace_id and listed_workspace_id != pinned):
                return "No scheduled jobs found."
            listed_workspace_id, readable_workspace_ids = pinned, {pinned}
        jobs, _total = await list_scheduled_jobs(
            db,
            entity_id,
            workspace_id=listed_workspace_id,
            readable_workspace_ids=readable_workspace_ids,
        )

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
    user_id: str | None = None,
    runtime_workspace_id: str | None = None,
    query: str = "",
    workspace_id: str = "",
    workspace_ids: list[str] | None = None,
    enabled_only: bool = False,
    statuses: list[str] | None = None,
    agent_id: str = "",
    agent_ids: list[str] | None = None,
    schedule_kinds: list[str] | None = None,
    job_types: list[str] | None = None,
    execution_types: list[str] | None = None,
    last_statuses: list[str] | None = None,
    created_after: str = "",
    created_before: str = "",
    updated_after: str = "",
    updated_before: str = "",
    last_run_after: str = "",
    last_run_before: str = "",
    next_run_after: str = "",
    next_run_before: str = "",
    consecutive_errors_min: int | None = None,
    consecutive_errors_max: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Any = None,
    runtime_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Return one fully filtered scheduled-job page through Runtime."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import list_scheduled_jobs
    from packages.core.services.workspace_access import readable_workspace_ids_for_user

    scope = runtime_workspace_chat_automation_scope(runtime_kwargs)
    if scope:
        user_id = scope[1]
        runtime_workspace_id = scope[0]
    try:
        resolved_limit = max(1, min(int(limit or 50), 200))
        resolved_offset = max(0, min(int(offset or 0), 10_000))
        resolved_workspace_id = scope[0] if scope else str(workspace_id or "").strip() or None
        if scope:
            workspace_ids = []
        resolved_errors_min = (
            int(consecutive_errors_min)
            if consecutive_errors_min is not None
            else None
        )
        resolved_errors_max = (
            int(consecutive_errors_max)
            if consecutive_errors_max is not None
            else None
        )
        if (resolved_errors_min is not None and resolved_errors_min < 0) or (
            resolved_errors_max is not None and resolved_errors_max < 0
        ):
            raise ValueError("consecutive error bounds must be non-negative")
        invalid_schedule_kinds = sorted(
            {
                str(value or "").strip().lower()
                for value in (schedule_kinds or [])
            }
            - set(SCHEDULED_JOB_QUERY_KINDS)
        )
        if invalid_schedule_kinds:
            raise ValueError(
                "schedule_kinds contains unsupported values: "
                + ", ".join(invalid_schedule_kinds)
            )
        if not user_id and not runtime_workspace_id:
            return json.dumps({
                "ok": False,
                "error": "user or Workspace context is required",
            })
        session_context = nullcontext(db) if db is not None else async_session()
        async with session_context as session:
            if not user_id:
                if resolved_workspace_id and resolved_workspace_id != runtime_workspace_id:
                    return json.dumps({"ok": True, "automations": [], "total": 0, "count": 0})
                if workspace_ids and set(map(str, workspace_ids)) != {str(runtime_workspace_id)}:
                    return json.dumps({"ok": True, "automations": [], "total": 0, "count": 0})
                resolved_workspace_id = str(runtime_workspace_id)
                workspace_ids = None
                readable_workspace_ids = {str(runtime_workspace_id)}
            else:
                readable_workspace_ids = await readable_workspace_ids_for_user(
                    session,
                    entity_id=entity_id,
                    user_id=user_id,
                    role=None,
                )
            pinned = await _collaborator_job_workspace(
                session, entity_id=entity_id, user_id=user_id, workspace_id=runtime_workspace_id,
            )
            if pinned is not None:
                # A collaborator's run lists its own Workspace's Jobs only.
                requested = ({resolved_workspace_id} if resolved_workspace_id else set()) | set(map(str, workspace_ids or ()))
                if not pinned or requested - {pinned}:
                    return json.dumps({"ok": True, "automations": [], "total": 0, "count": 0})
                resolved_workspace_id, workspace_ids, readable_workspace_ids = pinned, None, {pinned}
            jobs, total = await list_scheduled_jobs(
                session,
                entity_id,
                enabled_only=enabled_only,
                workspace_id=resolved_workspace_id,
                workspace_ids=workspace_ids,
                search=str(query or "").strip() or None,
                statuses=statuses,
                agent_id=str(agent_id or "").strip() or None,
                agent_ids=agent_ids,
                schedule_kinds=schedule_kinds,
                job_types=job_types,
                execution_types=execution_types,
                last_statuses=last_statuses,
                created_after=created_after or None,
                created_before=created_before or None,
                updated_after=updated_after or None,
                updated_before=updated_before or None,
                last_run_after=last_run_after or None,
                last_run_before=last_run_before or None,
                next_run_after=next_run_after or None,
                next_run_before=next_run_before or None,
                consecutive_errors_min=resolved_errors_min,
                consecutive_errors_max=resolved_errors_max,
                limit=resolved_limit,
                offset=resolved_offset,
                readable_workspace_ids=readable_workspace_ids,
            )
    except (TypeError, ValueError) as exc:
        return json.dumps({
            "ok": False,
            "error": "invalid_automation_filters",
            "message": str(exc),
        })

    items = [
        {
            "job_id": job.job_id,
            "name": job.name,
            "schedule_kind": job.schedule_kind,
            "job_type": job.job_type,
            "execution_type": job.execution_type,
            "cron_expr": job.cron_expr,
            "every_seconds": job.every_seconds,
            "run_at": job.run_at,
            "timezone": job.timezone,
            "enabled": bool(job.enabled),
            "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
            "last_status": job.last_status,
            "consecutive_errors": int(job.consecutive_errors or 0),
            "workspace_id": job.workspace_id,
            "agent_id": job.agent_id,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
        for job in jobs
    ]
    next_offset = resolved_offset + len(items)
    has_more = next_offset < total
    return json.dumps({
        "ok": True,
        "automations": items,
        "total": total,
        "count": len(items),
        "limit": resolved_limit,
        "offset": resolved_offset,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    })


async def runtime_cancel_scheduled_job_action(
    *,
    entity_id: str,
    job_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    runtime_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Cancel a scheduled job through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services.scheduler_service import (
        ManagedAgentInitiativeDriverError,
        delete_scheduled_job,
        get_scheduled_job,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledAgentLineageDeletionBlockedError,
    )

    scope = runtime_workspace_chat_automation_scope(runtime_kwargs)
    async with async_session() as db:
        existing = await get_scheduled_job(db, job_id, entity_id)
        if existing is not None and not await _runtime_scoped_job_writable(
            db, job=existing, entity_id=entity_id, scope=scope,
        ):
            existing = None
        if existing is not None and not await _runtime_can_manage_scheduled_job(
            db,
            entity_id=entity_id,
            job=existing,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            existing = None
        if existing is None:
            return f"Job '{job_id}' not found."
        if existing is not None and getattr(existing, "agent_initiative_id", None):
            return json.dumps({
                "ok": False,
                "error": "managed_agent_initiative",
                "message": "Use the Agent Initiative controls to delete this rule.",
            })
        try:
            deleted = await delete_scheduled_job(db, job_id, entity_id)
        except ManagedAgentInitiativeDriverError:
            await db.rollback()
            return json.dumps({
                "ok": False,
                "error": "managed_agent_initiative",
                "message": "Use the Agent Initiative controls to delete this rule.",
            })
        except ScheduledAgentLineageDeletionBlockedError as exc:
            await db.rollback()
            return json.dumps({
                "ok": False,
                "error": exc.code,
                "message": str(exc),
            })
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
    actor_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    runtime_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Enable or disable a scheduled job through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.constants.task_actors import TaskActor
    from packages.core.services.scheduler_service import (
        get_scheduled_job,
        toggle_scheduled_job,
    )

    scope = runtime_workspace_chat_automation_scope(runtime_kwargs)
    async with async_session() as db:
        existing = await get_scheduled_job(db, job_id, entity_id)
        if existing is not None and not await _runtime_scoped_job_writable(
            db, job=existing, entity_id=entity_id, scope=scope,
        ):
            existing = None
        if existing is not None and not await _runtime_can_manage_scheduled_job(
            db,
            entity_id=entity_id,
            job=existing,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            existing = None
        if existing is None:
            return f"Job '{job_id}' not found."
        if existing is not None and getattr(existing, "agent_initiative_id", None):
            return json.dumps({
                "ok": False,
                "error": "managed_agent_initiative",
                "message": "Use mute, pause, or resume on the Agent Initiative.",
            })
        job = await toggle_scheduled_job(
            db,
            job_id,
            entity_id,
            enabled,
            changed_by_kind=(
                TaskActor.AGENT.value if actor_id else TaskActor.MANOR.value
            ),
            changed_by_id=actor_id,
            require_workspace=True,
            scope_workspace_id=scope[0] if scope else None,
            scope_user_id=scope[1] if scope else None,
        )
        if job:
            await db.commit()

    if job:
        return f"Job '{job_id}' is now {'enabled' if enabled else 'disabled'}."
    return f"Job '{job_id}' not found."


async def runtime_run_scheduled_job_now_action(
    *,
    entity_id: str,
    job_id: str,
    runtime_kwargs: dict[str, Any] | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Dispatch an immediate run for a scheduled job through Runtime."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.ai.runtime.execution_origin import runtime_tool_is_user_requested
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    scope = runtime_workspace_chat_automation_scope(runtime_kwargs)
    async with async_session() as db:
        query = select(ScheduledJob).where(
            ScheduledJob.job_id == job_id,
            ScheduledJob.entity_id == entity_id,
        )
        result = await db.execute(query.with_for_update() if scope else query)
        job = result.scalar_one_or_none()
        if not await _runtime_scoped_job_writable(
            db, job=job, entity_id=entity_id, scope=scope,
        ):
            return f"Job '{job_id}' not found."
        if not job or getattr(job, "agent_initiative_id", None):
            return f"Job '{job_id}' not found."
        context_kwargs = dict(runtime_kwargs or {})
        context = runtime_tool_call_context_from_kwargs(context_kwargs, entity_id=entity_id)
        resolved_user_id = user_id or context.user_id
        resolved_workspace_id = workspace_id or context.workspace_id
        if not await _runtime_can_manage_scheduled_job(
            db,
            entity_id=entity_id,
            job=job,
            user_id=resolved_user_id,
            workspace_id=resolved_workspace_id,
        ):
            return f"Job '{job_id}' not found."
        human_requested = bool(context.user_id) and await runtime_tool_is_user_requested(
            db, entity_id=entity_id, workspace_id=job.workspace_id, kwargs=context_kwargs,
        )
        job_db_id = job.id

    from packages.core.tasks.scheduler_tasks import dispatch_job_occurrence

    dispatch = await dispatch_job_occurrence(
        job_db_id=job_db_id,
        trigger_type="manual" if human_requested else "runtime",
        occurrence_key=f"{'manual' if human_requested else 'runtime'}:{generate_ulid()}",
        requesting_user_id=context.user_id if human_requested else None,
    )
    if dispatch is None:
        return f"Job '{job_id}' is unavailable for this execution."
    return f"Dispatched immediate run for job '{job_id}'."


async def _runtime_can_manage_scheduled_job(
    db: Any,
    *,
    entity_id: str,
    job: Any,
    user_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Apply row-level Workspace authorization after coarse tool permission."""

    job_workspace_id = str(getattr(job, "workspace_id", "") or "")
    if user_id:
        if not job_workspace_id:
            return True
        from packages.core.services.workspace_access import user_can_write_workspace_id

        return await user_can_write_workspace_id(
            db,
            workspace_id=job_workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=None,
        )
    return bool(workspace_id and job_workspace_id == str(workspace_id))


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

