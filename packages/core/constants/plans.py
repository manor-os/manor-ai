"""Subscription plan lookup.


OSS deployments return ``OSS_PLAN`` so self-hosted runtime code can keep
calling plan helpers without exposing Manor Cloud subscription economics.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ``AI_MARGIN`` lives in ``billing_service`` (single source of truth,
# env-overridable). Import it from there if you need the value — the
# duplicate constant that used to live here was silent-drift bait.


# ── Fallback (used when DB is empty / unreachable) ────────────────────


if "DEFAULT_PLAN_ID" not in globals():
    DEFAULT_PLAN_ID = "oss"
if "_FALLBACK_PLANS" not in globals():
    _FALLBACK_PLANS: dict[str, dict[str, Any]] = {}


# ── Live cache — mutated in-place so existing `from … import PLANS` works ──

PLANS: dict[str, dict[str, Any]] = dict(_FALLBACK_PLANS)
"""Active plans keyed by plan_id. Mutated in-place by
``load_plans_into_cache`` so any module that imported this symbol holds
a live reference. **Do not reassign — call the loader instead.**"""

_cache_loaded_from_db: bool = False


if "_PLAN_ALIASES" not in globals():
    _PLAN_ALIASES: dict[str, str] = {}


def canonical_plan_id(plan_id: str | None) -> str | None:
    """Normalize legacy short plan IDs to DB-backed subscription plan IDs."""
    if not plan_id:
        return plan_id
    if plan_id in PLANS:
        return plan_id
    alias = _PLAN_ALIASES.get(plan_id)
    if alias in PLANS:
        return alias
    return plan_id


# ── OSS / dev mode ────────────────────────────────────────────────────

OSS_PLAN = {
    "name": "OSS",
    "price_usd": 0,
    "workspaces": None,
    "users": None,
    "storage_mb": None,
    "ai_budget_usd": None,
    "ai_overage": True,
    "features": {},
    "support": "self",
}


def is_cloud() -> bool:
    """DEPLOYMENT_MODE=cloud → cloud (plan gates active, billing active)."""
    return os.getenv("DEPLOYMENT_MODE", "").lower() == "cloud"


def is_dev() -> bool:
    """MANOR_ENV=dev → cloud-in-dev (plan gates active, Stripe bypassed)."""
    return os.getenv("MANOR_ENV", "").lower() in ("dev", "development", "local")


# ── Sync API (consumed everywhere — must stay sync) ───────────────────

def get_plan(plan_id: str | None) -> dict[str, Any]:
    """Get plan config by ID. Returns OSS plan for self-hosted; falls
    back to the 'free' plan when the id is unknown."""
    if not is_cloud():
        return OSS_PLAN
    plan_id = canonical_plan_id(plan_id)
    if not plan_id:
        return PLANS.get(DEFAULT_PLAN_ID, OSS_PLAN)
    return PLANS.get(plan_id, PLANS.get(DEFAULT_PLAN_ID, OSS_PLAN))


def get_plan_limit(plan_id: str | None, key: str) -> int | float | None:
    """Get a specific limit from a plan. None = unlimited."""
    return get_plan(plan_id).get(key)


# ── Async loader (called at startup + after admin mutations) ──────────

async def load_plans_into_cache(db) -> int:
    """Refresh plan metadata when the deployment provides a plan catalog."""
    global _cache_loaded_from_db
    if not is_cloud():
        return 0
    return 0


