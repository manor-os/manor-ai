"""Monitoring Celery tasks — daily health briefing, heartbeat check, SLA sweep.

Ported from manor-multi-agent's system_monitor.py and heartbeat.py into
manor-os's Celery Beat architecture.

Tasks:
  daily_health_briefing  — runs at 8am daily, generates a health report per entity
  heartbeat_check        — runs every 30 min, proactive agent attention in active conversations
  sla_breach_check       — runs every 5 min, flips ``Task.sla_breached`` and
                          fires escalation rules when SLA windows expire
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)

MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() != "false"
HEARTBEAT_OK_MARKER = "HEARTBEAT_OK"
# Skip proactive messages if the conversation already has a recent assistant
# message within this many minutes.
HEARTBEAT_COOLDOWN_MINUTES = 30
HITL_REMINDER_AFTER_MINUTES = int(os.getenv("HITL_REMINDER_AFTER_MINUTES", "60"))
HITL_REMINDER_COOLDOWN_MINUTES = int(os.getenv("HITL_REMINDER_COOLDOWN_MINUTES", "240"))
HITL_REMINDER_LIMIT = int(os.getenv("HITL_REMINDER_LIMIT", "50"))


def _run_async(coro):
    """Run an async coroutine from synchronous Celery worker context."""
    return asyncio.run(coro)


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _hitl_wait_started_at(step) -> datetime:
    """Best-effort timestamp for when a step entered waiting_human."""
    return (
        _aware_utc(getattr(step, "updated_at", None))
        or _aware_utc(getattr(step, "started_at", None))
        or _aware_utc(getattr(step, "created_at", None))
        or datetime.now(timezone.utc)
    )


def _hitl_last_reminded_at(plan, step_id: str) -> datetime | None:
    state = getattr(plan, "dispatcher_state", None) or {}
    reminders = state.get("hitl_reminders") if isinstance(state, dict) else None
    if not isinstance(reminders, dict):
        return None
    item = reminders.get(step_id)
    if isinstance(item, dict):
        return _parse_iso_datetime(item.get("last_reminded_at"))
    return _parse_iso_datetime(item)


def _hitl_reminder_due(
    *,
    wait_started_at: datetime,
    last_reminded_at: datetime | None,
    now: datetime,
    after_minutes: int = HITL_REMINDER_AFTER_MINUTES,
    cooldown_minutes: int = HITL_REMINDER_COOLDOWN_MINUTES,
) -> bool:
    wait_started_at = _aware_utc(wait_started_at) or now
    last_reminded_at = _aware_utc(last_reminded_at)
    if wait_started_at > now - timedelta(minutes=after_minutes):
        return False
    if last_reminded_at and last_reminded_at > now - timedelta(minutes=cooldown_minutes):
        return False
    return True


def _record_hitl_reminder(plan, step_id: str, *, now: datetime, wait_minutes: int) -> None:
    state = dict(getattr(plan, "dispatcher_state", None) or {})
    reminders = dict(state.get("hitl_reminders") or {})
    reminders[step_id] = {
        "last_reminded_at": now.isoformat(),
        "wait_minutes": wait_minutes,
    }
    state["hitl_reminders"] = reminders
    plan.dispatcher_state = state


# ---------------------------------------------------------------------------
# 1. Daily Health Briefing
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.daily_health_briefing", max_retries=1)
def daily_health_briefing(self):
    """Generate and deliver a daily health briefing for every active entity.

    Gathers task stats, agent execution metrics, scheduled job health, and
    goal progress, then formats them into a structured 3-section report
    saved as a system notification for each entity.
    """
    if not MONITOR_ENABLED:
        logger.info("daily_health_briefing: disabled via MONITOR_ENABLED=false")
        return

    try:
        _run_async(_async_daily_health_briefing())
    except Exception as exc:
        logger.error("daily_health_briefing failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=300)


async def _async_daily_health_briefing():
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.user import Entity

    async with create_worker_session()() as db:
        # Get all active entities
        result = await db.execute(
            select(Entity).where(Entity.deleted_at.is_(None))
        )
        entities = list(result.scalars().all())

    if not entities:
        logger.info("daily_health_briefing: no active entities found")
        return

    logger.info("daily_health_briefing: generating briefings for %d entities", len(entities))

    for entity in entities:
        try:
            await _generate_entity_briefing(entity.id, entity.name)
        except Exception as e:
            logger.warning(
                "daily_health_briefing: failed for entity=%s: %s",
                entity.id, e,
            )


async def _generate_entity_briefing(entity_id: str, entity_name: str):
    """Gather metrics and save a health briefing notification for one entity."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.user import User

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with create_worker_session()() as db:
        # ── Task stats ──
        task_stats = await _query_task_stats(db, entity_id, now, today_start)

        # ── Agent execution metrics (last 24h) ──
        exec_stats = await _query_execution_stats(db, entity_id, now)

        # ── Scheduled job health ──
        job_stats = await _query_job_stats(db, entity_id)

        # ── Goal progress ──
        goal_stats = await _query_goal_stats(db, entity_id)

        # ── Format the report ──
        report = _format_briefing(
            entity_name=entity_name,
            date_str=now.strftime("%Y-%m-%d"),
            task_stats=task_stats,
            exec_stats=exec_stats,
            job_stats=job_stats,
            goal_stats=goal_stats,
        )

        # ── Save as notification for each admin/owner user in the entity ──
        from packages.core.services.notification_service import create_notification

        result = await db.execute(
            select(User).where(
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
                User.status == "active",
                User.role.in_(["owner", "admin"]),
            )
        )
        admin_users = list(result.scalars().all())

        if not admin_users:
            logger.debug("daily_health_briefing: no admin users for entity=%s, logging only", entity_id)
            logger.info("daily_health_briefing [%s]:\n%s", entity_id, report[:500])
            return

        # Structured payload for the rich UI renderer. Mirrors the text
        # report so the web app can draw stat chips + action checklist
        # instead of showing the raw divider-heavy plaintext.
        alerts: list[str] = []
        if task_stats["overdue"] > 0:
            alerts.append(f"{task_stats['overdue']} overdue task(s)")
        if task_stats["blocked"] > 0:
            alerts.append(f"{task_stats['blocked']} blocked task(s)")
        if task_stats["stalled"] > 0:
            alerts.append(f"{task_stats['stalled']} stalled task(s)")
        if job_stats["broken"] > 0:
            alerts.append(f"{job_stats['broken']} broken scheduled job(s)")
        if goal_stats["stuck_hitl"] > 0:
            alerts.append(f"{goal_stats['stuck_hitl']} goal(s) awaiting human input")

        actions: list[str] = []
        if task_stats["overdue"] > 0:
            actions.append(f"Review {task_stats['overdue']} overdue task(s) and update deadlines or status")
        if task_stats["stalled"] > 0:
            actions.append(f"Check {task_stats['stalled']} stalled in-progress task(s)")
        if goal_stats["stuck_hitl"] > 0:
            actions.append(f"Respond to {goal_stats['stuck_hitl']} goal(s) waiting for your input")
        if job_stats["broken"] > 0:
            actions.append(f"Investigate {job_stats['broken']} broken scheduled job(s)")
        if task_stats["blocked"] > 0:
            actions.append(f"Unblock {task_stats['blocked']} blocked task(s)")

        briefing_meta = {
            "kind": "daily_briefing",
            "date": now.strftime("%Y-%m-%d"),
            "entity_name": entity_name,
            "tasks": task_stats,
            "executions": exec_stats,
            "jobs": job_stats,
            "goals": goal_stats,
            "alerts": alerts,
            "action_items": actions,
            "link": "/dashboard",
        }

        for user in admin_users:
            await create_notification(
                db,
                entity_id=entity_id,
                user_id=user.id,
                type="system_health",
                title=f"Daily Briefing — {now.strftime('%b %d, %Y')}",
                body=report,
                meta=briefing_meta,
            )

        await db.commit()
        logger.info(
            "daily_health_briefing: delivered to %d users for entity=%s",
            len(admin_users), entity_id,
        )


