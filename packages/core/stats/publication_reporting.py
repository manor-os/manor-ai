"""Generic publication observation comparisons built on Workspace Stats."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation


def _time(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


async def list_publication_observations(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    stat_ids: list[str],
    window_end: datetime | str,
) -> list[WorkspaceStatObservation]:
    """List observations before an exclusive report cutoff."""

    if not stat_ids:
        return []
    return list(
        (
            await db.execute(
                select(WorkspaceStatObservation)
                .where(
                    WorkspaceStatObservation.entity_id == entity_id,
                    WorkspaceStatObservation.workspace_id == workspace_id,
                    WorkspaceStatObservation.stat_id.in_(stat_ids),
                    WorkspaceStatObservation.observed_at < _time(window_end),
                )
                .order_by(WorkspaceStatObservation.observed_at, WorkspaceStatObservation.id)
            )
        ).scalars()
    )


def compare_publication_stats(
    stats: list[WorkspaceStat],
    observations: list[WorkspaceStatObservation],
    *,
    window_start: datetime | str,
    window_end: datetime | str,
) -> dict[str, Any]:
    """Return latest values and exact same-definition interval deltas by stat."""

    start, end = _time(window_start), _time(window_end)
    if end <= start:
        raise ValueError("window_end must be after window_start")
    by_stat: dict[str, list[WorkspaceStatObservation]] = {stat.id: [] for stat in stats}
    for observation in observations:
        if observation.stat_id in by_stat and observation.observed_at < end:
            by_stat[observation.stat_id].append(observation)
    results = []
    for stat in stats:
        rows = by_stat[stat.id]
        latest = rows[-1] if rows else None
        item: dict[str, Any] = {
            "stat_id": stat.id,
            "key": stat.key,
            "subject": (stat.collector_config or {}).get("subject"),
            "latest_value": float(latest.value) if latest and latest.value is not None else None,
            "latest_observed_at": latest.observed_at.isoformat() if latest else None,
            "source": latest.source if latest else None,
            "evidence": dict(latest.evidence or {}) if latest else {},
            "exact_delta": None,
            "delta_reason": "no_observation" if latest is None else None,
        }
        if latest is None:
            item["delta_reason"] = "no_observation"
        elif latest.value is None:
            item["delta_reason"] = "unavailable"
        elif latest.observed_at < start:
            item["delta_reason"] = "no_current_period_observation"
        elif (latest.evidence or {}).get("precision") != "exact":
            item["delta_reason"] = "rounded_observation"
        else:
            definition = (latest.evidence or {}).get("definition", "")
            baselines = [
                row
                for row in rows
                if row.observed_at < start
                and row.value is not None
                and (row.evidence or {}).get("precision") == "exact"
                and (row.evidence or {}).get("definition", "") == definition
                and row.source == latest.source
            ]
            if not baselines:
                item["delta_reason"] = "missing_comparable_baseline"
            else:
                baseline = baselines[-1]
                delta = latest.value - baseline.value
                item["baseline_observed_at"] = baseline.observed_at.isoformat()
                if delta < 0:
                    item["delta_reason"] = "counter_decrease"
                else:
                    item["exact_delta"] = float(delta)
        results.append(item)
    return {"window": {"start": start.isoformat(), "end": end.isoformat()}, "stats": results}
