"""Orchestration hazards that long-running background work exposes.

1. **Redis re-delivered long tasks.** ``task_acks_late=True`` acks only when a
   task *finishes*, but the Redis transport has no server-side ack — kombu
   emulates one with ``visibility_timeout``, default ONE HOUR. Any task past
   60 minutes was handed to a second worker while the first was still running
   it: duplicated publishes, duplicated media generation, duplicated paid API
   calls.
2. **One queue for everything.** Long work shared its concurrency slots with
   ``cleanup_expired_leases`` / ``scheduler.tick``. A few concurrent long runs
   stalled the whole system.

These tests pin each fix to its contract, not to its current numbers: the
timeout ordering and the queue *registry*.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.celery_app import CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS, celery_app
from packages.core.models.base import generate_ulid
from packages.core.models.worker import Worker, WorkerActivityLog
from packages.core.queues import (
    CELERY_BUILTIN_TASK_QUEUES,
    TASK_QUEUES,
    UNDECLARED_TASK_QUEUE,
    CeleryQueue,
    queue_for_task,
    route_task,
)


# ══ Hazard 1 — the broker must outlast the task ═══════════════════════

def test_task_agent_loop_limits_sit_under_visibility_and_above_its_slice():
    """visibility > hard > soft > the agent loop's own time slice.

    The loop yields at a round boundary once its slice is spent and re-stages
    its own wake. If the slice reached the soft limit, Celery would interrupt
    a round instead; if the hard limit reached visibility, the broker would
    hand the same wake to a second worker.
    """
    from packages.core.tasks.runtime_tasks import run_task_agent_loop_task
    from packages.core.workers.task_agent_loop import TASK_AGENT_LOOP_SLICE_SECONDS

    assert queue_for_task("runtime.run_task_agent_loop") is CeleryQueue.HEAVY
    assert (
        CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
        > run_task_agent_loop_task.time_limit
        > run_task_agent_loop_task.soft_time_limit
        > TASK_AGENT_LOOP_SLICE_SECONDS
    )
    assert run_task_agent_loop_task.max_retries == 0


@pytest.mark.asyncio
async def test_stale_external_workers_become_offline_without_touching_live_workers(
    db_session,
):
    from sqlalchemy import select

    from packages.core.workers import mark_stale_external_workers_offline

    now = datetime.now(timezone.utc)
    stale = Worker(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        kind="custom_http",
        display_name="Stale external worker",
        capabilities={},
        status="active",
        last_heartbeat_at=now - timedelta(minutes=10),
    )
    fresh = Worker(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        kind="custom_http",
        display_name="Fresh external worker",
        capabilities={},
        status="active",
        last_heartbeat_at=now,
    )
    internal_worker = Worker(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        kind="internal",
        display_name="Internal worker",
        capabilities={},
        status="active",
        last_heartbeat_at=now - timedelta(minutes=10),
    )
    db_session.add_all([stale, fresh, internal_worker])
    await db_session.flush()

    changed = await mark_stale_external_workers_offline(
        db_session,
        now=now,
    )

    activity = await db_session.scalar(
        select(WorkerActivityLog).where(
            WorkerActivityLog.worker_id == stale.id,
            WorkerActivityLog.event == "offline",
        )
    )
    assert changed == 1
    assert stale.status == "offline"
    assert fresh.status == "active"
    assert internal_worker.status == "active"
    assert activity is not None
    assert activity.payload_summary == {"reason": "heartbeat_stale"}


def test_celery_app_actually_applies_the_visibility_timeout():
    """The constant is worthless unless the transport is configured with it.

    Kombu's Redis default is 3600s; leaving ``broker_transport_options`` unset
    is precisely the bug — so assert the app carries the derived value, and
    that it is nowhere near the default.
    """
    options = celery_app.conf.broker_transport_options or {}
    assert options.get("visibility_timeout") == CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
    assert options["visibility_timeout"] > 3600

    result_options = celery_app.conf.result_backend_transport_options or {}
    assert (
        result_options.get("visibility_timeout")
        == CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
    )


def test_celery_result_ttl_bounds_periodic_task_retention():
    """Beat runs frequently, so result rows cannot use Celery's one-day default."""
    assert celery_app.conf.result_expires == 3600


# ══ Hazard 2 — work and control plane are different queues ════════════

def _registered_task_names() -> set[str]:
    celery_app.loader.import_default_modules()
    return set(celery_app.tasks)


