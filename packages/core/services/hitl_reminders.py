"""Remind people about cards waiting on them: twice at most, then silence.

The first reminder comes at 24h and the final one at 96h (and at least 72h
after the first). Requests older than the window are never reminded, so a
deploy does not flood people with old cards. State lives on the request
(``context["reminders"]``). Each sweep sends one message per person and
Workspace, through their own notification settings.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, and_, bindparam, func, or_, select, text

from packages.core.constants.approvals import APPROVAL_OPEN_STATUSES
from packages.core.database import async_session
from packages.core.database_values import dumps_storable_json
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message
from packages.core.models.workspace import Workspace
from packages.core.services.notify import notify

logger = logging.getLogger(__name__)

FIRST_HOURS = int(os.getenv("HITL_REMINDER_FIRST_HOURS", "24"))
FINAL_HOURS = int(os.getenv("HITL_REMINDER_FINAL_HOURS", "96"))
MAX_AGE_HOURS = int(os.getenv("HITL_REMINDER_MAX_AGE_HOURS", "120"))
STATE_KEY = "reminders"
NOTIFICATION_KIND = "task_hitl_reminder"
_STAGE_HOURS = (FIRST_HOURS, FINAL_HOURS)
_STAGE_WAIT_TEXT = {1: "over a day", 2: "over 4 days"}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def due_stage(*, created_at: datetime, state: dict[str, Any], now: datetime) -> int | None:
    """The reminder stage due now (1 or 2), or None."""
    sent = int(state.get("sent") or 0)
    if sent >= len(_STAGE_HOURS):
        return None
    age = now - _aware(created_at)
    if age > timedelta(hours=MAX_AGE_HOURS) and sent == 0:
        return None
    if age < timedelta(hours=_STAGE_HOURS[sent]):
        return None
    last_at = state.get("last_at")
    if sent and last_at:
        spacing = timedelta(hours=_STAGE_HOURS[sent] - _STAGE_HOURS[sent - 1])
        if now - _aware(datetime.fromisoformat(last_at)) < spacing:
            return None
    return sent + 1


def summary(request: HitlRequest) -> str:
    payload = request.payload if isinstance(request.payload, dict) else {}
    for key in ("question", "action_description", "what_happened", "why"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return " ".join(str(request.reason or "A decision").split())[:160]


async def reminder_recipients(db, request: HitlRequest) -> list[str]:
    """Who must answer: the Task's people for a Task card, else the Workspace's approvers.

    No admin fallback for Task cards: a Task with nobody on it follows the
    Workspace policy, including an explicit opt-out.
    """
    from packages.core.services.notification_workspace_callbacks import resolve_workspace_hitl_recipients
    from packages.core.services.task_event_notifications import task_event_recipient_users

    if request.origin_task_id:
        _task, users = await task_event_recipient_users(
            db, request.entity_id, "task.hitl_reminder", {"task_id": request.origin_task_id},
            admin_fallback=False,
        )
        if users:
            return [str(user.id) for user in users]
    return await resolve_workspace_hitl_recipients(
        db, entity_id=request.entity_id, workspace_id=request.workspace_id,
    )


async def card_link(db, request: HitlRequest) -> str:
    message_id = request.origin_message_id or (await db.execute(
        select(Message.id).where(
            Message.pending_action["approval_request_id"].as_string() == request.id,
        ).limit(1)
    )).scalar_one_or_none()
    if message_id and request.workspace_id:
        return f"/chat?workspace={request.workspace_id}#workspace-chat-message-{message_id}"
    if request.origin_task_id:
        return f"/tasks/{request.origin_task_id}"
    return f"/chat?workspace={request.workspace_id}"


def _message(items: list[dict[str, Any]]) -> tuple[str, str]:
    """Copy for one batch; every item in a batch is from the same Workspace."""
    if len(items) == 1:
        item = items[0]
        return (f"Waiting on you: {item['summary'][:80]}",
                f"{item['workspace']} · waiting {_STAGE_WAIT_TEXT[item['stage']]}.")
    lines = [f"• {i['summary'][:100]} (waiting {_STAGE_WAIT_TEXT[i['stage']]})" for i in items]
    return f"{len(items)} items are waiting on you in {items[0]['workspace']}", "\n".join(lines)


def _due_in_sql(now: datetime):
    """Rows with a reminder due now, so rows that never will be cannot fill the batch."""
    sent = func.coalesce(HitlRequest.context[(STATE_KEY, "sent")].as_integer(), 0)
    last_at = HitlRequest.context[(STATE_KEY, "last_at")].astext.cast(DateTime(timezone=True))
    return or_(
        and_(
            sent == 0,
            HitlRequest.created_at >= now - timedelta(hours=MAX_AGE_HOURS),
            HitlRequest.created_at <= now - timedelta(hours=FIRST_HOURS),
        ),
        and_(
            sent == 1,
            HitlRequest.created_at <= now - timedelta(hours=FINAL_HOURS),
            last_at <= now - timedelta(hours=FINAL_HOURS - FIRST_HOURS),
        ),
    )


_CLAIM_SQL = text(
    "UPDATE hitl_requests"
    " SET context = jsonb_set(coalesce(context, '{}'::jsonb), '{reminders}', CAST(:state AS jsonb), true)"
    " WHERE id = :id AND status IN :open"
    " AND coalesce((context #>> '{reminders,sent}')::int, 0) = :prev"
    " RETURNING id"
).bindparams(bindparam("open", expanding=True))


async def _claim(db, request_id: str, *, prev_sent: int, state: dict[str, Any]) -> bool:
    """Record reminder state only if the request is still open and at the stage read.

    Answer paths lock the row and write into ``context``; this touches only
    ``context.reminders`` in one statement, so it can never erase an answer,
    and it fails for a request answered since it was read.
    """
    claimed = (await db.execute(_CLAIM_SQL, {
        "state": dumps_storable_json(state), "id": request_id,
        "open": list(APPROVAL_OPEN_STATUSES), "prev": prev_sent,
    })).scalar_one_or_none()
    return claimed is not None


async def _log_on_task(db, item: dict[str, Any], *, now: datetime) -> None:
    """Show the reminder on the Task's timeline, in the claim's transaction."""
    from packages.core.constants.task import TaskLogType
    from packages.core.constants.task_actors import TaskActor
    from packages.core.services.task_service import add_task_log

    await add_task_log(
        db, item["task_id"], TaskLogType.AI_HITL_REMINDER,
        f"Reminder sent: \"{item['summary'][:100]}\" has waited {_STAGE_WAIT_TEXT[item['stage']]} for an answer.",
        actor=TaskActor.SYSTEM, created_by="system",
        metadata={
            "task_id": item["task_id"], "hitl_request_id": item["id"], "stage": item["stage"],
            "wait_minutes": item["wait_minutes"], "prompt": item["summary"], "reminded_at": now.isoformat(),
        },
    )


async def run_hitl_reminder_sweep(*, now: datetime | None = None, limit: int = 200) -> int:
    """Send due reminders. Returns the number of messages handed to notify().

    Three phases: read and prepare without writing; claim each request with a
    guarded update; after commit, notify only for the claims that succeeded.
    """
    now = now or datetime.now(timezone.utc)
    # Keyed by (entity, person, Workspace): a batch never spans Workspaces.
    batches: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    prepared: list[tuple[dict[str, Any], int, list[str]]] = []
    unreachable: list[tuple[str, int]] = []
    async with async_session() as db:
        requests = (await db.execute(
            select(HitlRequest).where(
                HitlRequest.status.in_(APPROVAL_OPEN_STATUSES),
                HitlRequest.workspace_id.is_not(None),
                _due_in_sql(now),
            ).order_by(HitlRequest.created_at.asc()).limit(limit)
        )).scalars().all()
        names: dict[str, str] = {}
        for request in requests:
            state = (request.context or {}).get(STATE_KEY) or {}
            # The SQL predicate picks candidates; this check decides, so a
            # mismatch between the two can only skip a reminder, never add one.
            stage = due_stage(created_at=request.created_at, state=state, now=now)
            if stage is None:
                continue
            users = await reminder_recipients(db, request)
            if not users:
                unreachable.append((request.id, int(state.get("sent") or 0)))
                continue
            if request.workspace_id not in names:
                names[request.workspace_id] = (await db.execute(
                    select(Workspace.name).where(Workspace.id == request.workspace_id)
                )).scalar_one_or_none() or "Workspace"
            item = {"id": request.id, "stage": stage, "summary": summary(request),
                    "workspace": names[request.workspace_id], "workspace_id": request.workspace_id,
                    "link": await card_link(db, request), "entity_id": request.entity_id,
                    "task_id": request.origin_task_id,
                    "wait_minutes": max(0, int((now - _aware(request.created_at)).total_seconds() // 60))}
            prepared.append((item, int(state.get("sent") or 0), users))

        for request_id, prev_sent in unreachable:
            # Nobody can answer it: finish its reminders so it is not rescanned every tick.
            await _claim(db, request_id, prev_sent=prev_sent, state={
                "sent": len(_STAGE_HOURS), "skipped": "no_recipients", "last_at": now.isoformat(),
            })
        for item, prev_sent, users in prepared:
            state = {"sent": item["stage"], "last_at": now.isoformat()}
            if not await _claim(db, item["id"], prev_sent=prev_sent, state=state):
                continue
            if item["task_id"]:
                await _log_on_task(db, item, now=now)
            for user_id in users:
                batches[(item["entity_id"], user_id, item["workspace_id"])].append(item)
        await db.commit()

    delivered = 0
    for (entity_id, user_id, workspace_id), items in batches.items():
        title, body = _message(items)
        digest = hashlib.sha256("|".join(sorted(f"{i['id']}:{i['stage']}" for i in items)).encode()).hexdigest()[:32]
        try:
            await notify(
                entity_id=entity_id, user_id=user_id, type=NOTIFICATION_KIND, title=title, body=body,
                link=items[0]["link"] if len(items) == 1 else f"/chat?workspace={workspace_id}",
                # Always set: notify() then checks the person can still read this Workspace.
                severity="info", workspace_id=workspace_id,
                meta={"hitl_request_ids": [i["id"] for i in items]},
                idempotency_key=f"hitl-reminder:{user_id}:{digest}",
            )
            delivered += 1
        except Exception:
            logger.warning("hitl reminder notify failed for %s", user_id, exc_info=True)
    return delivered
