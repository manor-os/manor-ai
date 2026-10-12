"""Scheduled job tools for creating, querying, toggling, running, and cancelling."""
from __future__ import annotations

from packages.core.ai.runtime.scheduling import (
    SCHEDULED_JOB_CREATE_KINDS,
    SCHEDULED_JOB_QUERY_KINDS,
    SCHEDULED_FILE_OUTPUT_KINDS,
    runtime_cancel_scheduled_job_action,
    runtime_create_scheduled_job_action,
    runtime_list_scheduled_jobs_action,
    runtime_query_scheduled_jobs_action,
    runtime_run_scheduled_job_now_action,
    runtime_toggle_scheduled_job_action,
)
from packages.core.ai.runtime.tool_context import (
    runtime_agent_subscription_id_from_context,
    runtime_trusted_workspace_id_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.constants.agents import MANOR_AGENT_ID


async def _create_job_handler(
    entity_id: str = "", name: str = "", schedule_kind: str = "cron",
    payload_message: str = "", cron_expr: str = "", every_seconds: int = 0,
    run_at: str = "", agent_id: str = "", timezone: str = "UTC", **kwargs,
):
    """Create a new scheduled job."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    caller_agent_id = runtime_context.agent_id or MANOR_AGENT_ID
    return await runtime_create_scheduled_job_action(
        entity_id=entity_id,
        name=name,
        schedule_kind=schedule_kind,
        payload_message=payload_message,
        cron_expr=cron_expr,
        every_seconds=every_seconds,
        run_at=run_at,
        agent_id=agent_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=(
            runtime_agent_subscription_id_from_context(runtime_context)
        ),
        timezone_str=timezone,
        workspace_id=trusted_workspace_id,
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
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    return await runtime_list_scheduled_jobs_action(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        workspace_id=trusted_workspace_id,
    )


async def _query_jobs_handler(entity_id: str = "", **kwargs):
    """Return structured scheduled-job data for read-only consumers."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    return await runtime_query_scheduled_jobs_action(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        runtime_workspace_id=trusted_workspace_id,
        query=str(kwargs.get("query") or ""),
        workspace_id=str(kwargs.get("workspace_id") or ""),
        workspace_ids=list(kwargs.get("workspace_ids") or []),
        enabled_only=bool(kwargs.get("enabled_only", False)),
        statuses=list(kwargs.get("statuses") or []),
        agent_id=str(kwargs.get("agent_id") or ""),
        agent_ids=list(kwargs.get("agent_ids") or []),
        schedule_kinds=list(kwargs.get("schedule_kinds") or []),
        job_types=list(kwargs.get("job_types") or []),
        execution_types=list(kwargs.get("execution_types") or []),
        last_statuses=list(kwargs.get("last_statuses") or []),
        created_after=str(kwargs.get("created_after") or ""),
        created_before=str(kwargs.get("created_before") or ""),
        updated_after=str(kwargs.get("updated_after") or ""),
        updated_before=str(kwargs.get("updated_before") or ""),
        last_run_after=str(kwargs.get("last_run_after") or ""),
        last_run_before=str(kwargs.get("last_run_before") or ""),
        next_run_after=str(kwargs.get("next_run_after") or ""),
        next_run_before=str(kwargs.get("next_run_before") or ""),
        consecutive_errors_min=kwargs.get("consecutive_errors_min"),
        consecutive_errors_max=kwargs.get("consecutive_errors_max"),
        limit=int(kwargs.get("limit") or 50),
        offset=int(kwargs.get("offset") or 0),
        runtime_kwargs=kwargs,
    )


async def _cancel_job_handler(entity_id: str = "", job_id: str = "", **kwargs):
    """Cancel/delete a scheduled job by ID."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    return await runtime_cancel_scheduled_job_action(
        entity_id=entity_id,
        job_id=job_id,
        user_id=runtime_context.user_id,
        workspace_id=trusted_workspace_id,
    )


async def _toggle_job_handler(entity_id: str = "", job_id: str = "", enabled: bool = True, **kwargs):
    """Enable or disable a scheduled job."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    return await runtime_toggle_scheduled_job_action(
        entity_id=entity_id,
        job_id=job_id,
        enabled=enabled,
        actor_id=runtime_context.agent_id,
        user_id=runtime_context.user_id,
        workspace_id=trusted_workspace_id,
    )


