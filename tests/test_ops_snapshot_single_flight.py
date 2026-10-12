"""ops.collect_snapshot must never pile up on the control plane.

Its runtime scales with the number of containers on the host (one Docker
stats call each); on a large host one run outlives its 30 s beat interval,
and overlapping runs held every control-plane worker for minutes, starving
runtime.sandbox_scheduler.
"""

from __future__ import annotations

import pytest

from packages.core import queues
from packages.core.tasks import ops_tasks


def test_docker_walking_ops_tasks_have_their_own_queue():
    """The heavy worker runs agent tasks at concurrency 1; a 15 s snapshot
    every 30 s there would take half of it. On the control pool the same
    snapshot held one of two slots and the 1-2 s scheduler/outbox ticks were
    discarded meanwhile. Monitoring gets a queue of its own."""
    for name in ("ops.collect_snapshot", "ops.log_scan", "ops.alert_tick", "ops.send_digest"):
        assert queues.TASK_QUEUES[name] is queues.CeleryQueue.OPS, name
    assert queues.CeleryQueue.OPS not in (queues.CeleryQueue.CONTROL, queues.CeleryQueue.HEAVY)


def test_container_walk_is_bounded_by_the_budget(monkeypatch):
    import time
    from types import SimpleNamespace

    from packages.core.services import ops_service

    monkeypatch.setattr(ops_service, "OPS_SNAPSHOT_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(ops_service, "OPS_SNAPSHOT_MAX_WORKERS", 4)

    def make(name, delay):
        def stats(stream=False):
            time.sleep(delay)
            return {
                "memory_stats": {"usage": 10 * 1024 * 1024, "limit": 100 * 1024 * 1024},
                "cpu_stats": {},
                "precpu_stats": {},
            }

        return SimpleNamespace(
            id=name * 3,
            name=name,
            attrs={"State": {"Status": "running"}, "HostConfig": {}, "Config": {"Image": "img"}},
            stats=stats,
        )

    fast = [make(f"fast{i}", 0.05) for i in range(3)]
    slow = [make("slow", 5.0)]
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda all=True: fast + slow))
    monkeypatch.setattr(ops_service, "_safe_docker_client", lambda: client)
    monkeypatch.setattr(ops_service, "_calc_cpu_pct", lambda stats: 1.0)

    started = time.monotonic()
    rows = ops_service.collect_docker_containers()
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, "the slow container must not hold the tick"
    by_name = {row["name"]: row for row in rows}
    assert all(by_name[f"fast{i}"]["stats_sampled"] for i in range(3))
    assert by_name["slow"]["stats_sampled"] is False and by_name["slow"]["mem_mb"] == 0


def test_snapshot_is_skipped_while_another_run_holds_the_lock(monkeypatch):
    async def held(token):
        return False

    calls = []
    monkeypatch.setattr(ops_tasks, "_try_acquire_snapshot_lock", held)
    monkeypatch.setattr(ops_tasks, "_release_snapshot_lock", _record(calls, "release"))
    import packages.core.services.ops_service as ops_service

    monkeypatch.setattr(ops_service, "collect_snapshot", lambda: calls.append("collect") or {})

    result = ops_tasks.collect_snapshot_task()

    assert result == {"ok": True, "skipped": "snapshot already running"}
    assert calls == [], "neither collection nor a lock release may run for a skipped tick"


def test_snapshot_releases_the_lock_even_when_collection_fails(monkeypatch):
    async def free(token):
        return True

    calls = []
    monkeypatch.setattr(ops_tasks, "_try_acquire_snapshot_lock", free)
    monkeypatch.setattr(ops_tasks, "_release_snapshot_lock", _record(calls, "release"))
    import packages.core.services.ops_service as ops_service

    def boom():
        raise RuntimeError("docker down")

    monkeypatch.setattr(ops_service, "collect_snapshot", boom)

    result = ops_tasks.collect_snapshot_task()

    assert result["ok"] is False
    assert calls == ["release"]


def _record(calls, label):
    async def _inner(token):
        calls.append(label)

    return _inner


def test_alert_tick_never_collects_on_the_control_plane(monkeypatch):
    """With no fresh snapshot the tick must skip; live-collecting here would
    put the Docker walk back on the control queue."""

    async def nothing():
        return None

    monkeypatch.setattr(ops_tasks, "_read_snapshot_from_redis", nothing)
    import packages.core.services.ops_service as ops_service

    def boom():
        raise AssertionError("alert_tick must not collect")

    monkeypatch.setattr(ops_service, "collect_snapshot", boom)

    assert ops_tasks.alert_tick_task() == {"ok": True, "skipped": "no snapshot"}


def test_lock_release_is_fenced_by_the_owner_token():
    assert "ARGV[1]" in ops_tasks._RELEASE_IF_OWNER and "del" in ops_tasks._RELEASE_IF_OWNER
    assert ops_tasks._SNAPSHOT_LOCK_TTL_S <= 300


def test_run_coroutine_surfaces_the_real_error():
    async def failing():
        raise RuntimeError("real reason")

    with pytest.raises(RuntimeError, match="real reason"):
        ops_tasks._run_coroutine(failing)


def test_container_walk_does_not_always_skip_the_same_tail(monkeypatch):
    """With a binding budget the misses must move between ticks."""
    import time
    from types import SimpleNamespace

    from packages.core.services import ops_service

    monkeypatch.setattr(ops_service, "OPS_SNAPSHOT_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr(ops_service, "OPS_SNAPSHOT_MAX_WORKERS", 1)

    def make(name):
        def stats(stream=False):
            time.sleep(0.15)
            return {"memory_stats": {"usage": 1, "limit": 2}}

        return SimpleNamespace(
            id=name,
            name=name,
            attrs={"State": {"Status": "running"}, "HostConfig": {}, "Config": {"Image": "i"}},
            stats=stats,
        )

    containers = [make(f"c{i}") for i in range(6)]
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda all=True: list(containers)))
    monkeypatch.setattr(ops_service, "_safe_docker_client", lambda: client)
    monkeypatch.setattr(ops_service, "_calc_cpu_pct", lambda stats: 0.0)
    monkeypatch.setattr(ops_service.random, "shuffle", lambda items: items.reverse())

    rows = ops_service.collect_docker_containers()
    sampled = {row["name"] for row in rows if row["stats_sampled"]}
    assert sampled, "some containers must be sampled"
    assert "c5" in sampled, "submission order follows the shuffled order, not the list order"
    assert len(sampled) < 6, "the budget must bind in this setup"
