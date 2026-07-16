"""Dashboard analytics service — aggregate stats, trends, and activity feeds."""
from __future__ import annotations

from datetime import date

from sqlalchemy import Date, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Conversation, Task
from packages.core.models.document import Document, VectorStatus
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.models.people import Client
from packages.core.models.staff import Staff
from packages.core.models.usage import TokenUsageLog
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.services.task_deadlines import task_deadline_overdue_expr
from packages.core.services.timezone_utils import (
    user_current_date,
    user_day_bounds_utc,
    user_range_start_utc,
    user_timezone_name,
    utc_now,
)


async def get_dashboard_stats(
    db: AsyncSession, entity_id: str, *, workspace_id: str | None = None,
    timezone_name: str | None = None,
) -> dict:
    """Main dashboard stats -- single query per table using COUNT/SUM."""
    now = utc_now()
    today = user_current_date(timezone_name, now)
    today_start, today_end = user_day_bounds_utc(timezone_name, today, now=now)

    # ── Tasks ──
    task_q = select(
        func.count().label("total"),
        func.count().filter(Task.status == "pending").label("pending"),
        func.count().filter(Task.status == "proposed").label("proposed"),
        func.count().filter(Task.status == "in_progress").label("in_progress"),
        func.count().filter(Task.status == "completed").label("completed"),
        func.count().filter(Task.status == "failed").label("failed"),
        func.count().filter(Task.status == "cancelled").label("cancelled"),
        func.count().filter(Task.status == "waiting_on_customer").label("waiting_on_customer"),
        func.count().filter(
            task_deadline_overdue_expr(
                Task.deadline,
                now_expr=now,
                current_date_expr=today,
            )
            & (Task.status.notin_(["completed", "cancelled", "failed"]))
        ).label("overdue"),
    ).where(
        Task.entity_id == entity_id,
        # Exclude automation-linked tasks (same filter as task_service.list_tasks)
        Task.details["scheduled_job_id"].astext.is_(None),
    )
    if workspace_id:
        task_q = task_q.where(Task.workspace_id == workspace_id)
    t = (await db.execute(task_q)).one()

    # ── Documents ──
    doc_q = select(
        func.count().label("total"),
        func.count().filter(Document.vector_status == VectorStatus.READY).label("indexed"),
    ).where(Document.entity_id == entity_id)
    d = (await db.execute(doc_q)).one()

    # ── Agents ──
    agent_total_q = select(func.count()).select_from(Agent).where(
        (Agent.entity_id == entity_id) | (Agent.is_template.is_(True))
    )
    agent_total = (await db.execute(agent_total_q)).scalar() or 0

    sub_q = select(func.count()).select_from(AgentSubscription).where(
        AgentSubscription.entity_id == entity_id
    )
    if workspace_id:
        sub_q = sub_q.where(AgentSubscription.workspace_id == workspace_id)
    agent_subscribed = (await db.execute(sub_q)).scalar() or 0

    # ── Conversations ──
    conv_q = select(
        func.count().label("total"),
        func.count().filter(
            (Conversation.created_at >= today_start)
            & (Conversation.created_at < today_end)
        ).label("today"),
    ).where(Conversation.entity_id == entity_id)
    if workspace_id:
        conv_q = conv_q.where(Conversation.workspace_id == workspace_id)
    c = (await db.execute(conv_q)).one()

    # ── Clients ──
    client_q = select(
        func.count().label("total"),
        func.count().filter(Client.status == "active").label("active"),
    ).where(
        (Client.entity_id == entity_id) & (Client.deleted_at.is_(None))
    )
    cl = (await db.execute(client_q)).one()

    # ── Staff ──
    staff_q = select(func.count()).select_from(Staff).where(
        Staff.entity_id == entity_id,
        Staff.deleted_at.is_(None),
    )
    staff_total = (await db.execute(staff_q)).scalar() or 0

    # ── Usage ──
    usage_q = select(
        func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("total_cost"),
        func.coalesce(
            func.sum(TokenUsageLog.total_tokens).filter(
                (TokenUsageLog.created_at >= today_start)
                & (TokenUsageLog.created_at < today_end)
            ),
            0,
        ).label("today_tokens"),
    ).where(TokenUsageLog.entity_id == entity_id)
    u = (await db.execute(usage_q)).one()

    return {
        "tasks": {
            "total": int(t.total),
            "by_status": {
                "proposed": int(t.proposed),
                "pending": int(t.pending),
                "in_progress": int(t.in_progress),
                "completed": int(t.completed),
                "failed": int(t.failed),
                "cancelled": int(t.cancelled),
                "waiting_on_customer": int(t.waiting_on_customer),
            },
            "overdue": int(t.overdue),
        },
        "documents": {"total": int(d.total), "indexed": int(d.indexed)},
        "agents": {"total": int(agent_total), "subscribed": int(agent_subscribed)},
        "conversations": {"total": int(c.total), "today": int(c.today)},
        "clients": {"total": int(cl.total), "active": int(cl.active)},
        "staff": {"total": int(staff_total)},
        "usage": {
            "total_tokens": int(u.total_tokens),
            "total_cost": float(u.total_cost),
            "today_tokens": int(u.today_tokens),
        },
    }


