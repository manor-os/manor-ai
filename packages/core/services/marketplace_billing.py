"""Billing entitlement shared by paid Workspace Blueprint entry points."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.plans import is_cloud
from packages.core.services.plan_enforcement import get_entity_plan


class MarketplacePaidPlanRequiredError(PermissionError):
    """Raised when a paid Blueprint buyer has no active paid Manor plan."""

    def __init__(self, *, blueprint_id: str | None, plan_name: str) -> None:
        super().__init__(
            "An active paid Manor plan is required before purchasing, "
            "installing, or upgrading a paid Blueprint. Upgrade your plan "
            "to continue."
        )
        self.detail = {
            "message": str(self),
            "limit": None,
            "current": None,
            "plan": plan_name,
            "kind": "generic",
            "return_to": (
                f"/blueprints/{blueprint_id}?purchase=resume"
                if blueprint_id
                else None
            ),
        }


def blueprint_delivery_source(
    blueprint: Any,
    purchase: Any | None,
) -> tuple[dict, str | None]:
    """Return the content/version an entitled buyer should receive.

    Published rows are the current supported release, including updates made
    after the purchase.  The immutable purchase snapshot is a receipt and a
    fallback for a release the seller has archived or unpublished; it is not a
    parallel version channel that permanently pins ordinary installs.
    """
    if purchase is not None and blueprint.status != BlueprintStatus.PUBLISHED:
        return (
            purchase.payload_snapshot or {},
            purchase.blueprint_content_version,
        )
    return blueprint.payload or {}, blueprint.content_version


def blueprint_delivery_requires_paid_plan(
    blueprint: Any,
    purchase: Any | None,
) -> bool:
    """Return whether the content selected for delivery is paid content.

    A published release follows its current listing price. An archived or
    unpublished fallback follows the immutable purchase receipt instead, so a
    later seller repricing cannot turn a historical paid snapshot into a
    subscription-free delivery path.
    """
    if purchase is not None and blueprint.status != BlueprintStatus.PUBLISHED:
        return int(purchase.amount_cents or 0) > 0
    return int(blueprint.price_cents or 0) > 0


def plan_is_paid(plan: dict) -> bool:
    """Return whether a plan represents a paid or contracted entitlement."""
    price = plan.get("price_usd")
    if price is None:
        # Enterprise/contact-sales plans are contracted rather than free.
        return True
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


async def has_paid_marketplace_plan(
    db: AsyncSession,
    *,
    entity_id: str,
) -> bool:
    """Return whether Marketplace paid-content access is currently active."""
    if not is_cloud():
        return True
    return plan_is_paid(await get_entity_plan(db, entity_id))


async def require_paid_marketplace_plan(
    db: AsyncSession,
    *,
    entity_id: str,
    blueprint_id: str | None = None,
) -> None:
    """Fail closed for paid Blueprint operations without a paid Manor plan.

    Cloud raw imports use this gate too because their origin is not
    server-verifiable. OSS keeps its existing Blueprint behavior and never
    points users at a subscription flow that does not exist.
    """
    if not is_cloud():
        return
    plan = await get_entity_plan(db, entity_id)
    if plan_is_paid(plan):
        return
    raise MarketplacePaidPlanRequiredError(
        blueprint_id=blueprint_id,
        plan_name=str(plan.get("name") or "Free"),
    )
