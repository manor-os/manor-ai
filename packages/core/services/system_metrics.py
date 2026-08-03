"""Persist host resource samples out of the ops snapshot.

``ops.collect_snapshot`` (Celery beat, every 30s) already collects host
CPU/mem/disk/load via ``ops_service.collect_snapshot`` and writes one
JSON blob to Redis with a short TTL — history used to be thrown away.
``record_system_sample`` extracts the four platform-level gauges from
that same snapshot dict and inserts one ``system_metrics_samples`` row,
giving the admin Performance dashboard a queryable trend series
(``metrics_query.system_metrics``). Retention (14 days) is enforced by
``metrics_rollup.run_daily_rollup``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.system_metrics import SystemMetricsSample

logger = logging.getLogger(__name__)


def _as_float(value: object) -> Optional[float]:
    """Numeric → float; anything else (str, bool, None, dict...) → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_sample_fields(snapshot: object) -> dict:
    """Pull the four platform-level gauges out of a
    ``ops_service.collect_snapshot()`` dict (``host.cpu_pct``,
    ``host.mem.pct``, ``host.disk.pct``, ``host.load_avg[0]``).
    Missing or malformed keys degrade to None per field — never raise
    (the host dict is ``{"error": ...}`` when psutil is unavailable).
    """
    host = snapshot.get("host") if isinstance(snapshot, dict) else None
    if not isinstance(host, dict):
        host = {}
    mem = host.get("mem")
    disk = host.get("disk")
    load = host.get("load_avg")
    return {
        "cpu_pct": _as_float(host.get("cpu_pct")),
        "mem_pct": _as_float(mem.get("pct")) if isinstance(mem, dict) else None,
        "disk_pct": _as_float(disk.get("pct")) if isinstance(disk, dict) else None,
        "load_1m": _as_float(load[0]) if isinstance(load, (list, tuple)) and load else None,
    }


async def record_system_sample(
    db: AsyncSession, snapshot: object,
) -> Optional[SystemMetricsSample]:
    """Insert one ``system_metrics_samples`` row from an ops snapshot.

    Returns the flushed (uncommitted — caller commits) row, or ``None``
    when the snapshot carries no usable host gauge at all (e.g. psutil
    unavailable) — an all-None row would only add chart noise. A partial
    snapshot still produces a row, with None for the missing gauges.
    """
    fields = extract_sample_fields(snapshot)
    if all(v is None for v in fields.values()):
        return None
    ts = snapshot.get("ts") if isinstance(snapshot, dict) else None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        sampled_at = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        sampled_at = datetime.now(timezone.utc)
    row = SystemMetricsSample(sampled_at=sampled_at, **fields)
    db.add(row)
    await db.flush()
    return row
