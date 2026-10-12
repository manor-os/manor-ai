"""Waits that park a Task agent loop instead of holding its worker.

``wait_until`` and the loop's ``wait_media_jobs`` return a
``RuntimeToolSuspension`` while their condition is not met. The worker parks
the pending call (phase WAITING_TIMER / WAITING_JOB, a delayed wake) and the
next wake re-executes that exact call before any model round: it either
parks again or returns its result. A wake can arrive early (any other stage
replaces the Task's single wake row), so every re-execution re-checks.

The wait's frozen terms (absolute wake time, job ids, deadline) live in the
run checkpoint under ``task_loop_wait``; a re-executed call reads them from
there, never from model-supplied arguments.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from packages.core.ai.runtime.control import RuntimeToolSuspension
from packages.core.services.task_coordinator_wakeup import TASK_LOOP_WAIT_KEY

WAIT_UNTIL_TOOL_NAME = "wait_until"
WAIT_MEDIA_JOBS_TOOL_NAME = "wait_media_jobs"
TASK_LOOP_WAIT_VERSION = 1
WAITING_TIMER_KIND = "waiting_timer"
WAITING_JOB_KIND = "waiting_job"
WAIT_KINDS = frozenset({WAITING_TIMER_KIND, WAITING_JOB_KIND})

WAIT_MIN_SECONDS = 60
# Workers' clocks differ slightly: a wake this close to its time is due, so
# it is never re-parked for a second or two.
WAKE_SKEW = timedelta(seconds=2)
WAIT_MAX = timedelta(days=90)
# Media jobs fail by themselves after about 30 minutes; a parked wait gives
# them twice that before it returns timed_out to the model.
JOB_WAIT_DEFAULT = timedelta(minutes=60)
# Fallback poll while jobs run, in case a completion wake is lost.
JOB_POLL_MAX_SECONDS = 300
# One wait covers at most this many job ids; the rest are reported dropped.
MAX_WAIT_JOB_IDS = 50
_MAX_DROPPED_JOB_IDS = 200
_REASON_MAX_CHARS = 160

WAIT_UNTIL_TOOL = {
    "type": "function",
    "function": {
        "name": WAIT_UNTIL_TOOL_NAME,
        "description": (
            "Pause this Task until a later time without holding a worker, then "
            "continue from here. Use it to wait for something outside the Task: a "
            "reply, a scheduled event, a page or file that updates later. Give "
            "either `at` (an ISO 8601 time; UTC when it has no offset) or `seconds`. "
            "The wait is between 1 minute and 90 days. The result says when the "
            "Task woke; re-check external state after waking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "at": {
                    "type": "string",
                    "description": "When to continue, e.g. 2026-10-12T09:00:00Z.",
                },
                "seconds": {
                    "type": "number",
                    "description": "How long to wait from now, in seconds (60 to 7776000).",
                },
                "reason": {
                    "type": "string",
                    "description": "What the Task is waiting for; shown on the Task.",
                },
            },
            "required": ["reason"],
        },
    },
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def tool_call_id(arguments: dict[str, Any]) -> str:
    """The runtime's id for the executing call (never model-supplied)."""

    return str(arguments.get("_runtime_tool_call_id_from_context") or "").strip()


def resumed_wait(wait: Any, call_id: str, kind: str) -> dict[str, Any] | None:
    """The parked wait this exact call owns, or None for a fresh call."""

    if (
        isinstance(wait, dict)
        and call_id
        and wait.get("version") == TASK_LOOP_WAIT_VERSION
        and wait.get("kind") == kind
        and wait.get("tool_call_id") == call_id
    ):
        return wait
    return None


def reason_text(value: Any) -> str:
    return (value.strip() if isinstance(value, str) else "")[:_REASON_MAX_CHARS]


def wait_until_target(arguments: dict[str, Any], now: datetime) -> tuple[datetime | None, str | None]:
    """A fresh call's absolute wake time, clamped to [now + 60 s, now + 90 d]."""

    has_at = isinstance(arguments.get("at"), str) and arguments["at"].strip()
    seconds = arguments.get("seconds")
    has_seconds = seconds is not None and not isinstance(seconds, bool)
    if has_at and has_seconds:
        return None, "Give either at or seconds, not both."
    if has_at:
        target = parse_time(arguments["at"])
        if target is None:
            return None, "at must be an ISO 8601 time such as 2026-10-12T09:00:00Z."
    elif has_seconds:
        try:
            amount = float(seconds)
        except (TypeError, ValueError):
            return None, "seconds must be a number."
        if amount != amount or amount in (float("inf"), float("-inf")):
            return None, "seconds must be a finite number."
        target = now + timedelta(seconds=max(0.0, min(amount, WAIT_MAX.total_seconds())))
    else:
        return None, "Give at (an ISO 8601 time) or seconds."
    return min(max(target, now + timedelta(seconds=WAIT_MIN_SECONDS)), now + WAIT_MAX), None