async def _query_task_stats(db, entity_id: str, now: datetime, today_start: datetime) -> dict:
    """Query task statistics for the briefing."""
    from sqlalchemy import select, func
    from packages.core.models.task import Task
    from packages.core.services.task_deadlines import task_deadline_overdue_expr

    # Total open tasks
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status.in_(["pending", "in_progress", "blocked"]),
        )
    )
    open_count = r.scalar_one()

    # Overdue tasks (have a deadline in the past, still not completed)
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status.in_(["pending", "in_progress", "blocked"]),
            Task.deadline.isnot(None),
            task_deadline_overdue_expr(Task.deadline, now_expr=now, current_date_expr=today_start.date()),
        )
    )
    overdue_count = r.scalar_one()

    # Blocked tasks
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status == "blocked",
        )
    )
    blocked_count = r.scalar_one()

    # Completed today
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status == "completed",
            Task.completed_at >= today_start,
        )
    )
    completed_today = r.scalar_one()

    # Stalled: in_progress for more than 48h with no update
    stale_cutoff = now - timedelta(hours=48)
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status == "in_progress",
            Task.updated_at < stale_cutoff,
        )
    )
    stalled_count = r.scalar_one()

    return {
        "open": open_count,
        "overdue": overdue_count,
        "blocked": blocked_count,
        "completed_today": completed_today,
        "stalled": stalled_count,
    }


