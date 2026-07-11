"""
PlanGate — centralized plan permission enforcement.

Usage (FastAPI dependency):
    from apps.api.deps import require_plan

    @router.post("/workspaces")
    async def create_workspace(
        _gate=Depends(require_plan("workspaces")),
        ...
    ):

Usage (direct call — services, Celery tasks):
    from packages.core.services.plan_gate import check

    result = await check(db, entity_id, "ai_budget_usd")
    if not result.allowed:
        logger.warning("Budget exhausted: %s", result.message)

OSS mode: check() always returns allowed=True. Single bypass point.

Hot-path caching: positive (allowed=True) gate results are cached
in-process for ``_GATE_CACHE_TTL`` seconds keyed on (entity_id,
resource). Negative results are NEVER cached — auto-recharge / manual
top-ups should unblock the next call immediately. This is invisible to
callers: ``check()`` returns the same shape, just sometimes faster.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import is_cloud

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Counter = Callable[[AsyncSession, str], Awaitable[int | float]]

_counters: dict[str, Counter] = {}


@dataclass
class GateResult:
    allowed: bool
    message: str = ""
    limit: int | float | None = None
    current: int | float | None = None
    plan: str = ""
    overage: bool = False


# ---------------------------------------------------------------------------
# Counter registry — add new resource types here
# ---------------------------------------------------------------------------

def counter(resource: str):
    """Decorator to register a resource counter."""
    def wrap(fn: Counter) -> Counter:
        _counters[resource] = fn
        return fn
    return wrap


@counter("workspaces")
async def _count_workspaces(db: AsyncSession, entity_id: str) -> int:
    from packages.core.models.workspace import Workspace
    q = select(func.count()).select_from(Workspace).where(
        Workspace.entity_id == entity_id,
        Workspace.status != "archived",
    )
    # Exclude legacy soft-deleted rows (deleted_at set but row still exists)
    if hasattr(Workspace, "deleted_at"):
        q = q.where(Workspace.deleted_at.is_(None))
    r = await db.execute(q)
    return r.scalar_one()


@counter("users")
async def _count_users(db: AsyncSession, entity_id: str) -> int:
    from packages.core.models.user import User, UserMembership
    r = await db.execute(
        select(func.count()).select_from(UserMembership)
        .join(User, User.id == UserMembership.user_id)
        .where(
            UserMembership.entity_id == entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )
    return r.scalar_one()


@counter("storage_mb")
async def _count_storage(db: AsyncSession, entity_id: str) -> float:
    from packages.core.models.document import Document
    r = await db.execute(
        select(func.coalesce(func.sum(Document.file_size), 0)).where(
            Document.entity_id == entity_id,
        )
    )
    return r.scalar_one() / (1024 * 1024)


@counter("ai_budget_usd")
async def _count_ai_usage(db: AsyncSession, entity_id: str) -> float:
    """Return AI budget usage as a USD figure.

    If the entity has credit_grants, compare grants vs consumption and
    convert back to USD. Otherwise fall back to entity.settings for
    legacy deployments.
    """

    # Fallback: legacy entity.settings
    from packages.core.models.user import Entity
    r = await db.execute(select(Entity).where(Entity.id == entity_id))
    entity = r.scalar_one_or_none()
    if not entity:
        return 0.0
    return float((entity.settings or {}).get("ai_usage_usd", 0.0))


# ---------------------------------------------------------------------------
# Core gate
# ---------------------------------------------------------------------------

_LABELS = {
    "workspaces": "workspaces",
    "users": "team members",
    "storage_mb": "MB of knowledge base storage",
    "ai_budget_usd": "in AI budget",
}


# ── Hot-path cache for positive gate results ────────────────────────
#
# ``_preflight_credit_check`` runs on every LLM call. Positive gate
# caching is useful for slow-moving resources such as workspace counts.
# Do not cache ``ai_budget_usd`` positives: long or concurrent agent loops
# need fresh credit balances so exhausted accounts cannot keep spending
# during the cache TTL.
#
# Negatives are NEVER cached: a top-up or auto-recharge should unblock
# the very next call.

_GATE_CACHE_TTL = float(os.getenv("PLAN_GATE_CACHE_TTL_SECONDS", "10"))
_gate_cache: dict[tuple[str, str], tuple[GateResult, float]] = {}


def _cache_key(entity_id: str, resource: str) -> tuple[str, str]:
    return (entity_id, resource)


def invalidate_gate_cache(entity_id: Optional[str] = None) -> None:
    """Drop cached gate results.

    Pass ``entity_id`` to invalidate one tenant (e.g. after admin grants
    credits or changes plan). Call with no args to flush everything —
    rarely needed; mostly useful in tests."""
    if entity_id is None:
        _gate_cache.clear()
        return
    for key in list(_gate_cache.keys()):
        if key[0] == entity_id:
            _gate_cache.pop(key, None)


async def check(
    db: AsyncSession, entity_id: str, resource: str,
) -> GateResult:
    """Check if entity can use/create a resource under their plan.

    Works anywhere: FastAPI deps, service layer, Celery tasks.
    OSS mode always returns allowed=True.
    """
    if not is_cloud():
        return GateResult(allowed=True)

    cacheable = resource != "ai_budget_usd"

    # Cache lookup — positive results only, TTL gated.
    key = _cache_key(entity_id, resource)
    cached = _gate_cache.get(key) if cacheable else None
    if cached is not None:
        result, expires_at = cached
        if time.monotonic() < expires_at:
            return result
        # Expired — drop and fall through to the live check.
        _gate_cache.pop(key, None)

    from packages.core.services.plan_enforcement import get_entity_plan

    plan = await get_entity_plan(db, entity_id)
    plan_name = plan.get("name", "Free")
    limit = plan.get(resource)

    # For AI budget: if entity has credit_grants, use grant total as limit
    if resource == "ai_budget_usd":
        pass

    # None = unlimited (Enterprise, or resource not capped)
    if limit is None:
        result = GateResult(allowed=True)
        if cacheable:
            _gate_cache[key] = (result, time.monotonic() + _GATE_CACHE_TTL)
        return result

    count_fn = _counters.get(resource)
    if not count_fn:
        raise ValueError(f"No counter registered for resource: {resource}")

    current = await count_fn(db, entity_id)

    if current >= limit:
        label = _LABELS.get(resource, resource)
        if resource == "ai_budget_usd":
            from packages.core.services.billing_service import CREDITS_PER_USD
            credits_limit = int(limit * CREDITS_PER_USD)
            credits_used = int(current * CREDITS_PER_USD)
            return GateResult(
                allowed=False,
                message=(
                    f"You've used all {credits_limit:,} credits on the {plan_name} plan. "
                    f"Purchase more credits or upgrade your plan to continue."
                ),
                limit=credits_limit, current=credits_used, plan=plan_name,
            )
        return GateResult(
            allowed=False,
            message=f"You've reached the {plan_name} plan limit of {limit} {label}. Upgrade for more.",
            limit=limit, current=current, plan=plan_name,
        )

    result = GateResult(allowed=True, limit=limit, current=current, plan=plan_name)
    if cacheable:
        _gate_cache[key] = (result, time.monotonic() + _GATE_CACHE_TTL)
    return result
