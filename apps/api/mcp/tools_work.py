"""Arrange a Workspace's work: read its tasks, manage its scheduled jobs."""
from __future__ import annotations

import re
import secrets
from typing import Any

from apps.api.mcp.registry import Tool, ToolContext, ToolError, boolean, obj, require, string

_TASKS = "/api/v1/tasks"
_JOBS = "/api/v1/jobs"

_TASK_LIST_KEYS = ("id", "title", "status", "priority", "task_type", "workspace_id",
                   "owner_service_key", "owner_agent_name", "agent_name", "assignee_name",
                   "parent_task_id", "current_blocker", "execution_active", "deadline",
                   "scheduled_at", "created_at", "updated_at", "status_changed_at", "completed_at")
_LOG_KEYS = ("id", "log_type", "created_at", "created_by", "author_agent_name", "attachments")
_LOG_CONTENT_CHARS = 2000
_JOB_LIST_KEYS = ("id", "job_id", "name", "workspace_id", "schedule_kind", "cron_expr",
                  "every_seconds", "run_at", "timezone", "execution_type", "execution_target",
                  "agent_id", "enabled", "workspace_blocked", "last_run_at", "last_status",
                  "last_error", "consecutive_errors", "can_manage")
_JOB_FIELDS = ("name", "job_type", "schedule_kind", "cron_expr", "every_seconds", "run_at",
               "timezone", "payload_message", "agent_id", "execution_type", "execution_target",
               "default_delivery_mode")
_JOB_CREATE_ONLY_FIELDS = ("workspace_id", "stop_after")
_JOB_TYPE_BY_KIND = {"cron": "cron", "every": "interval", "at": "once"}


def _limit(args: dict, default: int) -> int:
    return min(max(int(args.get("limit") or default), 1), 200)


async def list_tasks(ctx: ToolContext, args: dict) -> Any:
    page = await ctx.api.get(
        _TASKS, workspace_id=require(args, "workspace_id"), status=args.get("status"),
        attention=bool(args.get("attention")) or None, parent_task_id=args.get("parent_task_id"),
        limit=_limit(args, 50), offset=args.get("offset"),
    )
    return {"total": page.get("total"),
            "tasks": [{key: task.get(key) for key in _TASK_LIST_KEYS} for task in page.get("items") or []]}


def _log(entry: dict) -> dict:
    content = entry.get("content") or ""
    if len(content) > _LOG_CONTENT_CHARS:
        content = content[:_LOG_CONTENT_CHARS] + f"… [{len(content) - _LOG_CONTENT_CHARS} more chars]"
    return {**{key: entry.get(key) for key in _LOG_KEYS}, "content": content}


async def get_task(ctx: ToolContext, args: dict) -> Any:
    task_id = require(args, "task_id")
    task = await ctx.api.get(f"{_TASKS}/{task_id}")
    logs = await ctx.api.get(f"{_TASKS}/{task_id}/logs") or []
    log_limit = min(max(int(args.get("log_limit") or 20), 0), 200)
    return {
        "task": task,
        "pending_approvals": {"current_blocker": task.get("current_blocker")},
        "log_count": len(logs),
        "recent_logs": [_log(entry) for entry in logs[:log_limit]],
    }


async def list_scheduled_jobs(ctx: ToolContext, args: dict) -> Any:
    page = await ctx.api.get(
        _JOBS, workspace_id=args.get("workspace_id"), status=args.get("status"),
        search=args.get("search"), limit=_limit(args, 50), offset=args.get("offset"),
    )
    return {
        "total": page.get("total"), "enabled_total": page.get("enabled_total"),
        "attention_total": page.get("attention_total"),
        "jobs": [{key: job.get(key) for key in _JOB_LIST_KEYS} for job in page.get("items") or []],
    }


async def get_scheduled_job(ctx: ToolContext, args: dict) -> Any:
    job_id = require(args, "job_id")
    job = await ctx.api.get(f"{_JOBS}/{job_id}")
    runs = await ctx.api.get(f"{_JOBS}/{job_id}/runs", limit=min(max(int(args.get("run_limit") or 10), 1), 50))
    return {"job": job, "recent_runs": runs}