async def _query_execution_stats(db, entity_id: str, now: datetime) -> dict:
    """Query agent execution metrics for the last 24 hours."""
    from sqlalchemy import select, func
    from packages.core.models.scheduler import AgentExecution

    since = now - timedelta(hours=24)

    # Total executions in the last 24h
    r = await db.execute(
        select(func.count()).select_from(AgentExecution).where(
            AgentExecution.entity_id == entity_id,
            AgentExecution.created_at >= since,
        )
    )
    total = r.scalar_one()

    if total == 0:
        return {"total": 0, "success_rate": 0.0, "avg_turns": 0.0, "total_tokens": 0}

    # Successful executions
    r = await db.execute(
        select(func.count()).select_from(AgentExecution).where(
            AgentExecution.entity_id == entity_id,
            AgentExecution.created_at >= since,
            AgentExecution.status == "completed",
        )
    )
    success_count = r.scalar_one()

    # Average turns
    r = await db.execute(
        select(func.avg(AgentExecution.turns_used)).where(
            AgentExecution.entity_id == entity_id,
            AgentExecution.created_at >= since,
        )
    )
    avg_turns = r.scalar_one() or 0.0

    return {
        "total": total,
        "success_rate": round(success_count / total * 100, 1) if total else 0.0,
        "avg_turns": round(float(avg_turns), 1),
        "total_tokens": 0,  # token_usage is JSONB, skip deep aggregation for now
    }


async def _query_job_stats(db, entity_id: str) -> dict:
    """Query scheduled job health."""
    from sqlalchemy import select, func
    from packages.core.models.scheduler import ScheduledJob

    # Total enabled jobs
    r = await db.execute(
        select(func.count()).select_from(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.enabled == True,  # noqa: E712
        )
    )
    enabled_count = r.scalar_one()

    # Jobs with errors
    r = await db.execute(
        select(func.count()).select_from(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.enabled == True,  # noqa: E712
            ScheduledJob.last_status == "error",
        )
    )
    error_count = r.scalar_one()

    # Jobs with consecutive errors >= 3 (likely broken)
    r = await db.execute(
        select(func.count()).select_from(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.enabled == True,  # noqa: E712
            ScheduledJob.consecutive_errors >= 3,
        )
    )
    broken_count = r.scalar_one()

    return {
        "enabled": enabled_count,
        "errored": error_count,
        "broken": broken_count,
    }


async def _query_goal_stats(db, entity_id: str) -> dict:
    """Query plan + step health for the daily briefing.

    The old implementation counted GoalRun + StepRun rows. Those tables
    are gone; the new equivalents are ExecutionPlan (Planner output)
    and ExecutionStep (atomic node). "Stuck HITL" now means a step in
    ``waiting_human`` status.
    """
    from sqlalchemy import select, func
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    # Active plans (running)
    r = await db.execute(
        select(func.count()).select_from(ExecutionPlan).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.status.in_(["running", "pending_approval", "paused"]),
        )
    )
    active_plans = r.scalar_one()

    # Plans with steps waiting on human input
    r = await db.execute(
        select(func.count(func.distinct(ExecutionStep.plan_id))).where(
            ExecutionStep.entity_id == entity_id,
            ExecutionStep.step_status == "waiting_human",
        )
    )
    stuck_plans = r.scalar_one()

    return {
        "active": active_plans,
        "stuck_hitl": stuck_plans,
    }


