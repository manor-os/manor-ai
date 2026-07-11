"""Twitter/X follower growth — the wedge recipe for Demo A.

Mints:
  * 1 Goal — `follower_count` toward operator-supplied target/deadline,
    with a daily measurement_source pointing at the X integration.
  * 3 seed Tasks — "draft tomorrow's posts", "reply to mentions",
    "engage with target accounts" — each at priority 2 so the
    Strategist will queue real work into them on the next tick.

Why these three: the v2 design's wedge pitch is "you sleep, Manor
grows your audience." These cover the production side (posts), the
relationship side (mentions), and the discovery side (engagement) —
the minimum viable surface for measurable follower growth.

Operator can edit/delete any of them; nothing the Strategist later
generates depends on these specific Task IDs.
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
class TwitterGrowthTemplate:
    key: str = "twitter_growth"
    title: str = "Twitter / X Growth"
    summary: str = (
        "Daily follower-count goal + recurring posts, mention replies, "
        "and engagement tasks. Pairs with a captured X session."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "required": ["target_followers", "deadline"],
                "properties": {
                    "target_followers": {"type": "integer"},
                    "current_followers": {"type": "integer"},
                    "deadline": {"type": "string"},  # ISO date
                    "x_handle": {"type": "string"},
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from datetime import date

        from packages.core.goals import create_goal
        from packages.core.services.task_service import create_task

        params = inp.params
        target = int(params["target_followers"])
        baseline = params.get("current_followers")
        deadline = date.fromisoformat(str(params["deadline"]))
        handle = params.get("x_handle") or "operator"

        goal = await create_goal(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            title=f"Reach {target:,} X followers (@{handle})",
            description=(
                "Grow @" + handle + " to " + f"{target:,}"
                + " followers via consistent posting + engagement."
            ),
            metric_key="follower_count",
            target_value=target,
            baseline_value=baseline,
            deadline=deadline,
            measurement_source={
                "provider": "x",
                "action": "x.get_profile_stats",
                "params": {"handle": handle},
            },
            measurement_cadence="daily",
            priority=2,
        )

        task_ids: list[str] = []
        seed_tasks = [
            (
                "Draft tomorrow's posts",
                "Generate 3-5 candidate posts for tomorrow based on what's "
                "performing this week. Operator approves before scheduling.",
                "content",
            ),
            (
                "Reply to mentions",
                "Triage the inbox: substantive replies to @mentions, dismiss spam.",
                "engagement",
            ),
            (
                "Engage with target accounts",
                "Find 10 high-quality conversations on follower-overlap accounts; "
                "draft a thoughtful reply for each.",
                "discovery",
            ),
        ]
        for title, body, kind in seed_tasks:
            t = await create_task(
                db, inp.entity_id,
                title=title,
                description=body,
                priority=2,
                task_type=kind,
                workspace_id=inp.workspace_id,
                creator_id=inp.user_id,
                details={"template_key": self.key, "template_role": kind},
            )
            task_ids.append(t.id)

        return TemplateResult(
            template_key=self.key,
            goal_id=goal.id,
            task_ids=task_ids,
            scheduled_job_ids=[],
            notes=[
                f"Created daily-cadence goal toward {target:,} followers by {deadline.isoformat()}.",
                "Seeded 3 starter tasks. The Strategist will queue more on the next weekly cycle.",
                f"Pair an X browser session via Settings → Integrations (provider=x, label='{handle}') so the worker can act.",
            ],
        )


register(TwitterGrowthTemplate())
