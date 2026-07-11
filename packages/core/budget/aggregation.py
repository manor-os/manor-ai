"""Cost roll-up + monthly reset.

``accumulate_step_cost`` is called from Dispatcher.complete_lease the
moment a worker reports a lease done. It:

  1. Adds ``step.cost.usd`` to ``plan.cost_tracking.usd``.
  2. Adds the same to ``workspace.monthly_spent_usd``.
  3. Adds the same to ``worker.monthly_spent_usd``.
  4. Fires chat alerts on 80% / 100% threshold crossings — deduped via
     ``workspace.budget_alert_state``.

Idempotent on its own — but Dispatcher.complete_lease only fires once
per lease (LeaseNotActive guards re-completion), so double-counting
isn't possible at the lease layer either.

``monthly_reset_scan`` runs daily; for each workspace/worker whose
``budget_reset_at`` is in a past month, it zeroes ``monthly_spent_usd``
and clears ``budget_alert_state``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.workspace import Workspace
from packages.core.models.worker import WorkLease, Worker

logger = logging.getLogger(__name__)


_WARNING_THRESHOLD = Decimal("0.80")
_CRITICAL_THRESHOLD = Decimal("1.00")


async def accumulate_step_cost(
    db: AsyncSession,
    step: ExecutionStep,
    lease: Optional[WorkLease] = None,
    *,
    notify: bool = True,
) -> dict:
    """Roll one completed step's cost into plan + workspace + worker
    accumulators. Caller commits.

    Returns a small status dict:
      {usd: <added>, plan_total: <after>, workspace_total: <after>,
       workspace_pct: <0..1>, alert_emitted: <state-change>}
    """
    cost = step.cost or {}
    raw = cost.get("usd")
    if raw is None:
        return {"usd": 0, "alert_emitted": None}

    try:
        delta = Decimal(str(raw))
    except Exception:
        logger.warning("step %s/%s: non-numeric cost.usd=%r — skipping accumulation",
                       step.plan_id, step.step_key, raw)
        return {"usd": 0, "alert_emitted": None}
    if delta <= 0:
        return {"usd": 0, "alert_emitted": None}

    # ── Plan rollup ──
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == step.plan_id)
    )).scalar_one_or_none()
    plan_total = Decimal("0")
    if plan is not None:
        ct = dict(plan.cost_tracking or {})
        prior = Decimal(str(ct.get("usd") or 0))
        plan_total = prior + delta
        ct["usd"] = float(plan_total)
        # Also accumulate llm token counters if present — operator might
        # want a per-plan token total without summing N step rows.
        for k in ("llm_tokens_input", "llm_tokens_output", "api_calls"):
            v = cost.get(k)
            if v is not None:
                try:
                    ct[k] = int(ct.get(k, 0)) + int(v)
                except Exception:
                    pass
        plan.cost_tracking = ct

    # ── Worker rollup ──
    if lease is not None:
        worker = (await db.execute(
            select(Worker).where(Worker.id == lease.worker_id)
        )).scalar_one_or_none()
        if worker is not None:
            worker.monthly_spent_usd = (worker.monthly_spent_usd or Decimal(0)) + delta

    # ── Workspace rollup ──
    if not step.workspace_id:
        await db.flush()
        return {
            "usd": float(delta),
            "plan_total": float(plan_total),
            "workspace_total": None,
            "workspace_pct": None,
            "alert_emitted": None,
        }

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == step.workspace_id)
    )).scalar_one_or_none()
    if workspace is None:
        await db.flush()
        return {
            "usd": float(delta),
            "plan_total": float(plan_total),
            "workspace_total": None,
            "workspace_pct": None,
            "alert_emitted": None,
        }

    prev_spent = workspace.monthly_spent_usd or Decimal(0)
    new_spent = prev_spent + delta
    workspace.monthly_spent_usd = new_spent

    # ── Threshold detection ──
    alert = None
    if workspace.monthly_budget_usd and workspace.monthly_budget_usd > 0 and notify:
        budget = workspace.monthly_budget_usd
        new_pct = new_spent / budget if budget > 0 else Decimal(0)

        prev_state = workspace.budget_alert_state or "normal"
        next_state = _bucket(new_pct)

        if next_state != prev_state:
            workspace.budget_alert_state = next_state
            if (
                (prev_state == "normal" and next_state in ("warning_80", "critical_100"))
                or (prev_state == "warning_80" and next_state == "critical_100")
            ):
                alert = next_state

    await db.flush()

    if alert:
        await _post_budget_alert(workspace, new_spent, alert)

    return {
        "usd": float(delta),
        "plan_total": float(plan_total),
        "workspace_total": float(new_spent),
        "workspace_pct": (
            float(new_spent / workspace.monthly_budget_usd)
            if workspace.monthly_budget_usd else None
        ),
        "alert_emitted": alert,
    }


async def accumulate_workspace_ai_cost(
    db: AsyncSession,
    *,
    workspace_id: Optional[str],
    cost_usd: Optional[float],
    notify: bool = True,
) -> dict:
    """Roll direct workspace AI spend into the same monthly budget counter.

    Dispatcher step costs cover worker-reported non-LLM costs. LLM calls from
    chat, planner, strategist, and worker agents are billed through the credit
    ledger instead, so they need their own path into ``monthly_spent_usd`` for
    workspace budget gates to reflect the full autonomous runtime.
    """
    if not workspace_id or cost_usd is None:
        return {"usd": 0, "workspace_total": None, "workspace_pct": None, "alert_emitted": None}

    try:
        delta = Decimal(str(cost_usd))
    except Exception:
        logger.warning(
            "workspace %s: non-numeric cost_usd=%r — skipping budget accumulation",
            workspace_id, cost_usd,
        )
        return {"usd": 0, "workspace_total": None, "workspace_pct": None, "alert_emitted": None}
    if delta <= 0:
        return {"usd": 0, "workspace_total": None, "workspace_pct": None, "alert_emitted": None}

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if workspace is None:
        await db.flush()
        return {"usd": float(delta), "workspace_total": None, "workspace_pct": None, "alert_emitted": None}

    prev_spent = workspace.monthly_spent_usd or Decimal(0)
    new_spent = prev_spent + delta
    workspace.monthly_spent_usd = new_spent

    alert = None
    if workspace.monthly_budget_usd and workspace.monthly_budget_usd > 0 and notify:
        budget = workspace.monthly_budget_usd
        new_pct = new_spent / budget if budget > 0 else Decimal(0)
        prev_state = workspace.budget_alert_state or "normal"
        next_state = _bucket(new_pct)
        if next_state != prev_state:
            workspace.budget_alert_state = next_state
            if (
                (prev_state == "normal" and next_state in ("warning_80", "critical_100"))
                or (prev_state == "warning_80" and next_state == "critical_100")
            ):
                alert = next_state

    await db.flush()
    if alert:
        await _post_budget_alert(workspace, new_spent, alert)

    return {
        "usd": float(delta),
        "workspace_total": float(new_spent),
        "workspace_pct": (
            float(new_spent / workspace.monthly_budget_usd)
            if workspace.monthly_budget_usd else None
        ),
        "alert_emitted": alert,
    }


def _bucket(pct: Decimal) -> str:
    if pct >= _CRITICAL_THRESHOLD:
        return "critical_100"
    if pct >= _WARNING_THRESHOLD:
        return "warning_80"
    return "normal"


async def _post_budget_alert(
    workspace: Workspace, spent: Decimal, state: str,
) -> None:
    """Best-effort chat alert on threshold crossing."""
    try:
        from packages.core.database import async_session
        from packages.core.services.credit_service import usd_to_credits
        from packages.core.workspace_chat import service as chat_service

        budget = workspace.monthly_budget_usd or Decimal(0)
        pct = float(spent / budget * 100) if budget > 0 else 0.0
        # User-facing surface is credits, not USD.
        spent_credits = usd_to_credits(float(spent))
        budget_credits = usd_to_credits(float(budget))
        if state == "critical_100":
            body = (
                f"🛑 **Budget hit** — workspace has spent {spent_credits} "
                f"of {budget_credits} credits ({pct:.0f}%). "
                f"{'New plans paused.' if workspace.auto_pause_on_budget else 'Logging only — auto-pause off.'} "
                f"Raise the cap or wait for the monthly reset."
            )
        else:
            body = (
                f"⚠ **Budget warning** — workspace has spent {spent_credits} "
                f"of {budget_credits} credits ({pct:.0f}%). "
                f"At 100% Manor will {'pause new plans' if workspace.auto_pause_on_budget else 'continue but log'}."
            )

        async with async_session() as db:
            await chat_service.post_message(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                body=body,
                message_kind="goal_alert",   # reuse existing chat kind
                author_kind="system",
            )
            await db.commit()
    except Exception:
        logger.warning("budget alert chat post failed", exc_info=True)


# ── Monthly reset ─────────────────────────────────────────────────────

def monthly_reset_due(reset_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True if this row should be zeroed for a fresh month.

    Reset windows are calendar months in UTC. ``reset_at == None`` is
    treated as "never reset, do it now" so freshly-budgeted workspaces
    start with a clean monthly_spent.
    """
    now = now or datetime.now(timezone.utc)
    if reset_at is None:
        return True
    return (reset_at.year, reset_at.month) < (now.year, now.month)


