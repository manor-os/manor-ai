"""One-person company — E-commerce / digital products.

Top-3 solopreneur business type #3: e-commerce & digital products (Shopify/
WooCommerce/Square storefronts, TikTok Shop, Amazon, POD, digital goods). The
research's levers are listing optimization, fulfillment/ops automation, and
margin/cash-flow tracking. This template scaffolds a monthly-revenue goal plus
catalog / ops / marketing / finance tasks and bundles the storefront +
marketplace + payments integrations (the ones added in the e-commerce MCP PR).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.templates.registry import (
    TemplateInput,
    TemplateResult,
    register,
)

RECOMMENDED_MCP = ["shopify", "woocommerce", "square", "tiktok_shop", "amazon", "stripe"]
# Real skill slugs (marketplace + built-in doc skills) — see test integrity check.
RECOMMENDED_SKILLS = [
    "reef_copywriting", "meta_tags_optimizer", "email_marketing",
    "pricing", "logo_generator", "xlsx",
    "order_triage", "support_reply",
]


@dataclass
class SoloEcommerceTemplate:
    key: str = "solo_ecommerce"
    title: str = "Solo E-commerce Store"
    summary: str = (
        "Run a one-person store: a monthly-revenue goal plus listing, "
        "fulfillment-automation, marketing and margin-tracking tasks. Bundles "
        "the storefront, marketplace and payments integrations."
    )
    params_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params_schema is None:
            self.params_schema = {
                "type": "object",
                "required": ["platform", "monthly_revenue_target"],
                "properties": {
                    "platform": {"type": "string"},   # shopify/woocommerce/square/…
                    # accept 20000 or 20000.50 — JSON ints must not be rejected
                    "monthly_revenue_target": {"type": ["number", "integer"]},
                    "currency": {"type": "string"},
                    "deadline": {"type": "string"},  # ISO date
                },
            }

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        from datetime import date, timedelta

        from packages.core.goals import create_goal
        from packages.core.services.task_service import create_task
        from packages.core.templates.recipes._solo_provision import provision_solo_agent

        p = inp.params
        platform = p["platform"]
        target = float(p["monthly_revenue_target"])
        currency = p.get("currency", "USD")
        deadline = (
            date.fromisoformat(str(p["deadline"]))
            if p.get("deadline")
            else date.today() + timedelta(days=90)
        )

        goal = await create_goal(
            db,
            entity_id=inp.entity_id,
            workspace_id=inp.workspace_id,
            title=f"Reach {currency} {target:,.0f} / month on {platform}",
            description=(
                f"Grow a one-person {platform} store to {currency} {target:,.0f} "
                "monthly revenue via listing optimization, fulfillment automation "
                "and margin tracking."
            ),
            metric_key="monthly_revenue",
            target_value=target,
            deadline=deadline,
            measurement_cadence="weekly",
            priority=2,
        )

        task_ids: list[str] = []
        seed = [
            ("Connect your store + payments",
             "Connect the recommended integrations so the agent can read orders/"
             "products and update listings + inventory.", "setup",
             {"recommended_mcp": RECOMMENDED_MCP, "recommended_skills": RECOMMENDED_SKILLS}),
            ("Optimize product listings + SEO",
             "Tighten titles, descriptions, images and tags on the top SKUs for "
             "conversion and search.", "catalog", None),
            ("Automate fulfillment + order status",
             "Wire up order triage, status updates and shipping notifications so "
             "orders run hands-off.", "ops", None),
            ("Set up post-purchase + abandoned-cart flows",
             "Recover carts and drive repeat purchases with automated lifecycle "
             "messaging.", "marketing", None),
            ("Reconcile payments + track margins",
             "Connect Stripe; track revenue vs COGS/fees so you watch margin, not "
             "just GMV.", "finance", None),
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
            agent_name=f"{platform} Store Agent",
            system_prompt=(
                f"You run a one-person {platform} store toward {currency} "
                f"{target:,.0f}/month. Optimize listings, automate fulfillment, "
                "recover carts, watch margins. Use the bound store/marketplace/"
                "payments tools and skills. Never auto-refund/cancel — propose."
            ),
            service_key="ecommerce",
            mcp=RECOMMENDED_MCP, skills=RECOMMENDED_SKILLS,
        )

        return TemplateResult(
            template_key=self.key,
            goal_id=goal.id,
            task_ids=task_ids,
            scheduled_job_ids=[],
            notes=[
                f"Weekly-cadence goal toward {currency} {target:,.0f}/month by {deadline.isoformat()}.",
                "Seeded 5 starter tasks: connect store, listings, fulfillment, lifecycle, margins.",
                f"Provisioned agent {prov['agent_id']} and hired it into the workspace — "
                f"bound {len(prov['bound_skills'])} skills + {len(prov['bound_mcp'])} MCP servers.",
                "Bound integrations: " + ", ".join(prov["bound_mcp"] or RECOMMENDED_MCP) + ".",
                "Bound skills: " + ", ".join(prov["bound_skills"] or RECOMMENDED_SKILLS) + ".",
            ],
        )


register(SoloEcommerceTemplate())
