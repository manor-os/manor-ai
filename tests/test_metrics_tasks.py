"""Celery task wiring test for the metrics daily rollup."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_metrics_daily_rollup_task_calls_the_rollup_service():
    from packages.core.tasks import metrics_tasks

    called = {}

    async def fake_run_daily_rollup(_db, *, target_day=None):
        called["target_day"] = target_day
        return {"day": "2026-07-15", "usage_rows": 1, "tool_call_rows": 0}

    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def commit(self):
            pass

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

    with patch("packages.core.services.metrics_rollup.run_daily_rollup", new=boom):
        metrics_tasks.metrics_daily_rollup()  # must not raise
