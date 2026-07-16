"""Scheduled job tools — let agents create, list, cancel, and run automations."""
from __future__ import annotations

from packages.core.ai.runtime.scheduling import (
    SCHEDULED_FILE_OUTPUT_KINDS,
    runtime_cancel_scheduled_job_action,
    runtime_create_scheduled_job_action,
    runtime_list_scheduled_jobs_action,
    runtime_query_scheduled_jobs_action,
    runtime_run_scheduled_job_now_action,
    runtime_toggle_scheduled_job_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


async def _create_job_handler(
    entity_id: str = "", name: str = "", schedule_kind: str = "cron",
    payload_message: str = "", cron_expr: str = "", every_seconds: int = 0,
    run_at: str = "", agent_id: str = "", timezone: str = "UTC", **kwargs,
):
    """Create a new scheduled job."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_create_scheduled_job_action(
        entity_id=entity_id,
        name=name,
        schedule_kind=schedule_kind,
        payload_message=payload_message,
        cron_expr=cron_expr,
        every_seconds=every_seconds,
        run_at=run_at,
        agent_id=agent_id,
        timezone_str=timezone,
        workspace_id=runtime_context.workspace_id,
        conversation_id=runtime_context.conversation_id,
        user_id=runtime_context.user_id,
        default_delivery_mode=kwargs.get("default_delivery_mode"),
        execution_target=kwargs.get("execution_target"),
        output_kind=str(kwargs.get("output_kind") or ""),
        file_kind=str(kwargs.get("file_kind") or ""),
        artifact_kind=str(kwargs.get("artifact_kind") or ""),
        requires_generated_file=kwargs.get("requires_generated_file"),
        requires_file_deliverable=kwargs.get("requires_file_deliverable"),
        max_turns=kwargs.get("max_turns"),
    )


async def _list_jobs_handler(entity_id: str = "", **kwargs):
    """List all scheduled jobs for this entity."""
    return await runtime_list_scheduled_jobs_action(entity_id=entity_id)


async def _query_jobs_handler(entity_id: str = "", **kwargs):
    """Return structured scheduled-job data for read-only consumers."""
    return await runtime_query_scheduled_jobs_action(
        entity_id=entity_id,
        query=str(kwargs.get("query") or ""),
        workspace_id=str(kwargs.get("workspace_id") or ""),
        enabled_only=bool(kwargs.get("enabled_only", False)),
        limit=int(kwargs.get("limit") or 50),
    )


async def _cancel_job_handler(entity_id: str = "", job_id: str = "", **kwargs):
    """Cancel/delete a scheduled job by ID."""
    return await runtime_cancel_scheduled_job_action(entity_id=entity_id, job_id=job_id)


async def _toggle_job_handler(entity_id: str = "", job_id: str = "", enabled: bool = True, **kwargs):
    """Enable or disable a scheduled job."""
    return await runtime_toggle_scheduled_job_action(
        entity_id=entity_id,
        job_id=job_id,
        enabled=enabled,
    )


async def _run_job_now_handler(entity_id: str = "", job_id: str = "", **kwargs):
    """Force-run a scheduled job immediately."""
    return await runtime_run_scheduled_job_now_action(entity_id=entity_id, job_id=job_id)


def get_tools():
    tools = [
        (
            {
                "type": "function",
                "function": {
                    "name": "create_scheduled_job",
                    "description": "Create a new scheduled automation. Supports cron expressions, fixed intervals, or one-time execution. An AI execution procedure will be auto-generated.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Human-readable name for the job"},
                            "schedule_kind": {"type": "string", "enum": ["cron", "every", "at"], "description": "cron=cron expression, every=fixed interval, at=one-time"},
                            "payload_message": {"type": "string", "description": "What the agent should do when this job runs (plain text)"},
                            "cron_expr": {"type": "string", "description": "5-field cron expression (for schedule_kind=cron). E.g. '0 9 * * *' for daily at 9am"},
                            "every_seconds": {"type": "integer", "description": "Interval in seconds (for schedule_kind=every). E.g. 3600 for hourly"},
                            "run_at": {"type": "string", "description": "ISO datetime for one-shot (for schedule_kind=at). E.g. '2026-04-25T09:00:00'"},
                            "agent_id": {"type": "string", "description": "Agent ID to execute (use 'manor-master' for Manor AI)"},
                            "timezone": {"type": "string", "description": "Timezone for the schedule. Default: UTC"},
                            "output_kind": {
                                "type": "string",
                                "enum": list(SCHEDULED_FILE_OUTPUT_KINDS),
                                "description": "Structured deliverable kind when the scheduled run must create a file/media artifact. Use 'video' for video generation. Do not infer from payload text.",
                            },
                            "requires_generated_file": {
                                "type": "boolean",
                                "description": "Set true when success requires a generated file/media artifact, not just a text report.",
                            },
                            "max_turns": {
                                "type": "integer",
                                "description": "Optional explicit agent turn budget for this automation.",
                            },
                            "execution_target": {
                                "type": "object",
                                "description": "Optional structured execution metadata. Prefer fixed fields like output_kind, requires_generated_file, and max_turns.",
                                "additionalProperties": True,
                            },
                        },
                        "required": ["name", "schedule_kind", "payload_message"],
                    },
                },
            },
            _create_job_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_jobs",
                    "description": "List all scheduled automations for this entity. Shows job ID, name, schedule, status, and last run time.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            _list_jobs_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "query_scheduled_jobs",
                    "description": (
                        "Return structured, read-only scheduled automation data for the current entity, "
                        "including schedule, enabled state, recent status, and error count."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Optional name or job ID filter.",
                            },
                            "workspace_id": {
                                "type": "string",
                                "description": "Optional workspace ID filter.",
                            },
                            "enabled_only": {
                                "type": "boolean",
                                "description": "Return only enabled automations when true.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                            },
                        },
                    },
                },
            },
            _query_jobs_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled_job",
                    "description": "Delete/cancel a scheduled job by its job_id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string", "description": "The job_id to cancel"},
                        },
                        "required": ["job_id"],
                    },
                },
            },
            _cancel_job_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "toggle_scheduled_job",
                    "description": "Enable or disable a scheduled job without deleting it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string", "description": "The job_id to toggle"},
                            "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
                        },
                        "required": ["job_id", "enabled"],
                    },
                },
            },
            _toggle_job_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "run_scheduled_job_now",
                    "description": "Force-run a scheduled job immediately, regardless of its schedule.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string", "description": "The job_id to run"},
                        },
                        "required": ["job_id"],
                    },
                },
            },
            _run_job_now_handler,
        ),
    ]
    return tools
