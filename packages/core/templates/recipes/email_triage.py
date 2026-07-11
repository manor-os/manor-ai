"""Email triage — daily inbox-zero recipe.

Mints:
  * 1 Goal — "median inbox response time" toward a per-day target
    (defaults to 4 hours), measured daily off the email integration.
  * 2 seed Tasks — "morning triage" + "afternoon sweep" — recurring
    cues the operator can promote to ScheduledJobs once they pick a
    cadence that fits their day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.templates.registry import (
    Template,
    TemplateInput,
    TemplateResult,
    register,
)


@dataclass
class EmailTriageTemplate:
    key: str = "email_triage"
    title: str = "Email Triage"
    summary: str = (
        "Reach inbox-zero by EOD: morning + afternoon triage tasks plus a "
        "daily 'median response time' goal."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "required": ["mailbox"],
                "properties": {
                    "mailbox": {"type": "string"},
                    "target_response_hours": {"type": "integer"},
                    "horizon_days": {"type": "integer"},
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from packages.core.goals import create_goal
        from packages.core.services.task_service import create_task

        params = inp.params
        mailbox = params["mailbox"]
        target_hours = int(params.get("target_response_hours", 4))
        horizon = int(params.get("horizon_days", 30))
        deadline = date.today() + timedelta(days=horizon)

        goal = await create_goal(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            title=f"Median inbox response ≤ {target_hours}h ({mailbox})",
            description=(
                f"Keep the median time-to-first-reply on {mailbox} under "
                f"{target_hours} hours over the trailing 7 days."
            ),
            metric_key="median_response_hours",
            target_value=target_hours,
            deadline=deadline,
            measurement_source={
                "provider": "gmail",
                "action": "gmail.compute_median_response",
                "params": {"mailbox": mailbox, "window_days": 7},
            },
            measurement_cadence="daily",
            priority=3,
        )

        task_ids: list[str] = []
        for title, body, role in [
            (
                "Morning triage",
                "Scan the overnight inbox; reply to anything blocking someone, "
                "snooze the rest with reasoning, archive marketing.",
                "morning",
            ),
            (
                "Afternoon sweep",
                "Quick second pass before EOD — anything still red gets a "
                "short status reply or a same-day follow-up.",
                "afternoon",
            ),
        ]:
            t = await create_task(
                db, inp.entity_id,
                title=title,
                description=body,
                priority=3,
                task_type="triage",
                workspace_id=inp.workspace_id,
                creator_id=inp.user_id,
                details={"template_key": self.key, "template_role": role,
                         "mailbox": mailbox},
            )
            task_ids.append(t.id)

        return TemplateResult(
            template_key=self.key,
            goal_id=goal.id,
            task_ids=task_ids,
            notes=[
                f"Daily goal: keep 7-day median response on {mailbox} ≤ {target_hours}h.",
                "2 starter tasks seeded — promote either to a recurring job once you pick a cadence.",
            ],
        )


register(EmailTriageTemplate())
