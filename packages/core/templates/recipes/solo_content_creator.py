"""One-person company — Solo Content Creator / Knowledge monetization.

Top-3 solopreneur business type #1 from the research: content & knowledge
monetization (creator economy, courses, newsletters, coaching). The binding
constraint per the research is *distribution* (audience-building) before
monetization, so this template scaffolds an audience-growth goal plus the
three workflow surfaces — produce, distribute, monetize — and bundles the
integrations + skills that let an agent actually run them.

MCP + skills are surfaced two ways: machine-readable on the seeded
"Connect your tools" task ``details`` (so the UI/agent can act), and as
human notes for the operator.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.templates.registry import (
    TemplateInput,
    TemplateResult,
    register,
)

# Real MCP server_keys from packages.core.services.mcp_seed._MCP_CATALOG.
RECOMMENDED_MCP = ["youtube", "tiktok", "facebook", "linkedin", "gmail", "tavily"]
# Real skill slugs (marketplace + built-in doc skills) — see test integrity check.
RECOMMENDED_SKILLS = [
    "brand_voice", "blog_writer",
    "email_marketing", "lead_magnets", "reef_copywriting", "pricing",
]


@dataclass
class SoloContentCreatorTemplate:
    key: str = "solo_content_creator"
    title: str = "Solo Content Creator"
    summary: str = (
        "Audience-growth goal + produce/distribute/monetize tasks for a "
        "one-person content or knowledge business. Bundles the video, social "
        "and email integrations an agent needs."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "required": ["niche", "target_subscribers"],
                "properties": {
                    "niche": {"type": "string"},
                    "primary_platform": {"type": "string"},  # youtube/tiktok/newsletter…
                    "target_subscribers": {"type": "integer"},
                    "current_subscribers": {"type": "integer"},
                    "deadline": {"type": "string"},  # ISO date
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from datetime import date, timedelta

        from packages.core.goals import create_goal
        from packages.core.services.task_service import create_task
        from packages.core.templates.recipes._solo_provision import provision_solo_agent

        p = inp.params
        niche = p["niche"]
        target = int(p["target_subscribers"])
        baseline = p.get("current_subscribers")
        platform = p.get("primary_platform") or "youtube"
        deadline = (
            date.fromisoformat(str(p["deadline"]))
            if p.get("deadline")
            else date.today() + timedelta(days=90)
        )

        goal = await create_goal(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            title=f"Grow {niche} audience to {target:,} on {platform}",
            description=(
                f"Build a one-person {niche} content business. Distribution first: "
                f"grow to {target:,} subscribers/followers, then monetize."
            ),
            metric_key="subscriber_count",
            target_value=target,
            baseline_value=baseline,
            deadline=deadline,
            measurement_cadence="daily",
            priority=2,
        )

        task_ids: list[str] = []
        seed = [
            ("Connect your tools",
             "Connect the recommended integrations so the agent can publish and "
             "research on your behalf.", "setup",
             {"recommended_mcp": RECOMMENDED_MCP, "recommended_skills": RECOMMENDED_SKILLS}),
            ("Define content pillars + weekly calendar",
             f"Pick 3-4 {niche} content pillars and a repeatable weekly publishing "
             "calendar. Create once, distribute everywhere.", "content", None),
            ("Ship a lead magnet / free offer",
             "Create a free resource that converts viewers into email subscribers "
             "(the owned audience).", "growth", None),
            ("Stand up a monetization offer",
             "Define the first paid offer (newsletter / course / coaching) and the "
             "path from free content to it.", "monetization", None),
        ]
        for title, body, kind, details in seed:
            d = {"template_key": self.key, "template_role": kind}
            if details:
                d.update(details)
            t = await create_task(
                db, inp.entity_id,
                title=title, description=body, priority=2, task_type=kind,
                workspace_id=inp.workspace_id, creator_id=inp.user_id, details=d,
            )
            task_ids.append(t.id)

        prov = await provision_solo_agent(
            db, entity_id=inp.entity_id, workspace_id=inp.workspace_id,
            agent_name=f"{niche} Content Agent",
            system_prompt=(
                f"You run a one-person {niche} content business on {platform}. "
                "Distribution first: produce, distribute and monetize. Use the "
                "bound social/video/email tools to publish and the content skills "
                "to create. Operator approves before anything goes public."
            ),
            service_key="content",
            mcp=RECOMMENDED_MCP, skills=RECOMMENDED_SKILLS,
        )

        return TemplateResult(
            template_key=self.key,
            goal_id=goal.id,
            task_ids=task_ids,
            scheduled_job_ids=[],
            notes=[
                f"Daily-cadence goal toward {target:,} subscribers by {deadline.isoformat()}.",
                "Seeded 4 starter tasks: connect tools, content pillars, lead magnet, monetization.",
                f"Provisioned agent {prov['agent_id']} and hired it into the workspace — "
                f"bound {len(prov['bound_skills'])} skills + {len(prov['bound_mcp'])} MCP servers.",
                "Bound integrations: " + ", ".join(prov["bound_mcp"] or RECOMMENDED_MCP) + ".",
                "Bound skills: " + ", ".join(prov["bound_skills"] or RECOMMENDED_SKILLS) + ".",
            ],
        )


register(SoloContentCreatorTemplate())
