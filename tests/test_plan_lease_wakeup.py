"""A terminal worker lease must wake the owning execution plan."""

import pytest


def test_wake_plan_cycle_enqueues_run_plan(monkeypatch):
    from packages.core.plans.wakeup import wake_plan_cycle

    calls: list[str] = []

    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))

    assert wake_plan_cycle("01KTESTPLANWAKEUP0000000000") is True

    assert calls == ["01KTESTPLANWAKEUP0000000000"]