def _format_briefing(
    *,
    entity_name: str,
    date_str: str,
    task_stats: dict,
    exec_stats: dict,
    job_stats: dict,
    goal_stats: dict,
) -> str:
    """Format stats into the structured 3-section report."""
    # ── Platform Health ──
    alerts = []
    if task_stats["overdue"] > 0:
        alerts.append(f"{task_stats['overdue']} overdue task(s)")
    if task_stats["blocked"] > 0:
        alerts.append(f"{task_stats['blocked']} blocked task(s)")
    if task_stats["stalled"] > 0:
        alerts.append(f"{task_stats['stalled']} stalled task(s) (no update in 48h)")
    if job_stats["broken"] > 0:
        alerts.append(f"{job_stats['broken']} scheduled job(s) with repeated failures")
    if goal_stats["stuck_hitl"] > 0:
        alerts.append(f"{goal_stats['stuck_hitl']} goal(s) waiting for human input")
    alert_text = "; ".join(alerts) if alerts else "All clear"

    # ── Action items ──
    action_items = []
    if task_stats["overdue"] > 0:
        action_items.append(f"Review {task_stats['overdue']} overdue task(s) and update deadlines or status")
    if task_stats["stalled"] > 0:
        action_items.append(f"Check {task_stats['stalled']} stalled in-progress task(s)")
    if goal_stats["stuck_hitl"] > 0:
        action_items.append(f"Respond to {goal_stats['stuck_hitl']} goal(s) waiting for your input")
    if job_stats["broken"] > 0:
        action_items.append(f"Investigate {job_stats['broken']} broken scheduled job(s)")
    if task_stats["blocked"] > 0:
        action_items.append(f"Unblock {task_stats['blocked']} blocked task(s)")
    if not action_items:
        action_items.append("No urgent actions required today.")

    action_lines = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(action_items))

    return (
        f"Manor Daily Briefing — {date_str}\n"
        f"Entity: {entity_name}\n"
        f"\n"
        f"--- Platform Health ---\n"
        f"  Tasks: {task_stats['open']} open | {task_stats['overdue']} overdue | "
        f"{task_stats['completed_today']} completed today\n"
        f"  Agent Executions (24h): {exec_stats['total']} total | "
        f"{exec_stats['success_rate']}% success | avg {exec_stats['avg_turns']} turns\n"
        f"  Scheduled Jobs: {job_stats['enabled']} enabled | "
        f"{job_stats['errored']} errored | {job_stats['broken']} broken\n"
        f"  Goals: {goal_stats['active']} active | {goal_stats['stuck_hitl']} awaiting human input\n"
        f"  Alerts: {alert_text}\n"
        f"\n"
        f"--- Business Overview ---\n"
        f"  Open workload: {task_stats['open']} tasks across all workspaces\n"
        f"  Stalled work: {task_stats['stalled']} task(s) with no progress in 48h\n"
        f"  Blocked: {task_stats['blocked']} task(s) needing intervention\n"
        f"\n"
        f"--- Action Items ---\n"
        f"{action_lines}\n"
    )


# ---------------------------------------------------------------------------
# 2. Heartbeat Check
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.heartbeat_check")
def heartbeat_check(self):
    """Check active conversations for proactive agent attention needs.

    Looks at conversations with recent activity, checks if the assigned agent
    has overdue/stalled tasks or stuck goals, and posts a proactive message
    if attention is needed.
    """
    try:
        _run_async(_async_heartbeat_check())
    except Exception as exc:
        logger.error("heartbeat_check failed: %s", exc, exc_info=True)


async def _async_heartbeat_check():
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.task import Conversation
    from packages.core.services.conversation_messages import add_message

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=2)
    cooldown_cutoff = now - timedelta(minutes=HEARTBEAT_COOLDOWN_MINUTES)

    async with create_worker_session()() as db:
        # Find active conversations that have messages in the last 2 hours
        # and have an assigned agent
        result = await db.execute(
            select(Conversation).where(
                Conversation.status == "active",
                Conversation.agent_id.isnot(None),
                Conversation.channel != "webchat",
            )
        )
        conversations = list(result.scalars().all())

        if not conversations:
            return

        checked = 0
        notified = 0

        for conv in conversations:
            try:
                should_notify, message = await _check_conversation_attention(
                    db, conv, now, recent_cutoff, cooldown_cutoff,
                )
                checked += 1

                if should_notify and message:
                    await add_message(
                        db, conv.id,
                        role="assistant",
                        content=message,
                    )
                    notified += 1
            except Exception as e:
                logger.debug(
                    "heartbeat_check: error checking conv=%s: %s",
                    conv.id, e,
                )

        if notified:
            await db.commit()

        if checked:
            logger.info(
                "heartbeat_check: checked %d conversations, sent %d proactive messages",
                checked, notified,
            )


