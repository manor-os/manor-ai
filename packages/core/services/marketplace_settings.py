"""Marketplace platform settings — admin-managed knobs in PlatformSetting.

Single KV document under key ``marketplace``. First (and v1-only) knob is
``platform_fee_percent`` — the cut taken on paid blueprint sales via
Stripe ``application_fee_amount``. Default 0: no fee.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.platform_setting import PlatformSetting

MARKETPLACE_SETTINGS_KEY = "marketplace"


async def _get_row(db: AsyncSession) -> PlatformSetting | None:
    return (await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == MARKETPLACE_SETTINGS_KEY)
    )).scalar_one_or_none()


async def get_marketplace_fee_percent(db: AsyncSession) -> int:
    """Return the current platform fee percent (default 0).

    Reads defensively: an out-of-range stored value is silently clamped
    into 0-100, and non-numeric garbage falls back to 0 — checkout must
    never fail because of a corrupt settings row.
    """
    row = await _get_row(db)
    if row is None:
        return 0
    value = (row.value or {}).get("platform_fee_percent", 0)
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


async def set_marketplace_fee_percent(
    db: AsyncSession, percent: int, *, updated_by: str,
) -> int:
    if not isinstance(percent, int) or isinstance(percent, bool) or percent < 0 or percent > 100:
        raise ValueError("platform_fee_percent must be an integer 0-100")
    row = await _get_row(db)
    if row is None:
        row = PlatformSetting(key=MARKETPLACE_SETTINGS_KEY, value={})
        db.add(row)
    row.value = {**(row.value or {}), "platform_fee_percent": percent}
    row.updated_by = updated_by
    await db.flush()
    return percent


def platform_fee_cents(amount_cents: int, fee_percent: int) -> int:
    """Floor division — the platform never rounds its cut up."""
    return (amount_cents * fee_percent) // 100
