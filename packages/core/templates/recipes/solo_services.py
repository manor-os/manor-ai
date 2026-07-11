"""One-person company — Productized professional services (freelancer/consultant).

Top-3 solopreneur business type #2: the most common nonemployer category
(Professional, Scientific & Technical Services). The research's lever here is
turning hourly freelancing into a productized offer with steady outbound, and
protecting cash flow (thin buffers are the #1 killer). This template scaffolds
a "new clients / month" goal plus offer / portfolio / outbound / finance tasks
and bundles the outreach + invoicing integrations.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.templates.registry import (
    TemplateInput,
    TemplateResult,
    register,
)

RECOMMENDED_MCP = ["linkedin", "gmail", "google_calendar", "github", "stripe", "quickbooks"]
# Real skill slugs (marketplace + built-in doc skills) — see test integrity check.
RECOMMENDED_SKILLS = [
    "go_to_market", "first_customers", "cold_outreach",
    "pricing", "marketing_strategy_pmm", "pptx",
    "invoicing_contracts", "support_reply",
]


@dataclass
class SoloServicesTemplate:
    key: str = "solo_services"
    title: str = "Solo Professional Services"
    summary: str = (
        "Productize a freelance/consulting service: a new-clients-per-month "
        "goal plus offer, portfolio, weekly outbound and invoicing tasks. "
        "Bundles outreach + billing integrations."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "required": ["service", "target_clients_per_month"],
                "properties": {
                    "service": {"type": "string"},
                    "target_clients_per_month": {"type": "integer"},
                    "portfolio_url": {"type": "string"},
                    "deadline": {"type": "string"},  # ISO date
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from datetime import date, timedelta

        from packages.core.goals import create_goal
        from packages.core.services.task_service import create_task
        from packages.core.templates.recipes._solo_provision import provision_solo_agent

        p = inp.params
        service = p["service"]
        target = int(p["target_clients_per_month"])
        portfolio = p.get("portfolio_url")
        deadline = (
            date.fromisoformat(str(p["deadline"]))
            if p.get("deadline")
            else date.today() + timedelta(days=90)
        )

        goal = await create_goal(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            title=f"Win {target} new {service} clients / month",
            description=(
                f"Productize {service}: standardized scope + price, steady outbound, "
                "and protected cash flow (keep >30 days of runway)."
                + (f" Portfolio: {portfolio}" if portfolio else "")
            ),
            metric_key="new_clients",
            target_value=target,
            deadline=deadline,
            measurement_cadence="weekly",
            priority=2,
        )

        task_ids: list[str] = []
        seed = [
            ("Connect your tools",
             "Connect the recommended integrations so the agent can do outreach, "
             "scheduling and invoicing.", "setup",
             {"recommended_mcp": RECOMMENDED_MCP, "recommended_skills": RECOMMENDED_SKILLS}),
            ("Productize the offer",
             f"Turn {service} into a fixed-scope, fixed-price package (what's in, "
             "what's out, price, turnaround).", "offer", None),
            ("Build / refresh portfolio + case studies",
             "Publish 2-3 proof points with outcomes and a testimonial.", "portfolio", None),
            ("Run weekly outbound to 10 prospects",
             "Identify 10 fitting prospects and draft a tailored first message each.", "sales", None),
            ("Set up invoicing + contracts",
             "Connect Stripe/QuickBooks; standardize a contract + payment terms to "
             "protect cash flow.", "finance", None),
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
            agent_name=f"{service} Services Agent",
            system_prompt=(
                f"You run a productized {service} business of one. Win {target} "
                "new clients/month: standardized offer, steady outbound, fast "
                "invoicing, protected cash flow. Use the bound outreach/billing "
                "tools and skills. Operator approves outbound + invoices."
            ),
            service_key="services",
            mcp=RECOMMENDED_MCP, skills=RECOMMENDED_SKILLS,
        )

        return TemplateResult(
            template_key=self.key,
            goal_id=goal.id,
            task_ids=task_ids,
            scheduled_job_ids=[],
            notes=[
                f"Weekly-cadence goal toward {target} new {service} clients/month by {deadline.isoformat()}.",
                "Seeded 5 starter tasks: connect tools, productize, portfolio, outbound, invoicing.",
                f"Provisioned agent {prov['agent_id']} and hired it into the workspace — "
                f"bound {len(prov['bound_skills'])} skills + {len(prov['bound_mcp'])} MCP servers.",
                "Bound integrations: " + ", ".join(prov["bound_mcp"] or RECOMMENDED_MCP) + ".",
                "Bound skills: " + ", ".join(prov["bound_skills"] or RECOMMENDED_SKILLS) + ".",
            ],
        )


register(SoloServicesTemplate())
