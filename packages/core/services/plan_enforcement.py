"""
Plan enforcement — metering and usage summaries.

For plan limit checks, use plan_gate.check() instead.

This module keeps:
  - get_entity_plan() — shared helper for loading an entity's plan
  - record_ai_cost()  — post-usage metering (records actual spend)
  - get_usage_summary() — billing UI data
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import canonical_plan_id, get_plan, is_cloud, is_dev
from packages.core.services.billing_service import AI_MARGIN, CREDITS_PER_USD

logger = logging.getLogger(__name__)


def _plan_credit_amount(plan: dict[str, Any]) -> int:
    credits = int(plan.get("credit_amount") or 0)
    if credits > 0:
        return credits
    budget_usd = plan.get("ai_budget_usd") or 0
    return int(float(budget_usd) * CREDITS_PER_USD) if budget_usd else 0


async def credit_usage_totals(db: AsyncSession, entity_id: str) -> dict[str, int | float]:
    """Return entity-level credit usage without depending on Cloud grant ledgers."""
    from packages.core.models.billing import CreditReservation, CreditUsageLog

    usage_row = (await db.execute(
        select(
            func.coalesce(func.sum(CreditUsageLog.total_credit), 0).label("credits_used"),
            func.coalesce(func.sum(CreditUsageLog.total_tokens), 0).label("tokens_used"),
            func.coalesce(func.sum(CreditUsageLog.cost_usd), 0).label("cost_usd"),
        ).where(CreditUsageLog.entity_id == entity_id)
    )).one()
    reserved = (await db.execute(
        select(func.coalesce(func.sum(CreditReservation.amount_credits), 0)).where(
            CreditReservation.entity_id == entity_id,
            CreditReservation.status == "active",
        )
    )).scalar_one() or 0
    return {
        "credits_used": int(usage_row.credits_used or 0),
        "tokens_used": int(usage_row.tokens_used or 0),
        "cost_usd": round(float(usage_row.cost_usd or 0), 6),
        "credits_reserved": int(reserved or 0),
    }


async def get_entity_plan(db: AsyncSession, entity_id: str) -> dict[str, Any]:
    """Load the entity's current plan."""
    if not is_cloud():
        from packages.core.constants.plans import OSS_PLAN
        return OSS_PLAN

    from packages.core.models.user import Entity
    result = await db.execute(select(Entity).where(Entity.id == entity_id))
    entity = result.scalar_one_or_none()
    if not entity:
        return get_plan("plan_free")

    plan_id = entity.plan_id or (entity.settings or {}).get("plan", "plan_free")
    return get_plan(plan_id)




async def record_ai_cost(
    db: AsyncSession, entity_id: str,
    provider_cost_usd: float, model: str | None = None,
) -> dict[str, Any]:
    """Record AI usage cost and check budget.

    Args:
        provider_cost_usd: actual cost to the LLM provider
        model: model ID for logging

    Returns:
        {"allowed": True, "billed": float} or {"allowed": False, "message": "..."}
    """
    billed = provider_cost_usd * (1 + AI_MARGIN)

    from packages.core.models.user import Entity
    result = await db.execute(select(Entity).where(Entity.id == entity_id))
    entity = result.scalar_one_or_none()
    if not entity:
        return {"allowed": True, "billed": billed}

    settings = dict(entity.settings or {})
    current_usage = float(settings.get("ai_usage_usd", 0.0))
    new_usage = current_usage + billed

    plan_id = entity.plan_id or settings.get("plan", "plan_free")
    plan = get_plan(plan_id)
    budget = plan.get("ai_budget_usd")
    # Dev mode: use Free plan budget as default so usage tracking works
    if budget is None and is_dev():
        budget = 2.0

    # Check if over budget
    if budget is not None and new_usage > budget and not plan.get("ai_overage"):
        # Dev mode: auto-reset instead of blocking
        if is_dev():
            logger.info("Dev mode: auto-resetting AI usage for entity %s", entity_id)
            settings["ai_usage_usd"] = 0.0
            settings["ai_provider_cost_usd"] = 0.0
            settings["used_credits"] = 0
            new_usage = billed
            settings["ai_usage_usd"] = round(new_usage, 4)
            entity.settings = settings
            await db.flush()
            return {"allowed": True, "billed": round(billed, 4), "total_usage": round(new_usage, 4), "budget": budget, "overage": False, "auto_reset": True}
        credits_limit = int(budget * CREDITS_PER_USD)
        return {
            "allowed": False,
            "message": f"You've used all {credits_limit:,} credits on the {plan.get('name', 'current')} plan. Purchase more credits or upgrade to continue.",
            "billed": billed,
        }

    # Record usage
    settings["ai_usage_usd"] = round(new_usage, 4)
    settings["ai_provider_cost_usd"] = round(
        float(settings.get("ai_provider_cost_usd", 0.0)) + provider_cost_usd, 4
    )
    entity.settings = settings
    await db.flush()

    return {
        "allowed": True,
        "billed": round(billed, 4),
        "total_usage": round(new_usage, 4),
        "budget": budget,
        "overage": budget is not None and new_usage > budget,
    }