async def _check_conversation_attention(
    db,
    conv,
    now: datetime,
    recent_cutoff: datetime,
    cooldown_cutoff: datetime,
) -> tuple[bool, str | None]:
    """Check if a conversation's agent needs to proactively notify the user.

    Returns (should_notify, message_text).
    """
    from sqlalchemy import select, func
    from packages.core.models.task import Message, Task
    from packages.core.models.execution import ExecutionStep
    from packages.core.services.task_deadlines import task_deadline_overdue_expr

    # Check if there are recent messages (last 2 hours)
    r = await db.execute(
        select(func.count()).select_from(Message).where(
            Message.conversation_id == conv.id,
            Message.created_at >= recent_cutoff,
        )
    )
    recent_msg_count = r.scalar_one()
    if recent_msg_count == 0:
        return False, None  # No recent activity, skip

    # Check if there's already a recent assistant message (cooldown)
    r = await db.execute(
        select(func.count()).select_from(Message).where(
            Message.conversation_id == conv.id,
            Message.role == "assistant",
            Message.created_at >= cooldown_cutoff,
        )
    )
    recent_assistant_count = r.scalar_one()
    if recent_assistant_count > 0:
        return False, None  # Already messaged recently, skip

    # ── Gather attention signals ──
    attention_items: list[str] = []

    # 1. Overdue tasks assigned to this agent
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == conv.entity_id,
            Task.agent_id == conv.agent_id,
            Task.status.in_(["pending", "in_progress"]),
            Task.deadline.isnot(None),
            task_deadline_overdue_expr(Task.deadline, now_expr=now, current_date_expr=now.date()),
        )
    )
    overdue_tasks = r.scalar_one()
    if overdue_tasks > 0:
        attention_items.append(f"{overdue_tasks} overdue task(s) assigned to me")

    # 2. Stalled tasks (in_progress for > 48h with no update)
    stale_cutoff = now - timedelta(hours=48)
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == conv.entity_id,
            Task.agent_id == conv.agent_id,
            Task.status == "in_progress",
            Task.updated_at < stale_cutoff,
        )
    )
    stalled_tasks = r.scalar_one()
    if stalled_tasks > 0:
        attention_items.append(f"{stalled_tasks} stalled task(s) with no progress in 48h")

    # 3. Plans with steps waiting on human input.
    # The old query joined by conversation_id; the new ExecutionPlan
    # doesn't carry that field directly, so we scope to entity-wide
    # pending HITL — the heartbeat message is per-conversation but the
    # surfaced count is "plans needing your attention overall", which
    # is actually the more useful framing for a single-operator system.
    r = await db.execute(
        select(func.count(func.distinct(ExecutionStep.plan_id))).where(
            ExecutionStep.entity_id == conv.entity_id,
            ExecutionStep.step_status == "waiting_human",
        )
    )
    stuck_plans = r.scalar_one()
    if stuck_plans > 0:
        attention_items.append(f"{stuck_plans} plan(s) waiting for your input")

    if not attention_items:
        return False, None

    # ── Format proactive message ──
    items_text = "\n".join(f"- {item}" for item in attention_items)
    message = (
        f"Heads up — I noticed a few items that may need your attention:\n\n"
        f"{items_text}\n\n"
        f"Let me know if you'd like me to help with any of these."
    )

    return True, message


