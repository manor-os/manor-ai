"""Product activation and retention metrics.

Mutable runtime rows power current activity while irreversible automation
milestones come from the idempotent product-growth fact table.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.analytics import ProductGrowthMilestone
from packages.core.constants.task import TaskStatus
from packages.core.models.product_growth import ProductGrowthEvent
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.user_session import UserSessionLog
from packages.core.models.workspace import WorkspaceActivity


RETENTION_DAYS = (1, 7, 30)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _earliest_by_user(rows: Iterable[tuple[Any, Any]]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for raw_user_id, raw_at in rows:
        if not raw_user_id or raw_at is None:
            continue
        user_id = str(raw_user_id)
        occurred_at = _as_utc(raw_at)
        previous = result.get(user_id)
        if previous is None or occurred_at < previous:
            result[user_id] = occurred_at
    return result




async def get_product_growth_metrics(
    db: AsyncSession,
    *,
    days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return platform activation and exact-day retention for signup cohorts.

    Definitions:
    - cohort membership: non-deleted users created in the UTC date window;
    - active day: every UTC day covered by a browser session's observed span;
    - value activation: a completed user-created Task or a successful
      Automation run;
    - D1/D7/D30: active on that exact calendar-day offset, with only cohorts
      old enough to have reached the offset included in the denominator.
    """
    days = max(1, min(int(days), 365))
    now = _as_utc(now or datetime.now(timezone.utc))
    today = now.date()
    cohort_start_day = today - timedelta(days=days - 1)
    cohort_start = datetime.combine(
        cohort_start_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    activity_start_day = min(cohort_start_day, today - timedelta(days=29))
    activity_start = datetime.combine(
        activity_start_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    user_rows = (await db.execute(
        select(User.id, User.created_at).where(
            User.deleted_at.is_(None),
            User.created_at >= cohort_start,
            User.created_at <= now,
        )
    )).all()
    signup_at_by_user = {
        str(user_id): _as_utc(created_at)
        for user_id, created_at in user_rows
    }
    user_ids = list(signup_at_by_user)

    # One bounded session scan powers both platform DAU/WAU/MAU and cohort
    # retention. The User join ensures soft-deleted identities are excluded.
    session_seen_at = func.coalesce(
        UserSessionLog.last_seen_at,
        UserSessionLog.ended_at,
        UserSessionLog.started_at,
    )
    session_started_at = func.coalesce(
        UserSessionLog.started_at,
        session_seen_at,
    )
    session_rows = (await db.execute(
        select(UserSessionLog.user_id, session_started_at, session_seen_at)
        .join(User, User.id == UserSessionLog.user_id)
        .where(
            User.deleted_at.is_(None),
            session_seen_at >= activity_start,
            session_started_at <= now,
            session_seen_at <= now,
        )
    )).all()
    active_days_by_user: dict[str, set[date]] = defaultdict(set)
    for raw_user_id, raw_started_at, raw_seen_at in session_rows:
        if (
            not raw_user_id
            or raw_started_at is None
            or raw_seen_at is None
        ):
            continue
        first_day = max(activity_start_day, _as_utc(raw_started_at).date())
        last_day = min(today, _as_utc(raw_seen_at).date())
        while first_day <= last_day:
            active_days_by_user[str(raw_user_id)].add(first_day)
            first_day += timedelta(days=1)

    milestones: dict[str, dict[str, datetime]] = {
        "workspace_created": {},
        "task_completed": {},
        "automation_created": {},
        "automation_succeeded": {},
    }
    if user_ids:
        workspace_rows = (await db.execute(
            select(WorkspaceActivity.user_id, func.min(WorkspaceActivity.created_at))
            .where(
                WorkspaceActivity.user_id.in_(user_ids),
                WorkspaceActivity.event_type.in_(("workspace.created", "workspace_created")),
            )
            .group_by(WorkspaceActivity.user_id)
        )).all()
        milestones["workspace_created"] = _earliest_by_user(workspace_rows)

        task_completed_at = func.coalesce(
            Task.completed_at,
            Task.status_changed_at,
            Task.updated_at,
            Task.created_at,
        )
        task_rows = (await db.execute(
            select(Task.creator_id, func.min(task_completed_at))
            .where(
                Task.creator_id.in_(user_ids),
                Task.status == TaskStatus.COMPLETED,
            )
            .group_by(Task.creator_id)
        )).all()
        milestones["task_completed"] = _earliest_by_user(task_rows)

        automation_event_rows = (await db.execute(
            select(
                ProductGrowthEvent.user_id,
                ProductGrowthEvent.milestone,
                func.min(ProductGrowthEvent.occurred_at),
            )
            .where(
                ProductGrowthEvent.user_id.in_(user_ids),
                ProductGrowthEvent.milestone.in_([
                    ProductGrowthMilestone.AUTOMATION_CREATED.value,
                    ProductGrowthMilestone.AUTOMATION_SUCCEEDED.value,
                ]),
            )
            .group_by(
                ProductGrowthEvent.user_id,
                ProductGrowthEvent.milestone,
            )
        )).all()
        automation_milestones: dict[str, dict[str, datetime]] = defaultdict(dict)
        for event_user_id, milestone, occurred_at in automation_event_rows:
            if not event_user_id or occurred_at is None:
                continue
            automation_milestones[str(milestone)][str(event_user_id)] = _as_utc(
                occurred_at
            )
        milestones["automation_created"] = automation_milestones[
            ProductGrowthMilestone.AUTOMATION_CREATED.value
        ]
        milestones["automation_succeeded"] = automation_milestones[
            ProductGrowthMilestone.AUTOMATION_SUCCEEDED.value
        ]

    # Defensive temporal filter: malformed/imported rows predating the user do
    # not qualify as activation.
    milestone_users: dict[str, set[str]] = {}
    for key, timestamps in milestones.items():
        milestone_users[key] = {
            user_id
            for user_id, occurred_at in timestamps.items()
            if user_id in signup_at_by_user
            and signup_at_by_user[user_id] <= occurred_at <= now
        }

    value_activated_users = (
        milestone_users["task_completed"]
        | milestone_users["automation_succeeded"]
    )


    registered = len(user_ids)
    milestone_labels = {
        "workspace_created": "Created a workspace",
        "task_completed": "Completed a task",
        "automation_created": "Created an automation",
        "automation_succeeded": "Ran an automation successfully",
    }
    activation_milestones = [
        {
            "key": key,
            "label": milestone_labels[key],
            "users": len(milestone_users[key]),
            "rate": _rate(len(milestone_users[key]), registered),
        }
        for key in milestone_labels
    ]

    retention = []
    for offset in RETENTION_DAYS:
        eligible = {
            user_id
            for user_id, signup_at in signup_at_by_user.items()
            if signup_at.date() <= today - timedelta(days=offset)
        }
        retained = {
            user_id
            for user_id in eligible
            if signup_at_by_user[user_id].date() + timedelta(days=offset)
            in active_days_by_user.get(user_id, set())
        }
        retention.append({
            "day": offset,
            "eligible_users": len(eligible),
            "retained_users": len(retained),
            "rate": _rate(len(retained), len(eligible)),
        })

    cohorts = []
    for index in range(days):
        cohort_day = cohort_start_day + timedelta(days=index)
        cohort_users = {
            user_id
            for user_id, signup_at in signup_at_by_user.items()
            if signup_at.date() == cohort_day
        }
        cohort_value_users = cohort_users & value_activated_users
        cohort_retention = []
        for offset in RETENTION_DAYS:
            is_mature = cohort_day <= today - timedelta(days=offset)
            retained_count = (
                sum(
                    cohort_day + timedelta(days=offset)
                    in active_days_by_user.get(user_id, set())
                    for user_id in cohort_users
                )
                if is_mature
                else 0
            )
            eligible_count = len(cohort_users) if is_mature else 0
            cohort_retention.append({
                "day": offset,
                "eligible_users": eligible_count,
                "retained_users": retained_count,
                "rate": _rate(retained_count, eligible_count),
            })
        cohorts.append({
            "date": cohort_day.isoformat(),
            "signups": len(cohort_users),
            "value_activated_users": len(cohort_value_users),
            "activation_rate": _rate(len(cohort_value_users), len(cohort_users)),
            "retention": cohort_retention,
        })

    active_users = {
        "dau": sum(today in dates for dates in active_days_by_user.values()),
        "wau": sum(
            any(today - timedelta(days=6) <= day <= today for day in dates)
            for dates in active_days_by_user.values()
        ),
        "mau": sum(
            any(today - timedelta(days=29) <= day <= today for day in dates)
            for dates in active_days_by_user.values()
        ),
    }

    return {
        "days": days,
        "active_users": active_users,
        "activation": {
            "cohort_users": registered,
            "value_activated_users": len(value_activated_users),
            "value_activation_rate": _rate(len(value_activated_users), registered),
            "milestones": activation_milestones,
        },
        "retention": retention,
        "cohorts": cohorts,
        "generated_at": now.isoformat(),
    }