async def get_usage_summary(db: AsyncSession, entity_id: str) -> dict[str, Any]:
    """Get current plan usage summary for the billing UI."""
    plan = await get_entity_plan(db, entity_id)

    from packages.core.models.user import Entity
    result = await db.execute(select(Entity).where(Entity.id == entity_id))
    entity = result.scalar_one_or_none()
    settings = dict(entity.settings) if entity and entity.settings else {}

    budget_usd = plan.get("ai_budget_usd")
    plan_credits = _plan_credit_amount(plan)
    # Dev mode: default to Free plan budget if no budget set
    if budget_usd is None and is_dev():
        budget_usd = 2.0  # Free plan default for dev
    budget_usd = budget_usd or 0
    usage_usd = float(settings.get("ai_usage_usd", 0.0))

    # Dev mode: auto-reset when budget exhausted (bypass real billing)
    if is_dev() and budget_usd and usage_usd >= budget_usd and entity:
        logger.info("Dev mode: auto-resetting AI usage for entity %s", entity_id)
        settings["ai_usage_usd"] = 0.0
        settings["ai_provider_cost_usd"] = 0.0
        settings["used_credits"] = 0
        entity.settings = settings
        await db.flush()
        usage_usd = 0.0

    usage_totals = await credit_usage_totals(db, entity_id)
    budget_credits: int | None = None
    usage_credits = 0
    credits_reserved = 0

    # Credits: Cloud checks credit_grants first (admin grants, invite bonuses),
    # then falls back to plan/settings totals. OSS keeps only the fallback path.

    if budget_credits is None:
        settings_total_credits = int(settings.get("total_credits", 0) or 0)
        budget_credits = settings_total_credits or (plan_credits if plan_credits else None)
        usage_credits = int(usage_totals["credits_used"]) + int(usage_totals["credits_reserved"])
        credits_reserved = int(usage_totals["credits_reserved"])

    return {
        "plan": plan.get("name", "Free"),
        "plan_id": canonical_plan_id(entity.plan_id or settings.get("plan", "plan_free")) if entity else "plan_free",
        "billing_mode": "cloud" if is_cloud() else "dev" if is_dev() else "oss",
        "ai_budget_usd": budget_usd,
        "ai_usage_usd": round(usage_usd, 2),
        "ai_provider_cost_usd": round(float(settings.get("ai_provider_cost_usd", 0.0)), 2),
        "ai_margin_pct": int(AI_MARGIN * 100),
        "ai_overage_allowed": plan.get("ai_overage", False),
        "credits_total": budget_credits,
        "credits_used": usage_credits,
        "credits_reserved": credits_reserved,
        "credits_remaining": max(0, (budget_credits or 0) - usage_credits),
        "workspaces_limit": plan.get("workspaces"),
        "users_limit": plan.get("users"),
        "storage_mb_limit": plan.get("storage_mb"),
        "billing_cycle_start": settings.get("billing_cycle_start"),
    }
