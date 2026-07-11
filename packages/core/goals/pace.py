"""Pure pace_status computation. No DB, no IO — easy to test.

Pace classifies how a Goal's current_value compares against where it
"should" be given the elapsed fraction of its deadline window.

Returns one of:
  'ahead'      — > 1.10 of expected pace (or already at/beyond target)
  'on_track'   — 0.90 - 1.10
  'behind'     — 0.60 - 0.90
  'at_risk'    — < 0.60
  'achieved'   — current_value has reached the target in the baseline→target direction
  'unknown'    — missing inputs (no baseline / no deadline)
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional


def compute_pace(
    *,
    current_value: Optional[Decimal],
    baseline_value: Optional[Decimal],
    target_value: Decimal,
    created_at: datetime,
    deadline: Optional[date],
    today: Optional[date] = None,
) -> str:
    """Classify a Goal's pace given its progress and timeline."""
    if current_value is None or target_value is None:
        return "unknown"

    current = Decimal(current_value)
    target = Decimal(target_value)
    baseline = Decimal(baseline_value) if baseline_value is not None else None

    # Already done → no pace classification needed. Goals may be either
    # higher-is-better (followers, revenue) or lower-is-better (latency, stale
    # rate). The baseline tells us the intended direction.
    if baseline is not None and target < baseline:
        achieved = current <= target
    else:
        achieved = current >= target
    if achieved:
        return "achieved"

    if deadline is None or baseline_value is None:
        return "unknown"

    today = today or date.today()
    start = created_at.date() if isinstance(created_at, datetime) else created_at
    total_days = (deadline - start).days
    if total_days <= 0:
        # Deadline already passed without achievement.
        return "at_risk"

    elapsed_days = max(0, (today - start).days)
    elapsed_frac = min(1.0, elapsed_days / total_days)

    distance = target - baseline
    if distance == 0:
        # Target equals baseline — degenerate; treat as achieved if
        # current >= target, else unknown (no movement expected).
        return "unknown"

    progress_frac = float(
        (current - baseline) / distance
    )

    # Avoid divide-by-zero when no meaningful time has elapsed yet. A brand-new
    # workspace can still have imported/sandbox evidence, so keep positive
    # movement visible instead of downgrading an already-moving goal to unknown.
    if elapsed_frac < 0.01:
        if progress_frac > 0.01:
            return "ahead"
        if progress_frac < -0.01:
            return "at_risk"
        return "unknown"

    ratio = progress_frac / elapsed_frac
    if ratio >= 1.10:
        return "ahead"
    if ratio >= 0.90:
        return "on_track"
    if ratio >= 0.60:
        return "behind"
    return "at_risk"