def timer_suspension(
    *,
    call_id: str,
    wake_at: datetime,
    requested_at: datetime,
    reason: str,
    now: datetime,
) -> RuntimeToolSuspension:
    return RuntimeToolSuspension(
        kind=WAITING_TIMER_KIND,
        reservation_id="",
        poll_after_seconds=max(0, int((wake_at - now).total_seconds())),
        deadline_at=wake_at,
        reason=reason or WAIT_UNTIL_TOOL_NAME,
        payload={"wake_at": iso(wake_at), "requested_at": iso(requested_at)},
    ).with_checkpoint_tool_call(
        tool_call_id=call_id,
        tool_name=WAIT_UNTIL_TOOL_NAME,
        # The frozen absolute time, so the transcript shows what was asked.
        arguments={"at": iso(wake_at), "reason": reason},
    )


def wait_is_due(wake_at: datetime | None, now: datetime) -> bool:
    return wake_at is None or now >= wake_at - WAKE_SKEW


def woke_result(*, wake_at: datetime, requested_at: datetime, reason: str, now: datetime) -> str:
    return json.dumps({
        "status": "woke",
        "reason": reason,
        "requested_at": iso(requested_at),
        "wake_at": iso(wake_at),
        "woke_at": iso(now),
        "slept_seconds": max(0, int((now - requested_at).total_seconds())),
        "late_seconds": max(0, int((now - wake_at).total_seconds())),
    })


def parked_wait_state(
    control: dict[str, Any],
    *,
    prior: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any] | None:
    """The checkpoint's ``task_loop_wait`` for a parked suspension; None if malformed."""

    kind = control.get("kind")
    checkpoint = control.get("checkpoint") if isinstance(control.get("checkpoint"), dict) else {}
    pending = checkpoint.get("pending_tool_call") if isinstance(checkpoint.get("pending_tool_call"), dict) else {}
    call_id = str(pending.get("id") or "").strip()
    payload = control.get("payload") if isinstance(control.get("payload"), dict) else {}
    if kind not in WAIT_KINDS or not call_id:
        return None
    owned = resumed_wait(prior, call_id, kind)
    wait: dict[str, Any] = {
        "version": TASK_LOOP_WAIT_VERSION,
        "kind": kind,
        "tool_call_id": call_id,
        "tool_name": str(pending.get("name") or "")[:100],
        "reason": reason_text(control.get("reason")),
        "parked_at": (owned or {}).get("parked_at") or iso(now),
    }
    if kind == WAITING_TIMER_KIND:
        wake_at = parse_time(payload.get("wake_at"))
        requested_at = parse_time(payload.get("requested_at")) or now
        if wake_at is None:
            return None
        wait.update({"wake_at": iso(wake_at), "requested_at": iso(requested_at)})
        return wait
    job_ids = payload.get("job_ids")
    deadline_at = parse_time(payload.get("deadline_at"))
    wait["requested_at"] = iso(parse_time(payload.get("requested_at")) or now)
    if (
        not isinstance(job_ids, list)
        or not job_ids
        or len(job_ids) > MAX_WAIT_JOB_IDS
        or not all(isinstance(item, str) and item for item in job_ids)
        or deadline_at is None
    ):
        return None
    wait.update({"job_ids": list(job_ids), "deadline_at": iso(deadline_at)})
    for key, limit in (("requested_job_ids", MAX_WAIT_JOB_IDS), ("dropped_job_ids", _MAX_DROPPED_JOB_IDS)):
        values = payload.get(key)
        if isinstance(values, list) and values:
            wait[key] = [str(item) for item in values if item][:limit]
    return wait


def wait_log_line(wait: dict[str, Any]) -> str:
    reason = f": {wait['reason']}" if wait.get("reason") else ""
    if wait["kind"] == WAITING_TIMER_KIND:
        return f"Waiting until {wait['wake_at']}{reason}."
    count = len(wait.get("job_ids") or [])
    return f"Waiting for {count} media job(s) until {wait['deadline_at']} at the latest{reason}."


async def media_jobs_settled(db: Any, *, entity_id: str, job_ids: list[str]) -> bool:
    """True when every listed job is terminal (or no longer exists)."""

    from packages.core.models.media_job import MediaJob, MediaJobStatus

    open_statuses = (MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value)
    still_open = (await db.execute(
        select(MediaJob.id).where(
            MediaJob.entity_id == entity_id,
            MediaJob.id.in_(list(job_ids)),
            MediaJob.status.in_(open_statuses),
        ).limit(1)
    )).scalar_one_or_none()
    return still_open is None


