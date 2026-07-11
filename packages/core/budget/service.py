"""Budget read / set helpers for the API + admin UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionStep
from packages.core.models.workspace import Workspace


@dataclass
class BudgetStatus:
    """Budget snapshot. The user-facing fields are ``*_credits`` —
    that's what the UI / chat alerts surface. ``*_usd`` is the precise
    storage representation; surfaced for admin / billing audit only."""

    # User-facing (credits — what the UI shows)
    monthly_budget_credits: Optional[int]
    monthly_spent_credits: int
    monthly_remaining_credits: Optional[int]
    pct_used: Optional[float]
    """0..>1.0 — None when no budget set."""

    # State + behaviour
    alert_state: Optional[str]
    """'normal' | 'warning_80' | 'critical_100' | None."""
    auto_pause_on_budget: bool
    budget_reset_at: Optional[datetime]
    days_until_month_end: int

    # Audit / admin (USD — precise storage representation)
    monthly_budget_usd: Optional[float]
    monthly_spent_usd: float
    credits_per_usd: int
    """The rate at the time of read; lets the UI explain "$X = N credits"
    if the operator wants to see the underlying."""


async def get_budget_status(
    db: AsyncSession, workspace_id: str,
) -> Optional[BudgetStatus]:
    from packages.core.services.credit_service import (
        usd_to_credits, get_rates,
    )

    ws = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        return None

    spent_usd = float(ws.monthly_spent_usd or 0)
    budget_usd = float(ws.monthly_budget_usd) if ws.monthly_budget_usd else None
    pct = (spent_usd / budget_usd) if (budget_usd and budget_usd > 0) else None

    rates = get_rates()
    spent_credits = usd_to_credits(spent_usd)
    budget_credits = usd_to_credits(budget_usd) if budget_usd is not None else None
    remaining_credits = (
        max(0, budget_credits - spent_credits) if budget_credits is not None else None
    )

    return BudgetStatus(
        monthly_budget_credits=budget_credits,
        monthly_spent_credits=spent_credits,
        monthly_remaining_credits=remaining_credits,
        pct_used=pct,
        alert_state=ws.budget_alert_state,
        auto_pause_on_budget=ws.auto_pause_on_budget,
        budget_reset_at=ws.budget_reset_at,
        days_until_month_end=_days_until_month_end(),
        monthly_budget_usd=budget_usd,
        monthly_spent_usd=spent_usd,
        credits_per_usd=rates["credits_per_usd"],
    )


async def set_workspace_budget(
    db: AsyncSession,
    workspace_id: str,
    *,
    monthly_budget_credits: Optional[int] = None,
    monthly_budget_usd: Optional[float] = None,
    auto_pause_on_budget: Optional[bool] = None,
    reset_alert_state: bool = True,
) -> Optional[Workspace]:
    """Update a workspace's budget. Caller commits.

    Pass ``monthly_budget_credits`` (preferred — matches the UI) OR
    ``monthly_budget_usd`` (admin / billing path). When both are given,
    credits wins. Pass either as ``None`` AND the other as ``0`` to
    clear the cap. Pass *both* as ``None`` to leave the budget alone
    (useful when only updating ``auto_pause_on_budget``).

    ``reset_alert_state=True`` (default) clears any sticky warning /
    critical state so raising the cap instantly stops alert spam.
    """
    from packages.core.services.credit_service import credits_to_usd

    ws = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        return None

    # Decide whether to mutate the cap. Either-or-neither.
    if monthly_budget_credits is not None:
        if monthly_budget_credits <= 0:
            ws.monthly_budget_usd = None
        else:
            ws.monthly_budget_usd = Decimal(str(credits_to_usd(monthly_budget_credits)))
    elif monthly_budget_usd is not None:
        ws.monthly_budget_usd = (
            Decimal(str(monthly_budget_usd))
            if monthly_budget_usd > 0 else None
        )
    # else: both None → no budget change.

    if auto_pause_on_budget is not None:
        ws.auto_pause_on_budget = auto_pause_on_budget
    if reset_alert_state:
        ws.budget_alert_state = "normal"

    await db.flush()
    return ws


async def get_workspace_spent_credits_per_kind(
    db: AsyncSession,
    workspace_id: str,
    *,
    since: Optional[datetime] = None,
) -> dict[str, int]:
    """Return current-month spend grouped by execution step kind.

    Governance policy uses this to enforce ``budget_caps_per_kind`` at lease
    checkout. We parse JSON costs in Python so a bad historical ``cost.usd``
    value cannot break dispatch with a SQL cast error.
    """
    from packages.core.services.credit_service import usd_to_credits

    since = since or _month_start()
    rows = (await db.execute(
        select(ExecutionStep.kind, ExecutionStep.cost)
        .where(
            ExecutionStep.workspace_id == workspace_id,
            ExecutionStep.finished_at.is_not(None),
            ExecutionStep.finished_at >= since,
        )
    )).all()

    by_kind_usd: dict[str, Decimal] = {}
    for kind, cost in rows:
        raw = (cost or {}).get("usd") if isinstance(cost, dict) else None
        if raw is None:
            continue
        try:
            amount = Decimal(str(raw))
        except Exception:
            continue
        if amount <= 0:
            continue
        key = str(kind or "unknown")
        by_kind_usd[key] = by_kind_usd.get(key, Decimal(0)) + amount

    return {
        kind: usd_to_credits(float(amount))
        for kind, amount in by_kind_usd.items()
    }


def _days_until_month_end(now: Optional[datetime] = None) -> int:
    from calendar import monthrange

    now = now or datetime.now(timezone.utc)
    last_day = monthrange(now.year, now.month)[1]
    return max(0, last_day - now.day)


def _month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