async def monthly_reset_scan(db: AsyncSession) -> dict:
    """Scan workspaces + workers due for monthly reset, zero them.

    Run via Celery beat (daily). Idempotent: rows reset within the
    current month are no-ops on re-run.
    """
    now = datetime.now(timezone.utc)
    ws_reset = 0
    wk_reset = 0

    workspaces = list((await db.execute(
        select(Workspace).where(Workspace.monthly_budget_usd.is_not(None))
    )).scalars().all())
    for ws in workspaces:
        if monthly_reset_due(ws.budget_reset_at, now):
            ws.monthly_spent_usd = Decimal(0)
            ws.budget_alert_state = "normal"
            ws.budget_reset_at = now
            ws_reset += 1

    workers = list((await db.execute(
        select(Worker).where(Worker.monthly_budget_usd.is_not(None))
    )).scalars().all())
    for w in workers:
        if monthly_reset_due(w.budget_reset_at, now):
            w.monthly_spent_usd = Decimal(0)
            w.budget_reset_at = now
            wk_reset += 1

    await db.flush()
    if ws_reset or wk_reset:
        logger.info("budget monthly reset: workspaces=%d workers=%d", ws_reset, wk_reset)
    return {"workspaces_reset": ws_reset, "workers_reset": wk_reset}