# ---------------------------------------------------------------------------
# 3. HITL Waiting Reminder
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.hitl_waiting_reminder", max_retries=1)
def hitl_waiting_reminder(self):
    """Remind task owners when plan steps wait too long for human input."""
    if not MONITOR_ENABLED:
        logger.info("hitl_waiting_reminder: disabled via MONITOR_ENABLED=false")
        return

    try:
        delivered = _run_async(_async_hitl_waiting_reminder())
        if delivered:
            logger.info("hitl_waiting_reminder: delivered %d reminder(s)", delivered)
    except Exception as exc:
        logger.error("hitl_waiting_reminder failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=300)


async def _async_hitl_waiting_reminder(
    *,
    now: datetime | None = None,
    after_minutes: int = HITL_REMINDER_AFTER_MINUTES,
    cooldown_minutes: int = HITL_REMINDER_COOLDOWN_MINUTES,
    limit: int = HITL_REMINDER_LIMIT,
) -> int:
    from sqlalchemy import func, select

    from packages.core.database import create_worker_session
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    current = now or datetime.now(timezone.utc)
    stale_cutoff = current - timedelta(minutes=after_minutes)

    async with create_worker_session()() as db:
        rows = list((await db.execute(
            select(ExecutionStep, ExecutionPlan, Task)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
            .join(Task, Task.id == ExecutionPlan.task_id)
            .where(
                ExecutionStep.step_status == "waiting_human",
                ExecutionPlan.status.in_(("running", "paused", "needs_attention")),
                func.coalesce(
                    ExecutionStep.updated_at,
                    ExecutionStep.started_at,
                    ExecutionStep.created_at,
                ) <= stale_cutoff,
            )
            .order_by(ExecutionStep.updated_at.asc().nullslast(), ExecutionStep.created_at.asc())
            .limit(limit)
        )).all())

        delivered = 0
        webhook_events: list[tuple[str, str, dict[str, Any]]] = []
        for step, plan, task in rows:
            wait_started_at = _hitl_wait_started_at(step)
            last_reminded_at = _hitl_last_reminded_at(plan, step.id)
            if not _hitl_reminder_due(
                wait_started_at=wait_started_at,
                last_reminded_at=last_reminded_at,
                now=current,
                after_minutes=after_minutes,
                cooldown_minutes=cooldown_minutes,
            ):
                continue

            wait_minutes = max(0, int((current - wait_started_at).total_seconds() // 60))
            count, event_payload = await _deliver_hitl_reminder(
                db,
                task=task,
                plan=plan,
                step=step,
                now=current,
                wait_minutes=wait_minutes,
            )
            if count:
                _record_hitl_reminder(plan, step.id, now=current, wait_minutes=wait_minutes)
                delivered += count
                webhook_events.append((task.entity_id, "task.hitl_reminder", event_payload))

        if delivered:
            await db.commit()
            from packages.core.services.event_emitter import (
                deliver_task_external_event,
                deliver_webhook_event,
            )
            for entity_id, event_type, payload in webhook_events:
                await deliver_webhook_event(entity_id, event_type, payload)
                await deliver_task_external_event(entity_id, event_type, payload)
        return delivered


async def _deliver_hitl_reminder(
    db,
    *,
    task,
    plan,
    step,
    now: datetime,
    wait_minutes: int,
) -> tuple[int, dict[str, Any]]:
    from packages.core.services.event_emitter import emit_in_session
    from packages.core.services.task_service import add_task_log

    prompt = (step.human_input_prompt or "This task is waiting for your input.").strip()
    meta = {
        "task_id": task.id,
        "plan_id": plan.id,
        "step_id": step.id,
        "step_key": step.step_key,
        "wait_minutes": wait_minutes,
        "prompt": prompt,
        "reminded_at": now.isoformat(),
    }
    await add_task_log(
        db,
        task.id,
        "ai_hitl_reminder",
        f"Reminder sent: step '{step.step_key}' has waited {wait_minutes} minute(s) for human input.",
        created_by="system",
        metadata=meta,
    )
    delivered = await emit_in_session(
        db,
        task.entity_id,
        "task.hitl_reminder",
        source="hitl_waiting_reminder",
        payload=meta,
    )
    return delivered, meta


# ---------------------------------------------------------------------------
# 4. SLA Breach Check
# ---------------------------------------------------------------------------
#
# Periodic sweep that flips ``Task.sla_breached`` to True for any open
# task whose SLA response or resolution window has elapsed, and fires
# the first escalation rule configured for the policy. Without this
# beat entry the SLA chip in the UI would never go red on its own —
# ``check_sla_deadlines`` is a request-time function that nothing was
# calling on a timer.

@celery_app.task(bind=True, name="monitor.sla_breach_check", max_retries=1)
def sla_breach_check(self):
    """Sweep all entities for SLA breaches.

    Cheap when no policies exist (the join filters out entities with
    no SLAs immediately). Idempotent — already-breached tasks are
    excluded by the query, so re-running is safe.
    """
    if not MONITOR_ENABLED:
        logger.info("sla_breach_check: disabled via MONITOR_ENABLED=false")
        return

    try:
        breached = _run_async(_async_sla_breach_check())
        if breached:
            logger.info("sla_breach_check: %d task(s) newly breached", breached)
    except Exception as exc:
        logger.error("sla_breach_check failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=60)


async def _async_sla_breach_check() -> int:
    from packages.core.database import create_worker_session
    from packages.core.services.task_automation_service import check_sla_deadlines

    async with create_worker_session()() as db:
        # entity_id=None → check all entities in one pass; the join
        # against task_sla_policies restricts the scan to tasks that
        # actually have a policy attached, so this stays cheap.
        breached = await check_sla_deadlines(db, entity_id=None)
        await db.commit()
        return breached


# ---------------------------------------------------------------------------
# Workspace readiness check — lightweight, no LLM
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.workspace_readiness_check", max_retries=0)
def workspace_readiness_check(self):
    """Check if any workspace became unblocked and trigger Strategist.

    Runs every 10 min. Compares workspace state snapshots to detect
    when missing setup items get resolved (integrations added, goals
    created, agents mapped). No LLM calls — pure DB queries.
    """
    try:
        triggered = _run_async(_async_readiness_check())
        if triggered:
            logger.info("workspace_readiness_check: triggered %d review(s)", triggered)
    except Exception as exc:
        logger.error("workspace_readiness_check failed: %s", exc, exc_info=True)


async def _async_readiness_check() -> int:
    from sqlalchemy import select, func
    from packages.core.database import create_worker_session
    from packages.core.models.workspace import Workspace, AgentSubscription
    from packages.core.models.goal import Goal
    from packages.core.models.document import Integration

    triggered = 0
    async with create_worker_session()() as db:
        # Active workspaces with heartbeat on (skip soft-deleted ones —
        # they're in the trash grace window and shouldn't run jobs).
        workspaces = list((await db.execute(
            select(Workspace).where(
                Workspace.status == "active",
                Workspace.heartbeat_enabled.is_(True),
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all())

        for ws in workspaces:
            settings = ws.settings or {}
            last_check = settings.get("_readiness_snapshot", {})

            # Current state (cheap counts)
            agent_count = (await db.execute(
                select(func.count()).select_from(AgentSubscription).where(
                    AgentSubscription.workspace_id == ws.id,
                    AgentSubscription.status == "active",
                )
            )).scalar_one()
            goal_count = (await db.execute(
                select(func.count()).select_from(Goal).where(
                    Goal.workspace_id == ws.id,
                    Goal.status == "active",
                )
            )).scalar_one()
            integration_count = (await db.execute(
                select(func.count()).select_from(Integration).where(
                    Integration.entity_id == ws.entity_id,
                    Integration.status == "active",
                )
            )).scalar_one()

            current = {
                "agents": agent_count,
                "goals": goal_count,
                "integrations": integration_count,
            }

            # Compare: did anything increase since last check?
            changed = False
            for key in ("agents", "goals", "integrations"):
                if current[key] > last_check.get(key, 0):
                    changed = True
                    break

            if changed:
                # Something new was added — trigger Strategist
                try:
                    from packages.core.tasks.ai_tasks import run_strategist_review
                    diff_parts = []
                    for key in ("agents", "goals", "integrations"):
                        old = last_check.get(key, 0)
                        new = current[key]
                        if new > old:
                            diff_parts.append(f"{key}: {old}->{new}")
                    run_strategist_review.apply_async(
                        args=[ws.id, f"readiness_changed: {', '.join(diff_parts)}"],
                        countdown=5,
                    )
                    triggered += 1
                except Exception:
                    logger.warning(
                        "workspace_readiness_check: failed to dispatch strategist for %s",
                        ws.id, exc_info=True,
                    )

            # Save snapshot for next check
            new_settings = dict(settings)
            new_settings["_readiness_snapshot"] = current
            ws.settings = new_settings

        await db.commit()
    return triggered