async def get_task_trends(
    db: AsyncSession, entity_id: str, days: int = 30,
    *, workspace_id: str | None = None,
    timezone_name: str | None = None,
) -> list[dict]:
    """Task creation/completion trends by day."""
    tz_name = user_timezone_name(timezone_name)
    cutoff = user_range_start_utc(timezone_name, days)
    created_day = cast(func.timezone(tz_name, Task.created_at), Date)
    completed_day = cast(func.timezone(tz_name, Task.completed_at), Date)

    created_where = (Task.entity_id == entity_id) & (Task.created_at >= cutoff)
    completed_where = (
        (Task.entity_id == entity_id)
        & (Task.completed_at.isnot(None))
        & (Task.completed_at >= cutoff)
    )
    if workspace_id:
        created_where = created_where & (Task.workspace_id == workspace_id)
        completed_where = completed_where & (Task.workspace_id == workspace_id)

    created_q = (
        select(
            created_day.label("day"),
            func.count().label("created"),
        )
        .where(created_where)
        .group_by(text("1"))
    )

    completed_q = (
        select(
            completed_day.label("day"),
            func.count().label("completed"),
        )
        .where(completed_where)
        .group_by(text("1"))
    )

    created_rows = (await db.execute(created_q)).all()
    completed_rows = (await db.execute(completed_q)).all()

    # Merge into a single dict keyed by date
    merged: dict[date, dict] = {}
    for row in created_rows:
        d = row.day.date() if hasattr(row.day, "date") else row.day
        merged.setdefault(d, {"date": d.isoformat(), "created": 0, "completed": 0})
        merged[d]["created"] = int(row.created)
    for row in completed_rows:
        d = row.day.date() if hasattr(row.day, "date") else row.day
        merged.setdefault(d, {"date": d.isoformat(), "created": 0, "completed": 0})
        merged[d]["completed"] = int(row.completed)

    return sorted(merged.values(), key=lambda x: x["date"])


async def get_usage_trends(
    db: AsyncSession, entity_id: str, days: int = 30,
    *, timezone_name: str | None = None,
) -> list[dict]:
    """Token usage by day."""
    tz_name = user_timezone_name(timezone_name)
    cutoff = user_range_start_utc(timezone_name, days)
    usage_day = cast(func.timezone(tz_name, TokenUsageLog.created_at), Date)

    q = (
        select(
            usage_day.label("day"),
            func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("cost"),
        )
        .where(
            (TokenUsageLog.entity_id == entity_id)
            & (TokenUsageLog.created_at >= cutoff)
        )
        .group_by(text("1"))
        .order_by(text("1"))
    )

    rows = (await db.execute(q)).all()
    return [
        {
            "date": (r.day.date() if hasattr(r.day, "date") else r.day).isoformat(),
            "tokens": int(r.tokens),
            "cost": float(r.cost),
        }
        for r in rows
    ]


