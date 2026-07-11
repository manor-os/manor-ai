"""Budget gates — call before issuing a lease.

Both checks return ``(allowed, reason)``:

  * ``check_workspace_budget(workspace_id)`` — returns False when the
    workspace has both a budget set AND ``auto_pause_on_budget=True``
    AND ``monthly_spent >= monthly_budget``. If ``auto_pause_on_budget``
    is False, returns True (logging-only mode).

  * ``check_worker_budget(worker)`` — same logic but on the Worker row.

Used by ``Dispatcher.checkout_steps_for_worker`` so over-budget
workspaces / workers don't get any new leases — existing in-flight
leases continue to run + be reported (we don't kill them).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workspace import Workspace
from packages.core.models.worker import Worker

logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Raised by service helpers — the dispatcher uses the bool/reason
    return from check_* instead since it skips silently per worker."""


async def check_workspace_budget(
    db: AsyncSession, workspace_id: Optional[str],
) -> Tuple[bool, str]:
    """Returns (allowed, reason). ``allowed=True`` means "go ahead"."""
    if not workspace_id:
        return True, ""
    ws = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        return True, ""

    return _check_budget(
        spent=ws.monthly_spent_usd,
        budget=ws.monthly_budget_usd,
        auto_pause=ws.auto_pause_on_budget,
        kind="workspace",
    )


def check_worker_budget(worker: Worker) -> Tuple[bool, str]:
    return _check_budget(
        spent=worker.monthly_spent_usd,
        budget=worker.monthly_budget_usd,
        auto_pause=worker.auto_pause_on_budget,
        kind="worker",
    )


def _check_budget(
    *,
    spent: Optional[Decimal],
    budget: Optional[Decimal],
    auto_pause: bool,
    kind: str,
) -> Tuple[bool, str]:
    if budget is None or budget <= 0:
        return True, ""    # no cap configured
    spent = spent or Decimal(0)
    if spent < budget:
        return True, ""
    if not auto_pause:
        # Over budget but logging-only mode — let it through with a
        # noisy log line so the operator sees overrun in their logs.
        logger.warning(
            "%s over budget: spent=%s budget=%s — auto_pause off, allowing",
            kind, spent, budget,
        )
        return True, ""
    return False, (
        f"{kind} over budget: spent=${float(spent):.2f} >= budget=${float(budget):.2f}"
    )
