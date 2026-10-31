"""Celery task wiring test for the metrics daily rollup."""
from __future__ import annotations

from unittest.mock import patch


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass


def test_metrics_daily_rollup_task_calls_the_rollup_service():
    from packages.core.tasks import metrics_tasks

    called = {}

    async def fake_run_daily_rollup(_db, *, target_day=None):
        called["target_day"] = target_day
        return {"day": "2026-07-15", "usage_rows": 1, "tool_call_rows": 0}

    with (
        patch("packages.core.services.metrics_rollup.run_daily_rollup", new=fake_run_daily_rollup),
        patch("packages.core.database.create_worker_session", return_value=lambda: _FakeSession()),
    ):
        metrics_tasks.metrics_daily_rollup()

    assert called["target_day"] is None


def test_metrics_daily_rollup_task_never_raises_on_failure():
    """Best-effort like every other beat task in ai_tasks.py — a failure
    is logged, never propagated to Celery as a task failure/retry storm."""
    from packages.core.tasks import metrics_tasks

    async def boom(*_a, **_kw):
        raise RuntimeError("db unavailable")

    with (
        patch("packages.core.services.metrics_rollup.run_daily_rollup", new=boom),
        patch("packages.core.services.workspace_health_rollup.rollup_workspace_day", new=boom),
        patch("packages.core.database.create_worker_session", return_value=lambda: _FakeSession()),
    ):
        metrics_tasks.metrics_daily_rollup()  # must not raise


def test_workspace_health_refresh_is_hourly_on_the_heavy_queue():
    from packages.core.celery_app import celery_app
    from packages.core.queues import TASK_QUEUES, CeleryQueue

    entry = celery_app.conf.beat_schedule["metrics-workspace-health-refresh"]
    assert entry["task"] == "metrics.workspace_health_refresh"
    assert TASK_QUEUES["metrics.workspace_health_refresh"] is CeleryQueue.HEAVY


def test_daily_rollup_also_rolls_workspace_health_even_if_the_platform_rollup_fails():
    from packages.core.tasks import metrics_tasks

    rolled = {}

    async def boom(*_a, **_kw):
        raise RuntimeError("platform rollup failed")

    async def fake_workspace_day(_db, *, day, now=None):
        rolled["day"] = day
        return {"day": day, "rows": 0, "pruned": 0}

    with (
        patch("packages.core.services.metrics_rollup.run_daily_rollup", new=boom),
        patch("packages.core.services.workspace_health_rollup.rollup_workspace_day", new=fake_workspace_day),
        patch("packages.core.database.create_worker_session", return_value=lambda: _FakeSession()),
    ):
        metrics_tasks.metrics_daily_rollup(target_day="2026-07-15")

    assert rolled["day"] == "2026-07-15"


def test_workspace_health_refresh_skips_while_another_run_holds_the_lock():
    from packages.core.tasks import metrics_tasks

    refreshed = []

    async def held(_token):
        return False

    async def fake_refresh(_db, *, now=None):
        refreshed.append(now)
        return {"workspaces": 0, "by_status": {}}

    with (
        patch.object(metrics_tasks, "_try_acquire_health_lock", new=held),
        patch("packages.core.services.workspace_health_snapshot.refresh_workspace_health", new=fake_refresh),
        patch("packages.core.database.create_worker_session", return_value=lambda: _FakeSession()),
    ):
        metrics_tasks.metrics_workspace_health_refresh()

    assert refreshed == []