def _new_job_key(args: dict) -> str:
    """Job keys are unique across Manor; suffix a readable slug to avoid collisions."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(args.get("name") or "job").lower()).strip("-")[:60] or "job"
    return f"{slug}-{secrets.token_hex(4)}"


async def upsert_scheduled_job(ctx: ToolContext, args: dict) -> Any:
    body = {key: args[key] for key in _JOB_FIELDS if key in args}
    if "schedule_kind" in body and "job_type" not in body:
        # Same pairing as the in-product scheduling tool; the UI reads job_type.
        body["job_type"] = _JOB_TYPE_BY_KIND.get(body["schedule_kind"], "once")
    job_id = args.get("job_id")
    if job_id:
        extra = [key for key in _JOB_CREATE_ONLY_FIELDS if key in args]
        if extra:
            raise ToolError(f"{', '.join(extra)} can only be set when creating a job")
        return await ctx.api.put(f"{_JOBS}/{job_id}", body)
    require(args, "name")
    body.update({key: args[key] for key in _JOB_CREATE_ONLY_FIELDS if key in args})
    return await ctx.api.post(_JOBS, {**body, "job_id": args.get("new_job_key") or _new_job_key(args)})


async def toggle_scheduled_job(ctx: ToolContext, args: dict) -> Any:
    job_id = require(args, "job_id")
    if not isinstance(args.get("enabled"), bool):
        raise ToolError("'enabled' must be true or false")
    job = await ctx.api.post(f"{_JOBS}/{job_id}/toggle", {"enabled": args["enabled"]})
    return {key: job.get(key) for key in _JOB_LIST_KEYS}


async def run_scheduled_job(ctx: ToolContext, args: dict) -> Any:
    job_id = require(args, "job_id")
    key = args.get("idempotency_key")
    queued = await ctx.api.post(
        f"{_JOBS}/{job_id}/run_now", headers={"Idempotency-Key": key} if key else None,
    )
    return {**queued, "note": "Queued. Read the outcome with get_scheduled_job (recent_runs)."}


_WORKSPACE_ID = string("Workspace id (from list_workspaces).")
_JOB_ID = string("Scheduled job id or job key (from list_scheduled_jobs).")
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}

TOOLS = [
    Tool("list_tasks", "List tasks",
         "A Workspace's tasks, newest first, with status, owner service/agent and any current "
         "blocker (approval, input or capability). attention=true lists only tasks waiting on a "
         "person.",
         list_tasks,
         properties={"workspace_id": _WORKSPACE_ID,
                     "status": string("Only this status, e.g. pending, in_progress, blocked, completed, failed."),
                     "attention": boolean("Only tasks that need a person's attention.", False),
                     "parent_task_id": string("Only direct subtasks of this task."),
                     "limit": {**_LIMIT, "default": 50},
                     "offset": {"type": "integer", "minimum": 0}},
         required=("workspace_id",), read_only=True, idempotent=True),
    Tool("get_task", "Get task",
         "One task: its details and result (actual_output), what blocks it (its current "
         "blocker, e.g. a pending approval or question), and its most recent activity log "
         "entries as evidence.",
         get_task,
         properties={"task_id": string("Task id (from list_tasks)."),
                     "log_limit": {"type": "integer", "minimum": 0, "maximum": 200, "default": 20,
                                   "description": "How many recent log entries to include."}},
         required=("task_id",), read_only=True, idempotent=True),
    Tool("list_scheduled_jobs", "List scheduled jobs",
         "Scheduled jobs (recurring or one-off automations): schedule, what each runs, whether "
         "it is enabled, and its last run status and error.",
         list_scheduled_jobs,
         properties={"workspace_id": string("Only jobs of this Workspace."),
                     "status": {"enum": ["all", "enabled", "paused", "attention"], "default": "all"},
                     "search": string("Match job names."),
                     "limit": {**_LIMIT, "default": 50},
                     "offset": {"type": "integer", "minimum": 0}},
         read_only=True, idempotent=True),
    Tool("get_scheduled_job", "Get scheduled job",
         "One scheduled job with its full configuration and its most recent runs "
         "(status, result, error, duration).",
         get_scheduled_job,
         properties={"job_id": _JOB_ID,
                     "run_limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
         required=("job_id",), read_only=True, idempotent=True),
    Tool("upsert_scheduled_job", "Create or update scheduled job",
         "Create (no job_id) or update a scheduled job. Schedule: schedule_kind cron + cron_expr "
         "(with timezone), every + every_seconds, or at + run_at (ISO time). What it runs: "
         "execution_type workflow with execution_target {workflow_id}, skill with {skill_id}, "
         "or agent with agent_id + payload_message. On update only the fields you pass change.",
         upsert_scheduled_job,
         properties={"job_id": _JOB_ID | {"description": "Update this job; omit to create."},
                     "name": string("Job name (required to create)."),
                     "workspace_id": string("Own the job by this Workspace (create only)."),
                     "schedule_kind": {"enum": ["cron", "every", "at"]},
                     "cron_expr": string("Cron expression, e.g. 0 9 * * 1-5."),
                     "every_seconds": {"type": "number", "minimum": 1},
                     "run_at": string("ISO 8601 time for a one-off run."),
                     "timezone": string("IANA timezone, e.g. Europe/London (default UTC)."),
                     "execution_type": string("workflow, skill or agent."),
                     "execution_target": obj("Target ids, e.g. {\"workflow_id\": \"…\"}."),
                     "agent_id": string("Agent that runs payload_message (execution_type agent)."),
                     "payload_message": string("Instruction sent to the agent on each run."),
                     "stop_after": string("ISO time after which the job stops (create only)."),
                     "new_job_key": string("Unique key for a new job; generated from the name if omitted.")},
         destructive=True),
    Tool("toggle_scheduled_job", "Enable or pause scheduled job",
         "Enable or pause a scheduled job. A job of a Workspace that is not active stays paused.",
         toggle_scheduled_job,
         properties={"job_id": _JOB_ID, "enabled": boolean("true to enable, false to pause.")},
         required=("job_id", "enabled"), destructive=True, idempotent=True),
    Tool("run_scheduled_job", "Run scheduled job now",
         "Queue one run of a scheduled job now, outside its schedule. The run does what the job "
         "does (it may message people or call external services). Reuse idempotency_key when "
         "retrying so the run is queued once.",
         run_scheduled_job,
         properties={"job_id": _JOB_ID,
                     "idempotency_key": string("Letters, digits and ._:- (max 128).", maxLength=128,
                                               pattern=r"^[A-Za-z0-9._:-]+$")},
         required=("job_id",), destructive=True, open_world=True),
]
