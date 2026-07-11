"""Daily briefing — the simplest 'is Manor working' recipe.

No goal — just a single recurring task that fires every morning and
posts a structured briefing to chat. Useful for new operators to feel
the system breathing before committing to a full goal-driven recipe.

The Briefing service (M3 Demo B) already runs a daily LLM-driven
inbox triage; this template seeds the trigger row + a placeholder Task
the briefing routes its output into.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.templates.registry import (
    Template,
    TemplateInput,
    TemplateResult,
    register,
)


@dataclass
class DailyBriefingTemplate:
    key: str = "daily_briefing"
    title: str = "Daily Briefing"
    summary: str = (
        "A morning briefing posted to chat — what happened overnight, "
        "what's queued today, what needs your attention."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "properties": {
                    "post_time_local": {"type": "string"},  # 'HH:MM'
                    "tz": {"type": "string"},
                    "include_sections": {"type": "array"},
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from sqlalchemy import select

        from packages.core.briefing.scheduling import (
            install_briefing_schedule,
            resolve_briefing_schedule_settings,
        )
        from packages.core.models.user import User
        from packages.core.models.workspace import Workspace
        from packages.core.services.task_service import create_task

        sections = inp.params.get("include_sections") or [
            "overnight",
            "today",
            "needs_attention",
            "wins",
        ]

        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == inp.workspace_id,
                Workspace.entity_id == inp.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is None:
            raise ValueError(f"workspace {inp.workspace_id} not found")

        user = None
        if inp.user_id:
            user = (await db.execute(
                select(User).where(
                    User.id == inp.user_id,
                    User.entity_id == inp.entity_id,
                    User.deleted_at.is_(None),
                )
            )).scalar_one_or_none()

        post_time, tz = resolve_briefing_schedule_settings(
            workspace=workspace,
            user=user,
            params=inp.params,
        )

        task = await create_task(
            db, inp.entity_id,
            title="Daily briefing",
            description=(
                f"Auto-generated briefing posted at {post_time} {tz}. "
                f"Sections: {', '.join(sections)}."
            ),
            priority=4,
            task_type="briefing",
            workspace_id=inp.workspace_id,
            creator_id=inp.user_id,
            details={
                "template_key": self.key,
                "post_time_local": post_time,
                "tz": tz,
                "sections": sections,
                "is_recurring_anchor": True,
            },
        )

        job = await install_briefing_schedule(
            db,
            workspace,
            time_of_day=post_time,
            timezone=tz,
            user_id=inp.user_id,
        )

        return TemplateResult(
            template_key=self.key,
            goal_id=None,
            task_ids=[task.id],
            scheduled_job_ids=[job.id],
            notes=[
                f"Scheduled briefing anchor task at {post_time} {tz}.",
                "Installed a daily briefing job that posts the generated summary to workspace chat.",
            ],
        )


register(DailyBriefingTemplate())