async def get_active_plans(
    db: AsyncSession, entity_id: str, limit: int = 5,
    *, workspace_id: str | None = None,
) -> list[dict]:
    """Currently running ExecutionPlans + per-plan progress.

    Replaces the old ``get_active_goals`` (which read from the now-gone
    ``goal_runs`` table). Progress is computed from the ratio of
    ``done`` steps to total steps materialised under the plan.
    """
    from sqlalchemy import func

    q = (
        select(ExecutionPlan)
        .where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.status.in_(
                ["running", "pending_approval", "paused", "draft"]
            ),
        )
        .order_by(ExecutionPlan.updated_at.desc().nullslast())
        .limit(limit)
    )
    if workspace_id:
        q = q.where(ExecutionPlan.workspace_id == workspace_id)
    plans = (await db.execute(q)).scalars().all()

    if not plans:
        return []

    # Single GROUP BY for step counts across all plans we care about.
    plan_ids = [p.id for p in plans]
    from sqlalchemy import case as sa_case
    counts_q = (
        select(
            ExecutionStep.plan_id,
            func.count().label("total"),
            func.sum(
                sa_case((ExecutionStep.step_status == "done", 1), else_=0)
            ).label("done"),
        )
        .where(ExecutionStep.plan_id.in_(plan_ids))
        .group_by(ExecutionStep.plan_id)
    )
    counts = {
        row.plan_id: (row.total or 0, int(row.done or 0))
        for row in (await db.execute(counts_q)).all()
    }

    results = []
    for p in plans:
        total, done = counts.get(p.id, (0, 0))
        progress = round(done / total * 100) if total else 0
        results.append({
            "id": p.id,
            "task_id": p.task_id,
            "status": p.status,
            "execution_mode": p.execution_mode,
            "progress_pct": progress,
            "step_count": total,
            "step_done": done,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })
    return results


# Backward-compat alias — some callers may still import the old name.
get_active_goals = get_active_plans


async def get_recent_activity(
    db: AsyncSession, entity_id: str, limit: int = 10,
    *, workspace_id: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """What Manor AI did recently — task creation and meaningful status changes.

    Simple query: recent tasks ordered by created_at, filtered by status
    changes. Avoids complex OR/nullslast that can fail on some configs.
    """
    visible_statuses = [
        "pending",
        "proposed",
        "in_progress",
        "waiting_on_customer",
        "completed",
        "failed",
    ]
    task_q = (
        select(Task)
        .where(
            Task.entity_id == entity_id,
            Task.status.in_(visible_statuses),
            # Keep scheduler-created internal tasks out of the human dashboard.
            Task.details["scheduled_job_id"].astext.is_(None),
        )
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    if workspace_id:
        task_q = task_q.where(Task.workspace_id == workspace_id)

    try:
        tasks = (await db.execute(task_q)).scalars().all()
    except Exception:
        return []

    results: list[dict] = []
    for t in tasks:
        desc = ""
        action = {
            "pending": "created",
            "proposed": "proposed",
            "in_progress": "in_progress",
            "waiting_on_customer": "waiting_on_customer",
            "completed": "completed",
            "failed": "failed",
        }.get(t.status, t.status)
        try:
            output = t.actual_output if hasattr(t, "actual_output") else None
            if t.status == "pending":
                desc = "Task created"
            elif t.status == "proposed":
                desc = "Proposal ready for review"
            elif t.status == "completed" and output and isinstance(output, dict):
                steps = output.get("steps") or []
                done = sum(1 for s in steps if s.get("status") == "done")
                files = output.get("files") or []
                parts = [f"{done} steps completed"]
                if files:
                    parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''} generated")
                desc = " · ".join(parts)
            elif t.status == "failed" and output and isinstance(output, dict):
                steps = output.get("steps") or []
                failed = [s for s in steps if s.get("status") == "failed"]
                if failed:
                    err = failed[0].get("error") or {}
                    desc = f"Failed: {err.get('message', 'unknown')[:120]}"
                else:
                    desc = "Execution failed"
            elif t.status == "waiting_on_customer":
                desc = "Needs your input to continue"
            elif t.status == "in_progress":
                desc = "Currently running"
        except Exception:
            pass

        ts = None
        try:
            ts = t.completed_at or t.started_at or t.created_at
        except Exception:
            ts = t.created_at

        results.append({
            "id": f"task-{t.id}",
            "type": "task",
            "action": action,
            "name": t.title or "Untitled",
            "description": desc,
            "timestamp": ts.isoformat() if ts else None,
            "task_id": t.id,
        })

    return results
