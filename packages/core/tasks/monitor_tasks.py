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
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.core.constants.goals import GoalStatus
from packages.core.constants.task import TaskStatus
from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)

MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() != "false"
HEARTBEAT_OK_MARKER = "HEARTBEAT_OK"
# Skip proactive messages if the conversation already has a recent assistant
# message within this many minutes.
HEARTBEAT_COOLDOWN_MINUTES = 30


def _run_async(coro):
    """Run an async coroutine from synchronous Celery worker context."""
    return asyncio.run(coro)


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
    from packages.core.services.workspace_health_metrics import stalled_task_clause

    # Total open tasks
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED,)),
        )
    )
    open_count = r.scalar_one()

    # Overdue tasks (have a deadline in the past, still not completed)
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED,)),
            Task.deadline.isnot(None),
            task_deadline_overdue_expr(Task.deadline, now_expr=now, current_date_expr=today_start.date()),
        )
    )
    overdue_count = r.scalar_one()

    # Blocked tasks
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status == TaskStatus.BLOCKED,
        )
    )
    blocked_count = r.scalar_one()

    # Completed today
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            Task.status == TaskStatus.COMPLETED,
            Task.completed_at >= today_start,
        )
    )
    completed_today = r.scalar_one()

    # Stalled: in progress with no status change for STALLED_AFTER — the
    # same definition the launch review and Workspace health use.
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == entity_id,
            stalled_task_clause(now),
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
    from packages.core.services.workspace_health_metrics import success_rate

    AGENT_EXECUTION_SUCCEEDED = "completed"
    AGENT_EXECUTION_FAILED = ("failed", "error")

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
        return {"total": 0, "success_rate": None, "avg_turns": 0.0, "total_tokens": 0}

    # Only finished executions are judged: running and cancelled ones are not.
    status_counts = dict((await db.execute(
        select(AgentExecution.status, func.count()).where(
            AgentExecution.entity_id == entity_id,
            AgentExecution.created_at >= since,
            AgentExecution.status.in_(
                (AGENT_EXECUTION_SUCCEEDED,) + AGENT_EXECUTION_FAILED
            ),
        ).group_by(AgentExecution.status)
    )).all())
    rate = success_rate(
        status_counts.get(AGENT_EXECUTION_SUCCEEDED, 0),
        sum(status_counts.get(status, 0) for status in AGENT_EXECUTION_FAILED),
    )

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
        # Percent of judged executions; None when none finished.
        "success_rate": None if rate is None else round(rate * 100, 1),
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
            ExecutionPlan.status.in_((ExecutionPlanStatus.RUNNING, ExecutionPlanStatus.PENDING_APPROVAL, ExecutionPlanStatus.PAUSED,)),
        )
    )
    active_plans = r.scalar_one()

    # Plans with steps waiting on human input
    r = await db.execute(
        select(func.count(func.distinct(ExecutionStep.plan_id))).where(
            ExecutionStep.entity_id == entity_id,
            ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN,
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
        alerts.append(f"{task_stats['stalled']} stalled task(s) (no status change in 48h)")
    if job_stats["broken"] > 0:
        alerts.append(f"{job_stats['broken']} scheduled job(s) with repeated failures")
    if goal_stats["stuck_hitl"] > 0:
        alerts.append(f"{goal_stats['stuck_hitl']} goal(s) waiting for human input")
    alert_text = "; ".join(alerts) if alerts else "All clear"
    exec_success = (
        "n/a" if exec_stats.get("success_rate") is None else f"{exec_stats['success_rate']}%"
    )

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
        f"{exec_success} success | avg {exec_stats['avg_turns']} turns\n"
        f"  Scheduled Jobs: {job_stats['enabled']} enabled | "
        f"{job_stats['errored']} errored | {job_stats['broken']} broken\n"
        f"  Goals: {goal_stats['active']} active | {goal_stats['stuck_hitl']} awaiting human input\n"
        f"  Alerts: {alert_text}\n"
        f"\n"
        f"--- Business Overview ---\n"
        f"  Open workload: {task_stats['open']} tasks across all workspaces\n"
        f"  Stalled work: {task_stats['stalled']} task(s) with no status change in 48h\n"
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
    from packages.core.services.workspace_health_metrics import stalled_task_clause

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
            Task.status.in_((TaskStatus.PENDING, TaskStatus.IN_PROGRESS,)),
            Task.deadline.isnot(None),
            task_deadline_overdue_expr(Task.deadline, now_expr=now, current_date_expr=now.date()),
        )
    )
    overdue_tasks = r.scalar_one()
    if overdue_tasks > 0:
        attention_items.append(f"{overdue_tasks} overdue task(s) assigned to me")

    # 2. Stalled tasks — the shared stall definition (no status change in 48h)
    r = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.entity_id == conv.entity_id,
            Task.agent_id == conv.agent_id,
            stalled_task_clause(now),
        )
    )
    stalled_tasks = r.scalar_one()
    if stalled_tasks > 0:
        attention_items.append(f"{stalled_tasks} stalled task(s) with no status change in 48h")

    # 3. Plans with steps waiting on human input.
    # The old query joined by conversation_id; the new ExecutionPlan
    # doesn't carry that field directly, so we scope to entity-wide
    # pending HITL — the heartbeat message is per-conversation but the
    # surfaced count is "plans needing your attention overall", which
    # is actually the more useful framing for a single-operator system.
    r = await db.execute(
        select(func.count(func.distinct(ExecutionStep.plan_id))).where(
            ExecutionStep.entity_id == conv.entity_id,
            ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN,
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
    """Remind people about cards waiting on them (24h, then +72h)."""
    if not MONITOR_ENABLED:
        logger.info("hitl_waiting_reminder: disabled via MONITOR_ENABLED=false")
        return

    try:
        from packages.core.services.hitl_reminders import run_hitl_reminder_sweep

        delivered = _run_async(run_hitl_reminder_sweep())
        if delivered:
            logger.info("hitl_waiting_reminder: delivered %d reminder(s)", delivered)
    except Exception as exc:
        logger.error("hitl_waiting_reminder failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(name="workspace.digest_sweep")
def workspace_digest_sweep() -> int:
    """Send each person's daily Workspace digest at 09:00 their time (every 15 min)."""
    if not MONITOR_ENABLED:
        logger.info("workspace_digest_sweep: disabled via MONITOR_ENABLED=false")
        return 0
    from packages.core.services.workspace_digest import run_workspace_digest_sweep

    sent = _run_async(run_workspace_digest_sweep())
    if sent:
        logger.info("workspace_digest_sweep: sent %d digest(s)", sent)
    return sent


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
    from packages.core.services.notification_service import create_notification_once
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
    )

    triggered = 0
    async with create_worker_session()() as db:
        # Connection monitoring applies to every live Blueprint Workspace;
        # the legacy count-based Strategist trigger remains heartbeat-only.
        workspaces = list((await db.execute(
            select(Workspace).where(
                Workspace.status.in_((
                    "active",
                    "paused",
                    "provisioning",
                    "needs_setup",
                )),
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all())

        for ws in workspaces:
            settings = ws.settings or {}
            last_check = settings.get("_readiness_snapshot", {})
            last_check = last_check if isinstance(last_check, dict) else {}
            current = dict(last_check) if isinstance(last_check, dict) else {}
            changed = False
            if ws.heartbeat_enabled and ws.status == "active":
                agent_count = (await db.execute(
                    select(func.count()).select_from(AgentSubscription).where(
                        AgentSubscription.workspace_id == ws.id,
                        AgentSubscription.status == "active",
                    )
                )).scalar_one()
                goal_count = (await db.execute(
                    select(func.count()).select_from(Goal).where(
                        Goal.workspace_id == ws.id,
                        Goal.status == GoalStatus.ACTIVE.value,
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
                changed = any(
                    current[key] > last_check.get(key, 0)
                    for key in ("agents", "goals", "integrations")
                )

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
                    from packages.core.strategist import (
                        ReviewTrigger, ReviewTriggerKind,
                    )
                    run_strategist_review.apply_async(
                        args=[ws.id],
                        kwargs={
                            **ReviewTrigger(
                                kind=ReviewTriggerKind.EVENT,
                                detail=f"readiness changed: {', '.join(diff_parts)}",
                            ).celery_kwargs(),
                            "user_id": (ws.settings or {}).get("created_by_user_id"),
                        },
                        countdown=5,
                    )
                    triggered += 1
                except Exception:
                    logger.warning(
                        "workspace_readiness_check: failed to dispatch strategist for %s",
                        ws.id, exc_info=True,
                    )

            # Save snapshot for next check
            from packages.core.services.workspace_access import (
                lock_workspace_access_boundary,
            )

            locked_ws = await lock_workspace_access_boundary(
                db,
                workspace_id=ws.id,
                entity_id=ws.entity_id,
            )
            if locked_ws is None or locked_ws.deleted_at is not None:
                await db.rollback()
                continue
            new_settings = dict(locked_ws.settings or {})
            new_settings["_readiness_snapshot"] = current
            blueprint = new_settings.get("_blueprint")
            if isinstance(blueprint, dict):
                setup_status = await evaluate_workspace_blocking_setup(
                    db,
                    locked_ws,
                )
                previous_monitor = blueprint.get("setup_monitor")
                monitor, incident_started = _workspace_setup_monitor_state(
                    setup_status,
                    previous_monitor if isinstance(previous_monitor, dict) else {},
                )
                blueprint = dict(blueprint)
                blueprint["setup_monitor"] = monitor
                new_settings["_blueprint"] = blueprint
                if incident_started:
                    recipient = str(
                        new_settings.get("created_by_user_id") or ""
                    ).strip()
                    if recipient:
                        labels = [
                            item.get("label") or item.get("provider") or item.get("todo_kind")
                            for item in monitor["incomplete"]
                        ]
                        labels = [str(label) for label in labels if label]
                        await create_notification_once(
                            db,
                            locked_ws.entity_id,
                            recipient,
                            "workspace_setup_required",
                            f"{locked_ws.name} needs a connection update",
                            body=(
                                "Reconnect or configure the required Blueprint "
                                "dependencies before normal Workspace work resumes."
                            ),
                            link=f"/workspaces/{locked_ws.id}?tab=overview",
                            meta={
                                "incident": monitor["incident"],
                                "requirements": monitor["incomplete"],
                                "labels": labels,
                            },
                            workspace_id=locked_ws.id,
                            idempotency_key=(
                                f"workspace-setup:{locked_ws.id}:"
                                f"{monitor['incident']}"
                            ),
                        )
            locked_ws.settings = new_settings
            startup_workspace_id = str(locked_ws.id)
            await db.commit()
            await _reconcile_blueprint_startup_after_monitor(
                db,
                workspace_id=startup_workspace_id,
                settings=new_settings,
            )

    return triggered


async def _reconcile_blueprint_startup_after_monitor(
    db,
    *,
    workspace_id: str,
    settings: dict[str, Any] | None,
) -> None:
    current_settings = settings if isinstance(settings, dict) else {}
    startup = current_settings.get("blocking_setup")
    if not isinstance(startup, dict):
        return
    checks = startup.get("checks")
    opted_in = bool(str(startup.get("on_ready_job_id") or "").strip()) or any(
        isinstance(check, dict)
        and str(check.get("setup_job_id") or "").strip()
        for check in (checks if isinstance(checks, list) else [])
    )
    if not opted_in:
        return
    try:
        from packages.core.services.blueprint_startup_service import (
            reconcile_blueprint_startup,
        )

        await reconcile_blueprint_startup(
            db,
            workspace_id=workspace_id,
            trigger="readiness_monitor",
        )
    except Exception:
        await db.rollback()
        logger.exception(
            "workspace_readiness_check: Blueprint startup reconciliation "
            "failed for %s",
            workspace_id,
        )


def _workspace_setup_monitor_state(
    setup_status: Any,
    previous: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return a stable connection-incident snapshot and transition signal."""
    details = getattr(setup_status, "details", None)
    raw_incomplete = (
        details.get("incomplete_checks")
        if isinstance(details, dict)
        else []
    )
    connection_todo_kinds = {
        "channel",
        "browser_session",
        "missing_integration",
        "mcp_server",
        "mcp_configuration",
    }
    incomplete: list[dict[str, str]] = []
    for check in raw_incomplete or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("kind") or "").strip()
        todo_kind = str(check.get("todo_kind") or "").strip()
        if kind != "integration_provider" and todo_kind not in connection_todo_kinds:
            continue
        incomplete.append({
            key: str(check.get(key) or "").strip()
            for key in (
                "key",
                "kind",
                "todo_kind",
                "provider",
                "setup_kind",
                "label",
            )
            if str(check.get(key) or "").strip()
        })
    incomplete.sort(key=lambda item: json.dumps(item, sort_keys=True))
    fingerprint = (
        hashlib.sha256(
            json.dumps(incomplete, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        if incomplete
        else ""
    )
    prior_fingerprint = str(previous.get("fingerprint") or "")
    incident = int(previous.get("incident") or 0)
    incident_started = bool(fingerprint and fingerprint != prior_fingerprint)
    if incident_started:
        incident += 1
    return ({
        "ready": not incomplete,
        "fingerprint": fingerprint,
        "incident": incident,
        "incomplete": incomplete,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }, incident_started)


# ---------------------------------------------------------------------------
# Experiment guardrail tick (M13) — deterministic, no LLM
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="experiments.guardrail_tick", max_retries=0)
def experiment_guardrail_tick(self):
    """Sweep running experiments against their guardrails (M13).

    Stops an experiment (and removes its overlay from the target) on
    consecutive cohort failures, on reaching ``scope.max_runs``, or when
    ``ends_at`` has passed — then auto-evaluates it. Pure DB arithmetic.
    """
    if not MONITOR_ENABLED:
        return
    try:
        acted = _run_async(_async_experiment_guardrail_tick())
        if acted:
            logger.info("experiment_guardrail_tick: acted on %d experiment(s): %s",
                        len(acted), acted)
    except Exception as exc:
        logger.error("experiment_guardrail_tick failed: %s", exc, exc_info=True)


async def _async_experiment_guardrail_tick() -> list[dict]:
    from packages.core.database import create_worker_session
    from packages.core.experiments import check_experiment_guardrails

    async with create_worker_session()() as db:
        results = await check_experiment_guardrails(db)
        await db.commit()
    return results