def job_wait_deadline(arguments: dict[str, Any], now: datetime) -> datetime:
    """A fresh call's deadline: its timeout_seconds (at most 60 min), else 60 min."""

    limit = JOB_WAIT_DEFAULT.total_seconds()
    try:
        seconds = float(arguments.get("timeout_seconds"))
    except (TypeError, ValueError):
        seconds = limit
    if seconds != seconds:
        seconds = limit
    return now + timedelta(seconds=max(0.0, min(seconds, limit)))


async def task_owned_job_ids(
    db: Any, *, entity_id: str, task_id: str, job_ids: list[str],
) -> list[str]:
    """The listed jobs this Task started (MediaJob.params.task_id), in order.

    Job reads are otherwise only Entity-scoped; a Task loop waits on, and is
    woken by, its own jobs only.
    """

    from packages.core.models.media_job import MediaJob

    owned = set((await db.execute(
        select(MediaJob.id).where(
            MediaJob.entity_id == entity_id,
            MediaJob.id.in_(list(job_ids)),
            MediaJob.params["task_id"].astext == task_id,
        )
    )).scalars().all())
    return [job_id for job_id in job_ids if job_id in owned]


def cap_job_ids(job_ids: list[str]) -> tuple[list[str], list[str]]:
    """(the ids one wait covers, the ids beyond MAX_WAIT_JOB_IDS it drops)."""

    return list(job_ids[:MAX_WAIT_JOB_IDS]), list(job_ids[MAX_WAIT_JOB_IDS:])[:_MAX_DROPPED_JOB_IDS]


def job_wait_result(
    raw: str,
    *,
    foreign: list[str],
    total: int,
    waited_seconds: float | None = None,
    dropped: list[str] | None = None,
) -> str:
    """The registered tool's result, in its own shape.

    Ids that are not this Task's jobs are reported as missing, ids beyond the
    50-id cap as dropped, and a parked wait reports its real budget instead
    of the single read's zero timeout.
    """

    if not foreign and waited_seconds is None and not dropped:
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict) or payload.get("kind") != "media_jobs":
        return raw
    if foreign:
        payload["missing_job_ids"] = [*(payload.get("missing_job_ids") or []), *foreign]
        payload["total_count"] = total
    if waited_seconds is not None:
        payload["timeout_seconds"] = max(0, int(waited_seconds))
    if dropped:
        payload["dropped_job_ids"] = list(dropped)
        payload["note"] = (
            f"wait_media_jobs waits for at most {MAX_WAIT_JOB_IDS} job ids per call; "
            f"{len(dropped)} id(s) were not checked. Call it again for those."
        )
    return json.dumps(payload, ensure_ascii=False)


def no_owned_jobs_result(job_ids: list[str], dropped: list[str] | None = None) -> str:
    return job_wait_result(json.dumps({
        "kind": "media_jobs",
        "status": "failed",
        "jobs": [],
        "missing_job_ids": list(job_ids),
        "pending_job_ids": [],
        "failed_job_ids": [],
        "completed_count": 0,
        "total_count": len(job_ids),
        "timed_out": False,
        "timeout_seconds": 0,
        "error": "None of these job ids is a media job of this Task.",
    }, ensure_ascii=False), foreign=[], total=len(job_ids), dropped=dropped)


def pending_job_ids(raw: str) -> list[str]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    return [str(item) for item in payload.get("pending_job_ids") or [] if item]


def job_suspension(
    *,
    call_id: str,
    job_ids: list[str],
    deadline_at: datetime,
    requested_at: datetime,
    now: datetime,
    requested_job_ids: list[str] | None = None,
    dropped_job_ids: list[str] | None = None,
) -> RuntimeToolSuspension:
    job_ids = list(job_ids[:MAX_WAIT_JOB_IDS])
    return RuntimeToolSuspension(
        kind=WAITING_JOB_KIND,
        reservation_id="",
        poll_after_seconds=max(0, min(JOB_POLL_MAX_SECONDS, int((deadline_at - now).total_seconds()))),
        deadline_at=deadline_at,
        reason=WAIT_MEDIA_JOBS_TOOL_NAME,
        payload={
            "job_ids": job_ids,
            "deadline_at": iso(deadline_at),
            "requested_at": iso(requested_at),
            "requested_job_ids": list(requested_job_ids or job_ids)[:MAX_WAIT_JOB_IDS],
            "dropped_job_ids": list(dropped_job_ids or [])[:_MAX_DROPPED_JOB_IDS],
        },
    ).with_checkpoint_tool_call(
        tool_call_id=call_id,
        tool_name=WAIT_MEDIA_JOBS_TOOL_NAME,
        arguments={"job_ids": job_ids, "deadline_at": iso(deadline_at)},
    )
