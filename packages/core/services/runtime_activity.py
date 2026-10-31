"""What a running Workflow step is doing right now, for the people watching it.

A tool that waits on long work (a Sandbox command, a render) reports what it
sees here, and the run's surfaces read it back beside the step. The record is
bounded, short-lived and best-effort: it never fails or slows the work it
describes, and it holds what a person may read about the step -- phases,
counts, warnings -- never command output or arguments.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

ACTIVITY_TTL_SECONDS = 24 * 3600
MAX_ACTIVITY_EVENTS = 12
MAX_MESSAGE_CHARS = 280
# A stuck Redis must not hold up the work being described.
_IO_TIMEOUT_SECONDS = 1.0
# Phases every long-running tool shares; surfaces word them in the viewer's
# language and fall back to ``message`` for anything else.
ACTIVITY_CODES = frozenset({
    "queued",
    "running",
    "preparing_machine",
    "uploading_files",
    "checking",
    "preparing",
    "rendering_frames",
    "encoding",
    "retrying",
    "verifying_output",
    "saving_outputs",
    # The watched work ended; surfaces stop showing its last phase.
    "finished",
})
ACTIVITY_KINDS = frozenset({"progress", "warning", "error", "waiting"})
# A Sandbox command's own events, by what a watcher should make of them.
_SANDBOX_EVENT_KINDS = {
    "progress": "progress",
    "warning": "warning",
    "error": "error",
    "need_input": "waiting",
    "need_file": "waiting",
    "need_tool": "waiting",
    "need_credential": "waiting",
}


def _key(workflow_run_id: str) -> str:
    return f"manor:runtime:activity:{workflow_run_id}"


def activity_scope(context: Any) -> tuple[str, str] | None:
    """The Workflow run step a tool call belongs to, when it runs in one."""

    run_id = str(getattr(context, "workflow_run_id", None) or "").strip()
    step_id = str(getattr(context, "workflow_step_id", None) or "").strip()
    return (run_id, step_id) if run_id and step_id else None


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def activity_event(
    *,
    kind: str = "progress",
    message: str = "",
    code: str | None = None,
    percent: Any = None,
    done: Any = None,
    total: Any = None,
    count: Any = None,
    position: Any = None,
) -> dict[str, Any] | None:
    """One public activity entry, or None when there is nothing to say."""

    entry: dict[str, Any] = {"kind": kind if kind in ACTIVITY_KINDS else "progress"}
    text = " ".join(str(message or "").split())[:MAX_MESSAGE_CHARS]
    if text:
        entry["message"] = text
    if code in ACTIVITY_CODES:
        entry["code"] = code
    numbers = {"done": done, "total": total, "count": count, "position": position}
    for name, value in numbers.items():
        if (number := _bounded_int(value)) is not None:
            entry[name] = number
    pct = _bounded_int(percent)
    if pct is None and entry.get("total") and "done" in entry:
        pct = round(100 * min(entry["done"], entry["total"]) / entry["total"])
    if pct is not None:
        entry["percent"] = min(100, pct)
    if "message" not in entry and "code" not in entry:
        return None
    return entry


def sandbox_activity_events(status: Any) -> list[dict[str, Any]]:
    """What a Sandbox execution status tells a watcher: its own events and its place in line."""

    entries: list[dict[str, Any]] = []
    if str(getattr(status, "status", "") or "") == "queued" and getattr(status, "queue_position", None):
        entry = activity_event(kind="waiting", code="queued", position=status.queue_position)
        if entry:
            entries.append(entry)
    for event in getattr(status, "events", None) or []:
        kind = _SANDBOX_EVENT_KINDS.get(str(getattr(event, "type", "") or ""))
        if kind is None:
            continue
        payload = getattr(event, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        entry = activity_event(
            kind=kind,
            message=str(getattr(event, "message", "") or ""),
            code=str(payload.get("code") or "") or None,
            percent=payload.get("percent"),
            done=payload.get("done"),
            total=payload.get("total"),
            count=payload.get("count"),
        )
        if entry:
            entries.append(entry)
    return entries


def _same_phase(last: dict[str, Any], entry: dict[str, Any]) -> bool:
    if last.get("kind") != "progress" or entry["kind"] != "progress":
        return False
    return (last.get("code") or last.get("message")) == (entry.get("code") or entry.get("message"))


def _same_entry(last: dict[str, Any], entry: dict[str, Any]) -> bool:
    return {k: v for k, v in last.items() if k != "at"} == {k: v for k, v in entry.items() if k != "at"}


async def record_activity(scope: tuple[str, str] | None, *entries: dict[str, Any] | None) -> None:
    """Append what a step is doing; never raises.

    Progress on the same phase replaces the entry before it, so a long
    render keeps its warnings while its frame count moves.
    """

    entries = tuple(entry for entry in entries if entry)
    if scope is None or not entries:
        return
    run_id, step_id = scope
    try:
        async with asyncio.timeout(_IO_TIMEOUT_SECONDS):
            from packages.core.cache import _get_redis

            redis = await _get_redis()
            if redis is None:
                return
            key = _key(run_id)
            raw = await redis.hget(key, step_id)
            try:
                events = list(json.loads(raw).get("events") or []) if raw else []
            except (TypeError, ValueError, AttributeError):
                events = []
            now = datetime.now(timezone.utc).isoformat()
            for entry in entries:
                entry = {**entry, "at": now}
                last = events[-1] if events else None
                if last is not None and (_same_phase(last, entry) or _same_entry(last, entry)):
                    events[-1] = entry
                else:
                    events.append(entry)
            events = events[-MAX_ACTIVITY_EVENTS:]
            await redis.hset(key, step_id, json.dumps({"events": events, "updated_at": now}, ensure_ascii=False))
            await redis.expire(key, ACTIVITY_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - watching must never break the work
        logger.debug("runtime activity record failed (best-effort)", exc_info=True)


async def read_activity(workflow_run_id: str) -> dict[str, dict[str, Any]]:
    """Each step's recent activity: ``{step_id: {"current", "events", "updated_at"}}``."""

    try:
        from packages.core.cache import _get_redis

        async with asyncio.timeout(_IO_TIMEOUT_SECONDS):
            redis = await _get_redis()
            if redis is None:
                return {}
            rows = await redis.hgetall(_key(workflow_run_id))
    except Exception:  # noqa: BLE001 - a missing feed is just no feed
        logger.debug("runtime activity read failed (best-effort)", exc_info=True)
        return {}
    activity: dict[str, dict[str, Any]] = {}
    for step_id, raw in (rows or {}).items():
        if isinstance(step_id, bytes):
            step_id = step_id.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        events = [event for event in (data.get("events") or []) if isinstance(event, dict)]
        if not events:
            continue
        activity[str(step_id)] = {
            "current": events[-1],
            "events": events,
            "updated_at": data.get("updated_at"),
        }
    return activity


def sandbox_activity_recorder(context: Any) -> Callable[[Any], Awaitable[None]] | None:
    """Report a watched Sandbox command's statuses on its Workflow step; None outside one."""

    scope = activity_scope(context)
    if scope is None:
        return None
    waiting = False

    async def record(status: Any) -> None:
        nonlocal waiting
        entries = sandbox_activity_events(status)
        queued = any(entry.get("code") == "queued" for entry in entries)
        if waiting and not queued and str(getattr(status, "status", "") or "") == "running":
            # Out of the line: say so, or the step keeps showing its old place in it.
            entries.insert(0, activity_event(code="running"))
        waiting = queued
        if getattr(status, "terminal", False):
            entries.append(activity_event(code="finished"))
        await record_activity(scope, *entries)

    return record


__all__ = [
    "ACTIVITY_CODES",
    "activity_event",
    "activity_scope",
    "read_activity",
    "record_activity",
    "sandbox_activity_events",
    "sandbox_activity_recorder",
]