def test_every_registered_task_resolves_to_exactly_one_declared_queue():
    """The registry is exhaustive over what the app actually registers.

    Asserted against the declared mapping, never by parsing task names: a task
    called ``ops.log_scan`` and a task called ``ops.purge_soft_deleted_users``
    share a prefix and belong on opposite queues, so prefix matching would be
    wrong even where it looks tidy.
    """
    registered = _registered_task_names()
    declared = set(TASK_QUEUES) | set(CELERY_BUILTIN_TASK_QUEUES)

    undeclared = sorted(registered - declared)
    assert not undeclared, (
        "these Celery tasks have no queue declaration — add them to "
        f"packages/core/queues.py::TASK_QUEUES: {undeclared}"
    )

    stale = sorted(declared - registered)
    assert not stale, (
        "packages/core/queues.py declares queues for tasks that no longer "
        f"exist: {stale}"
    )

    for name in registered:
        assert isinstance(queue_for_task(name), CeleryQueue)


def test_control_plane_beats_and_task_agent_loop_land_in_different_queues():
    """The whole point: a long Task run cannot occupy a control-plane slot."""
    agent_loop_queue = queue_for_task("runtime.run_task_agent_loop")
    control_beats = (
        "packages.core.tasks.ai_tasks.cleanup_expired_leases",
        "scheduler.tick",
    )

    assert agent_loop_queue is CeleryQueue.HEAVY
    for beat in control_beats:
        assert queue_for_task(beat) is CeleryQueue.CONTROL
        assert queue_for_task(beat) is not agent_loop_queue


def test_settlement_tasks_use_versioned_recovery_queue():
    """Pre-upgrade workers must never consume the new settlement wire."""

    for task_name in (
        "scheduler.settle_scheduled_agent_run",
        "scheduler.settle_scheduled_run",
    ):
        assert queue_for_task(task_name) is CeleryQueue.RECOVERY_V2

    assert (
        queue_for_task("packages.core.tasks.ai_tasks.run_agent_task")
        is CeleryQueue.HEAVY
    )
    assert queue_for_task("run_workflow") is CeleryQueue.HEAVY


def test_every_beat_entry_names_a_declared_task():
    """A beat entry pointing at an unregistered name would never fire."""
    declared = set(TASK_QUEUES) | set(CELERY_BUILTIN_TASK_QUEUES)
    scheduled = {
        entry["task"] for entry in celery_app.conf.beat_schedule.values()
    }
    assert scheduled <= declared


def test_a_task_without_a_queue_declaration_falls_back_to_heavy():
    """Policy for the gap: fail the suite, and meanwhile run it on HEAVY.

    The registry is mandatory — the coverage test above fails on any missing
    entry. But between "someone added a task" and "someone declared it", the
    runtime still has to route it somewhere, and HEAVY is the safe side: an
    undeclared *heavy* task on the control plane is exactly the stall this
    module exists to prevent, whereas an undeclared *control* task on the heavy
    queue merely runs with normal work latency.
    """
    unknown = "packages.core.tasks.some_future_module.brand_new_task"
    assert unknown not in TASK_QUEUES
    assert UNDECLARED_TASK_QUEUE is CeleryQueue.HEAVY
    assert queue_for_task(unknown) is CeleryQueue.HEAVY
    assert route_task(unknown) == {"queue": CeleryQueue.HEAVY.value}


def test_declared_queues_are_wired_into_the_app_with_distinct_routing_keys():
    """Redis resolves a direct-exchange message by ROUTING KEY, not queue name.

    Celery's default routing key is "celery"; a ``Queue("heavy")`` declared
    without an explicit routing key would therefore deliver every work task
    straight back into the control plane's list. This asserts the queues are
    declared, and that the router hands each task a queue whose routing key is
    its own name.
    """
    configured = celery_app.amqp.queues
    for queue in CeleryQueue:
        assert queue.value in configured
        assert configured[queue.value].routing_key == queue.value

    routed = celery_app.amqp.router.route(
        {}, "runtime.run_task_agent_loop",
    )["queue"]
    assert (routed.name, routed.routing_key) == (
        CeleryQueue.HEAVY.value, CeleryQueue.HEAVY.value,
    )


def test_default_queue_is_the_control_plane_so_a_bare_worker_still_ticks():
    """``celery worker`` with no -Q consumes ``task_default_queue``.

    Keeping that pointed at the historical queue name means an un-flagged
    worker keeps running the orchestration loop after this change; only the
    heavy tasks moved, and the deploy that consumes ``heavy`` ships with them.
    """
    assert celery_app.conf.task_default_queue == CeleryQueue.CONTROL.value
    assert CeleryQueue.CONTROL.value == "celery"
