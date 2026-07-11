"""Quota enforcement service — check limits, record usage, generate reports."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.quota import EntityQuota


async def get_or_create_quota(db: AsyncSession, entity_id: str) -> EntityQuota:
    """Get entity quota, creating default (free plan) if not exists."""
    result = await db.execute(
        select(EntityQuota).where(EntityQuota.entity_id == entity_id)
    )
    quota = result.scalars().first()
    if quota is None:
        today = date.today()
        quota = EntityQuota(
            id=generate_ulid(),
            entity_id=entity_id,
            plan_name="free",
            current_period_start=today.replace(day=1),
            last_daily_reset=today,
        )
        db.add(quota)
        await db.flush()
    return quota


async def _ensure_resets(db: AsyncSession, quota: EntityQuota) -> None:
    """Reset daily / monthly counters if the period has rolled over."""
    today = date.today()
    changed = False

    if quota.last_daily_reset is None or quota.last_daily_reset != today:
        quota.api_calls_today = 0
        quota.last_daily_reset = today
        changed = True

    if quota.current_period_start is None or quota.current_period_start.month != today.month or quota.current_period_start.year != today.year:
        quota.tokens_used_this_month = 0
        quota.current_period_start = today.replace(day=1)
        changed = True

    if changed:
        await db.flush()


async def check_quota(
    db: AsyncSession, entity_id: str, resource: str
) -> tuple[bool, str]:
    """Check if an entity is within quota for *resource*.

    resource: "users", "agents", "documents", "storage", "tokens", "api_calls"
    Returns (allowed, reason).  reason is empty string when allowed.
    """
    quota = await get_or_create_quota(db, entity_id)
    await _ensure_resets(db, quota)

    if resource == "tokens":
        if quota.max_tokens_monthly > 0 and quota.tokens_used_this_month >= quota.max_tokens_monthly:
            return False, f"Monthly token limit reached ({quota.max_tokens_monthly:,})"

    elif resource == "api_calls":
        if quota.max_api_calls_daily > 0 and quota.api_calls_today >= quota.max_api_calls_daily:
            return False, f"Daily API call limit reached ({quota.max_api_calls_daily:,})"

    elif resource == "users":
        if quota.max_users > 0:
            from packages.core.models.user import User
            count = (await db.execute(
                select(func.count()).select_from(User).where(User.entity_id == entity_id)
            )).scalar() or 0
            if count >= quota.max_users:
                return False, f"User limit reached ({quota.max_users})"

    elif resource == "agents":
        if quota.max_agents > 0:
            from packages.core.models.workspace import Agent
            count = (await db.execute(
                select(func.count()).select_from(Agent).where(Agent.entity_id == entity_id)
            )).scalar() or 0
            if count >= quota.max_agents:
                return False, f"Agent limit reached ({quota.max_agents})"

    elif resource == "documents":
        if quota.max_documents > 0:
            from packages.core.models.document import Document
            count = (await db.execute(
                select(func.count()).select_from(Document).where(Document.entity_id == entity_id)
            )).scalar() or 0
            if count >= quota.max_documents:
                return False, f"Document limit reached ({quota.max_documents})"

    elif resource == "storage":
        if quota.max_storage_bytes > 0 and quota.storage_used_bytes >= quota.max_storage_bytes:
            return False, f"Storage limit reached ({quota.max_storage_bytes:,} bytes)"

    return True, ""


# NOTE: record_token_usage / record_api_call removed — they were the
# shadow-write helpers backing EntityQuota counters that nothing
# enforced against. Real LLM accounting lives in TokenUsageLog +
# CreditUsageLog (see ``services/usage_service.py``). The existing
# ``EntityQuota.tokens_used_this_month`` / ``api_calls_today`` columns
# stop incrementing here on; ``get_usage_report`` now derives the plan
# label from ``Entity.plan_id`` and leaves the stale counter columns
# alone until the table is dropped.


async def get_usage_report(db: AsyncSession, entity_id: str) -> dict:
    """Get current usage vs limits for the entity.

    DEPRECATED — ``EntityQuota`` is shadow-bookkeeping that nothing
    enforces against. Real enforcement lives in ``plan_gate``. This
    endpoint is kept for the Subscription UI's plan-label read; counters
    will go stale and should be migrated to ``billing_service`` /
    ``plan_enforcement.get_usage_summary`` before the table is dropped.
    """
    quota = await get_or_create_quota(db, entity_id)
    await _ensure_resets(db, quota)

    # Plan name: prefer the live FK on Entity, fall back to the stale
    # quota.plan_name only if the entity has no plan_id set.
    plan_name = quota.plan_name
    try:
        from packages.core.models.user import Entity
        from packages.core.models.billing import SubscriptionPlan
        ent = (await db.execute(
            select(Entity).where(Entity.id == entity_id)
        )).scalar_one_or_none()
        if ent and ent.plan_id:
            plan = (await db.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.id == ent.plan_id)
            )).scalar_one_or_none()
            if plan:
                plan_name = plan.name
    except Exception:
        pass  # fall back to whatever quota row holds

    def _pct(used: int, limit: int) -> float | None:
        if limit <= 0:
            return None
        return round(used / limit * 100, 2)

    return {
        "plan": plan_name,
        "tokens": {
            "used": quota.tokens_used_this_month,
            "limit": quota.max_tokens_monthly,
            "pct": _pct(quota.tokens_used_this_month, quota.max_tokens_monthly),
        },
        "api_calls": {
            "used": quota.api_calls_today,
            "limit": quota.max_api_calls_daily,
            "pct": _pct(quota.api_calls_today, quota.max_api_calls_daily),
        },
        "storage": {
            "used": quota.storage_used_bytes,
            "limit": quota.max_storage_bytes,
            "pct": _pct(quota.storage_used_bytes, quota.max_storage_bytes),
        },
        "users": {"limit": quota.max_users},
        "agents": {"limit": quota.max_agents},
        "documents": {"limit": quota.max_documents},
    }


async def update_quota(db: AsyncSession, entity_id: str, **kwargs) -> EntityQuota:
    """Update quota limits (admin / billing).

    Accepted kwargs: plan_name, max_users, max_agents, max_documents,
    max_storage_bytes, max_tokens_monthly, max_api_calls_daily.
    """
    quota = await get_or_create_quota(db, entity_id)
    allowed = {
        "plan_name", "max_users", "max_agents", "max_documents",
        "max_storage_bytes", "max_tokens_monthly", "max_api_calls_daily",
    }
    for key, value in kwargs.items():
        if key in allowed and value is not None:
            setattr(quota, key, value)
    await db.flush()
    return quota
