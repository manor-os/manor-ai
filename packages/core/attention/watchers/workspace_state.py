"""Situations read straight off a Workspace's own state.

Each reads a field the platform already maintains (the readiness monitor's
setup snapshot, the scheduler's error count, the budget alert bucket, the
goal's measured pace); none computes anything expensive or calls a model.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.attention.contracts import DesiredItem
from packages.core.constants.attention import AttentionKind, AttentionUrgency
from packages.core.constants.goals import GoalStatus
from packages.core.constants.workspaces import LIVE_WORKSPACE_STATUSES
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Workspace


class SetupIncompleteWatcher:
    """A Blueprint Workspace's required connections are not set up.

    Reads ``settings._blueprint.setup_monitor``, which the readiness monitor
    (monitor.workspace_readiness_check) keeps current.
    """

    key = "setup_incomplete"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        workspaces = (await db.execute(
            select(Workspace).where(
                Workspace.deleted_at.is_(None),
                Workspace.status.in_(LIVE_WORKSPACE_STATUSES),
                Workspace.settings["_blueprint"]["setup_monitor"]["ready"].astext == "false",
            )
        )).scalars().all()
        items: list[DesiredItem] = []
        for workspace in workspaces:
            monitor = ((workspace.settings or {}).get("_blueprint") or {}).get("setup_monitor") or {}
            incomplete = [check for check in monitor.get("incomplete") or [] if isinstance(check, dict)]
            if not incomplete:
                continue
            labels = [
                str(check.get("label") or check.get("provider") or check.get("key") or "a connection")
                for check in incomplete
            ]
            checked_at = monitor.get("checked_at")
            try:
                opened_at = datetime.fromisoformat(checked_at) if isinstance(checked_at, str) else now
            except ValueError:
                opened_at = now
            items.append(DesiredItem(
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                kind=AttentionKind.SETUP_INCOMPLETE.value,
                subject_type="workspace_setup",
                subject_id=workspace.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title="Setup still needs: " + ", ".join(labels[:3]) + (
                    f" and {len(labels) - 3} more" if len(labels) > 3 else ""
                ),
                opened_at=opened_at,
                evidence={"incomplete": incomplete, "fingerprint": monitor.get("fingerprint")},
            ))
        return items


class AutomationPausedWatcher:
    """An automation the scheduler paused after repeated failures."""

    key = "automation_paused"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        from packages.core.services.scheduler_service import (
            SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD,
            is_agent_initiative_driver,
        )

        jobs = (await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.enabled.is_(False),
                ScheduledJob.workspace_id.is_not(None),
                ScheduledJob.consecutive_errors >= SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD,
            )
        )).scalars().all()
        items: list[DesiredItem] = []
        for job in jobs:
            if is_agent_initiative_driver(job):
                continue  # Initiative drivers back off; they are never paused.
            items.append(DesiredItem(
                entity_id=job.entity_id,
                workspace_id=job.workspace_id,
                kind=AttentionKind.AUTOMATION_PAUSED.value,
                subject_type="scheduled_job",
                subject_id=job.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title=f"Automation paused after {int(job.consecutive_errors or 0)} failed runs: "
                      f"{job.name or job.job_id}",
                opened_at=job.last_run_at or job.updated_at or now,
                evidence={
                    "label": job.name or job.job_id, "job_id": job.job_id,
                    "consecutive_errors": int(job.consecutive_errors or 0),
                },
                assignee_user_id=job.user_id,
            ))
        return items


class BudgetThresholdWatcher:
    """The Workspace has used 80% or all of its monthly budget."""

    key = "budget_threshold"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        workspaces = (await db.execute(
            select(Workspace).where(
                Workspace.deleted_at.is_(None),
                Workspace.budget_alert_state.in_(("warning_80", "critical_100")),
            )
        )).scalars().all()
        items: list[DesiredItem] = []
        for workspace in workspaces:
            used_up = workspace.budget_alert_state == "critical_100"
            items.append(DesiredItem(
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                kind=AttentionKind.BUDGET_THRESHOLD.value,
                subject_type="workspace_budget",
                subject_id=workspace.id,
                urgency=(AttentionUrgency.ACT_TODAY if used_up else AttentionUrgency.FYI).value,
                # The same row moves from 80% to used up.
                stage=1 if used_up else 0,
                title="Monthly budget used up" if used_up else "80% of the monthly budget used",
                opened_at=workspace.budget_reset_at or now,
                evidence={
                    "budget_alert_state": workspace.budget_alert_state,
                    "monthly_spent_usd": str(workspace.monthly_spent_usd or 0),
                    "monthly_budget_usd": str(workspace.monthly_budget_usd or 0),
                },
            ))
        return items


class GoalOffPaceWatcher:
    """An active goal measured at risk or behind. Only Workspaces with goals."""

    key = "goal_off_pace"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        goals = (await db.execute(
            select(Goal).where(
                Goal.status == GoalStatus.ACTIVE.value,
                Goal.workspace_id.is_not(None),
                Goal.pace_status.in_(("at_risk", "behind")),
            )
        )).scalars().all()
        items: list[DesiredItem] = []
        for goal in goals:
            # at_risk (< 60% of the expected pace) is worse than behind (60-90%).
            at_risk = goal.pace_status == "at_risk"
            items.append(DesiredItem(
                entity_id=goal.entity_id,
                workspace_id=goal.workspace_id,
                kind=AttentionKind.GOAL_OFF_PACE.value,
                subject_type="goal",
                subject_id=goal.id,
                urgency=(AttentionUrgency.ACT_TODAY if at_risk else AttentionUrgency.FYI).value,
                stage=1 if at_risk else 0,
                title=f"Goal {'at risk' if at_risk else 'behind'}: {goal.title}",
                opened_at=goal.current_value_updated_at or goal.updated_at or now,
                evidence={
                    "goal_id": goal.id,
                    "pace_status": goal.pace_status,
                    "current_value": str(goal.current_value) if goal.current_value is not None else None,
                    "target_value": str(goal.target_value) if goal.target_value is not None else None,
                },
            ))
        return items