async def _run_job_now_handler(entity_id: str = "", job_id: str = "", **kwargs):
    """Force-run a scheduled job immediately."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    trusted_workspace_id = runtime_trusted_workspace_id_from_context(runtime_context)
    return await runtime_run_scheduled_job_now_action(
        entity_id=entity_id,
        job_id=job_id,
        runtime_kwargs=kwargs,
        user_id=runtime_context.user_id,
        workspace_id=trusted_workspace_id,
    )


def get_tools():
    tools = [
        (
            {
                "type": "function",
                "function": {
                    "name": "create_scheduled_job",
                    "description": (
                        "Background-only scheduled automation. In Workspace Chat, "
                        "use this tool for recurring Workspace work. For a scheduled "
                        "or conditional update delivered to private Agent or Manor Chat, "
                        "use create_agent_initiative; do not use this tool. This tool "
                        "supports cron expressions, fixed intervals, or one-time "
                        "execution. Omit agent_id to use the calling Agent (Manor AI "
                        "becomes manor-master). To select another Agent, first call "
                        "query_entity_agents or manor(action='list_agents') and copy "
                        "an exact active returned id; a missing, stale, inactive, or "
                        "unavailable selection falls back to the calling Agent. The "
                        "Manor id is valid only when Manor is already the caller. "
                        "When enabled, it queues generation of an AI execution procedure."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Human-readable name for the job"},
                            "schedule_kind": {"type": "string", "enum": list(SCHEDULED_JOB_CREATE_KINDS), "description": "cron=cron expression, every=fixed interval, at=one-time"},
                            "payload_message": {"type": "string", "description": "What the agent should do when this job runs (plain text)"},
                            "cron_expr": {"type": "string", "description": "5-field cron expression (for schedule_kind=cron). E.g. '0 9 * * *' for daily at 9am"},
                            "every_seconds": {"type": "integer", "description": "Interval in seconds (for schedule_kind=every). E.g. 3600 for hourly"},
                            "run_at": {"type": "string", "description": "ISO datetime for one-shot (for schedule_kind=at). E.g. '2026-04-25T09:00:00'"},
                            "agent_id": {
                                "type": "string",
                                "description": (
                                    "Optional exact Agent id copied from query_entity_agents "
                                    "or manor(action='list_agents'). Omit to use the calling "
                                    "Agent. Invalid or unavailable ids safely fall back to it; "
                                    "a custom Agent cannot select Manor."
                                ),
                            },
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
                    "description": "List background scheduled automations for this entity. Proactive Chat rules are listed with list_agent_initiatives instead.",
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
                        "Query background scheduled automations with status, errors, and all other filters in one call. "
                        "Proactive Chat rules belong to Agent Initiatives. Values in plural "
                        "filters are ORed; different filters and inclusive ranges are ANDed. Filtering "
                        "happens before pagination and total is exact. Do not query once per status."
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
                                "description": "Legacy exact Workspace ID filter.",
                            },
                            "workspace_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 50,
                                "uniqueItems": True,
                                "description": "Match any listed Workspace ID (OR).",
                            },
                            "enabled_only": {
                                "type": "boolean",
                                "description": "Legacy enabled-only filter.",
                            },
                            "statuses": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["enabled", "paused", "attention"],
                                },
                                "minItems": 1,
                                "maxItems": 3,
                                "uniqueItems": True,
                                "description": "Match any effective status (OR).",
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "Legacy exact Agent ID filter.",
                            },
                            "agent_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 50,
                                "uniqueItems": True,
                                "description": "Match any listed Agent ID (OR).",
                            },
                            "schedule_kinds": {
                                "type": "array",
                                "items": {"type": "string", "enum": list(SCHEDULED_JOB_QUERY_KINDS)},
                                "minItems": 1,
                                "maxItems": len(SCHEDULED_JOB_QUERY_KINDS),
                                "uniqueItems": True,
                                "description": (
                                    "Match canonical schedule kinds cron/every/at or "
                                    "legacy persisted aliases interval/once."
                                ),
                            },
                            "job_types": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 10,
                                "uniqueItems": True,
                            },
                            "execution_types": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 10,
                                "uniqueItems": True,
                            },
                            "last_statuses": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 10,
                                "uniqueItems": True,
                                "description": "Match any persisted last-run status (OR).",
                            },
                            "consecutive_errors_min": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "Inclusive lower error-count bound.",
                            },
                            "consecutive_errors_max": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "Inclusive upper error-count bound.",
                            },
                            **{
                                field: {
                                    "type": "string",
                                    "format": "date-time",
                                    "description": f"Inclusive {label} bound.",
                                }
                                for field, label in (
                                    ("created_after", "created-at lower"),
                                    ("created_before", "created-at upper"),
                                    ("updated_after", "updated-at lower"),
                                    ("updated_before", "updated-at upper"),
                                    ("last_run_after", "last-run lower"),
                                    ("last_run_before", "last-run upper"),
                                    ("next_run_after", "next-run lower"),
                                    ("next_run_before", "next-run upper"),
                                )
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                            },
                            "offset": {
                                "type": "integer",
                                "minimum": 0,
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
                    "description": "Delete/cancel a background scheduled job by its job_id. Use delete_agent_initiative for a proactive Chat rule.",
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
                    "description": "Enable or disable a background scheduled job. Use mute/pause/resume_agent_initiative for a proactive Chat rule.",
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
                    "description": "Force-run a background scheduled job immediately. Use run_agent_initiative_now for a proactive Chat rule.",
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
