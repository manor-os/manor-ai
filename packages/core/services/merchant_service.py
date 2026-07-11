"""Stripe Connect merchant accounts — create, link, and mirror webhook state."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.merchant import MerchantAccount


async def get_merchant_account(
    db: AsyncSession, entity_id: str,
) -> Optional[MerchantAccount]:
    return (await db.execute(
        select(MerchantAccount).where(MerchantAccount.entity_id == entity_id)
    )).scalar_one_or_none()


async def ensure_merchant_account(
    db: AsyncSession, *, entity_id: str, stripe: Any,
) -> MerchantAccount:
    """Return the entity's merchant account, creating the Stripe Express
    account on first call. Caller commits."""
    row = await get_merchant_account(db, entity_id)
    if row is not None:
        return row
    account = stripe.Account.create(
        type="express",
        metadata={"entity_id": entity_id},
        # Stripe dedupes retried creates for the same entity server-side.
        idempotency_key=f"merchant-account-{entity_id}",
    )
    new_row = MerchantAccount(entity_id=entity_id, stripe_account_id=account.id)

    async def _insert() -> MerchantAccount:
        db.add(new_row)
        await db.flush()
        return new_row

    if not hasattr(db, "begin_nested"):
        return await _insert()

    try:
        async with db.begin_nested():
            return await _insert()
    except IntegrityError:
        # Lost the insert race on ux_merchant_accounts_entity — return the
        # winner's row (the shared idempotency key means both requests got
        # the same Stripe account, so nothing is orphaned).
        row = await get_merchant_account(db, entity_id)
        if row is not None:
            return row
        raise


def create_onboarding_link(*, stripe: Any, stripe_account_id: str, app_url: str) -> str:
    link = stripe.AccountLink.create(
        account=stripe_account_id,
        refresh_url=f"{app_url}/merchant?onboard=refresh",
        return_url=f"{app_url}/merchant?onboard=return",
        type="account_onboarding",
    )
    return link.url


async def apply_account_update(db: AsyncSession, account: dict) -> bool:
    """Mirror a Stripe ``account.updated`` event onto merchant_accounts.
    Returns False when the account id is unknown (not ours / stale)."""
    row = (await db.execute(
        select(MerchantAccount).where(
            MerchantAccount.stripe_account_id == account.get("id", "")
        )
    )).scalar_one_or_none()
    if row is None:
        return False
    row.charges_enabled = bool(account.get("charges_enabled", False))
    row.payouts_enabled = bool(account.get("payouts_enabled", False))
    if account.get("country"):
        row.country = account["country"]
    if account.get("details_submitted") and row.charges_enabled:
        row.onboarding_status = "complete"
    await db.flush()
    return True
