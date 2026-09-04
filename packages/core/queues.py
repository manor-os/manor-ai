"""Declared Celery queues and the task → queue registry.

Manor runs four different kinds of Celery task in one broker:

* the **control plane** — ``internal_worker_tick`` (5s), ``cleanup_expired_leases``
  (30s), ``scheduler.tick`` (60s), the ops/monitor beats. Every one of them is
  short, bounded and DB-shaped, and every one of them must run *on time*: they
  are the loop that dispatches new leases and reclaims dead ones.
* **interactive** — durable Chat execution and exact checkpoint resume. These
  tasks are user-facing and may stay active across agent/tool boundaries, so
  cloud deployments isolate them from the control-plane concurrency pool.
* **heavy** — ``execute_lease`` and friends. A single step may legitimately run
  for hours (``packages/core/services/step_deadline.py`` allows up to 6h).
* **versioned recovery** — short settlement-only deliveries whose wire and
  fencing contract must never be consumed by a pre-upgrade worker.

With one queue and one worker they share the same concurrency slots, so four
concurrent long steps stop the control plane dead: no new leases are
dispatched, no expired leases reclaimed, no scheduled job fires. Splitting them
by queue is what makes a long step a *local* cost instead of a global stall.

The split is an explicit, exhaustive registry keyed by the task's dotted name —
never a prefix/glob match over names. ``tests/test_orchestration_hardening.py``
asserts the registry covers exactly the set of registered tasks, so a task
added without a queue declaration fails the suite instead of silently landing
somewhere convenient.
"""
from __future__ import annotations

from enum import Enum


class CeleryQueue(str, Enum):
    """Every queue Manor declares.

    ``CONTROL`` deliberately keeps Celery's historical default queue name
    (``celery``): it is what ``task_default_queue`` points at, what a worker
    started without ``-Q`` consumes, and what the existing broker-depth probes
    already look at. So a bare ``celery -A packages.core.celery_app worker``
    still runs the whole control plane after this change; only the work tasks
    move, and the deploy that consumes the new queue ships with them.
    """

    CONTROL = "celery"
    INTERACTIVE = "interactive"
    HEAVY = "heavy"
    # Settlement payloads are a versioned wire contract. Keeping them on a
    # queue introduced with their consumer prevents an old rolling-deploy
    # worker from accepting a message whose fencing semantics it does not know.
    RECOVERY_V2 = "recovery-v2"


#: Queue used for a task that is registered but missing from the registry.
#: HEAVY on purpose: an undeclared heavy task on the control plane is the exact
#: failure this module exists to prevent, while an undeclared control task on
#: the heavy queue merely runs with normal work latency. The registry is still
#: required — the guard test fails on any gap — this is only the runtime
#: behaviour while that gap exists in someone's branch.
UNDECLARED_TASK_QUEUE = CeleryQueue.HEAVY


# Celery's own built-in tasks. Listed by name rather than matched by their
# ``celery.`` namespace so the registry stays a closed set: adding a name here
# is a deliberate act, and the guard test cross-checks this list against what
# the app actually registers.
CELERY_BUILTIN_TASK_QUEUES: dict[str, CeleryQueue] = {
    "celery.accumulate": CeleryQueue.CONTROL,
    "celery.backend_cleanup": CeleryQueue.CONTROL,
    "celery.chain": CeleryQueue.CONTROL,
    "celery.chord": CeleryQueue.CONTROL,
    "celery.chord_unlock": CeleryQueue.CONTROL,
    "celery.chunks": CeleryQueue.CONTROL,
    "celery.group": CeleryQueue.CONTROL,
    "celery.map": CeleryQueue.CONTROL,
    "celery.starmap": CeleryQueue.CONTROL,
}


# ── the registry ──────────────────────────────────────────────────────
#
# Read the blocks as the execution classes plus versioned recovery. The question
# to ask of a new task is
# not "is it beat-driven?" but "can it occupy a worker slot for an unbounded
# time?". A beat-driven task that fans out network calls (integrations.health_tick,
# embeddings.sweep_pending) belongs on HEAVY even though beat fires it: beat lives
# in the control-plane worker, but the task itself must not run there.

TASK_QUEUES: dict[str, CeleryQueue] = {
    # ── control plane: short, bounded, must run on schedule ──────────
    "packages.core.tasks.ai_tasks.internal_worker_tick": CeleryQueue.CONTROL,
    "packages.core.tasks.ai_tasks.cleanup_expired_leases": CeleryQueue.CONTROL,
    "packages.core.tasks.ai_tasks.budget_monthly_reset": CeleryQueue.CONTROL,
    "scheduler.tick": CeleryQueue.CONTROL,
    "scheduler.skill_generation_sweep": CeleryQueue.CONTROL,
    "workflow.resume_sweep": CeleryQueue.CONTROL,
    "workflow.terminal_effect_sweep": CeleryQueue.CONTROL,
    "scheduler.project_scheduled_agent_result": CeleryQueue.CONTROL,
    "scheduler.recover_prepared_dispatch": CeleryQueue.CONTROL,
    "scheduler.settle_scheduled_agent_run": CeleryQueue.RECOVERY_V2,
    "scheduler.settle_scheduled_run": CeleryQueue.RECOVERY_V2,
    "agent.recover_claim_loss_v2": CeleryQueue.RECOVERY_V2,
    "agent.recover_claim_loss_sweep_v2": CeleryQueue.RECOVERY_V2,
    "oauth.refresh_tick": CeleryQueue.CONTROL,
    "notification.dispatch_due": CeleryQueue.CONTROL,
    "monitor.heartbeat_check": CeleryQueue.CONTROL,
    "monitor.hitl_waiting_reminder": CeleryQueue.CONTROL,
    "monitor.sla_breach_check": CeleryQueue.CONTROL,
    "monitor.workspace_readiness_check": CeleryQueue.CONTROL,
    "experiments.guardrail_tick": CeleryQueue.CONTROL,
    "ops.collect_snapshot": CeleryQueue.CONTROL,
    "ops.alert_tick": CeleryQueue.CONTROL,
    "ops.log_scan": CeleryQueue.CONTROL,
    "ops.send_digest": CeleryQueue.CONTROL,
    "runtime.sandbox_scheduler": CeleryQueue.CONTROL,
    "runtime.outbox_dispatch": CeleryQueue.CONTROL,
    "runtime.allocate_sandbox": CeleryQueue.INTERACTIVE,
    "runtime.execute_run": CeleryQueue.INTERACTIVE,
    "runtime.resume_run": CeleryQueue.INTERACTIVE,

    # ── heavy: unbounded duration, user-visible execution ────────────
    "packages.core.tasks.ai_tasks.execute_lease": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.plan_and_run_task": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_plan": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_agent_task": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_morning_briefing": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_strategist_review": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_goal_measurement": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_workspace_stat_collection": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_outcome_evaluation": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.run_chat_insight_extraction": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.generate_job_skill": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.generate_knowledge_content": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.fetch_and_index_url_document": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.process_document_embeddings": CeleryQueue.HEAVY,
    "packages.core.tasks.ai_tasks.send_agent_greetings": CeleryQueue.HEAVY,
    "memory.entity_chat_extraction_sweep": CeleryQueue.HEAVY,
    "learning.apply_candidate": CeleryQueue.HEAVY,
    "embeddings.batch_index": CeleryQueue.HEAVY,
    "embeddings.sweep_pending": CeleryQueue.HEAVY,
    "media.cleanup_media_references": CeleryQueue.HEAVY,
    "media.process_video_job": CeleryQueue.HEAVY,
    "media.recover_stale_jobs": CeleryQueue.HEAVY,
    # Channel replies are user-facing and must remain available while a
    # long-running plan occupies the heavy worker.
    "channel.dispatch_inbound": CeleryQueue.INTERACTIVE,
    "channel.ms_teams_subscription_tick": CeleryQueue.HEAVY,
    "channel.outlook_subscription_tick": CeleryQueue.HEAVY,
    "channel.register_integration_webhooks": CeleryQueue.HEAVY,
    # Both tasks make retryable provider calls and may occupy a worker for up
    # to several minutes; keep them off the latency-sensitive control plane.
    "channel.disconnect_whatsapp_business": CeleryQueue.HEAVY,
    "channel.retire_nango_connection": CeleryQueue.HEAVY,
    "events.dispatch_external_due": CeleryQueue.HEAVY,
    "integrations.health_check": CeleryQueue.HEAVY,
    "integrations.health_tick": CeleryQueue.HEAVY,
    "scheduler.dispatch_job": CeleryQueue.HEAVY,
    "monitor.daily_health_briefing": CeleryQueue.HEAVY,
    "calendar.reconcile_booking_metadata": CeleryQueue.HEAVY,
    "maintenance.cleanup_ai_edit_sessions": CeleryQueue.HEAVY,
    "maintenance.cleanup_chat_uploads": CeleryQueue.HEAVY,
    "maintenance.repair_missing_document_files": CeleryQueue.HEAVY,
    "maintenance.sync_openrouter_pricing": CeleryQueue.HEAVY,
    "ops.purge_soft_deleted_users": CeleryQueue.HEAVY,
    "ops.purge_soft_deleted_workspaces": CeleryQueue.HEAVY,
    "metrics.daily_rollup": CeleryQueue.HEAVY,
    "metrics.http_flush": CeleryQueue.HEAVY,
    "run_workflow": CeleryQueue.HEAVY,
    "resume_workflow": CeleryQueue.HEAVY,
}


def queue_for_task(name: str) -> CeleryQueue:
    """The declared queue for ``name``. Exactly one, always."""
    declared = TASK_QUEUES.get(name)
    if declared is not None:
        return declared
    builtin = CELERY_BUILTIN_TASK_QUEUES.get(name)
    if builtin is not None:
        return builtin
    return UNDECLARED_TASK_QUEUE


def route_task(name: str, *_args: object, **_kwargs: object) -> dict[str, str]:
    """Celery ``task_routes`` callable — registry lookup, nothing else."""
    return {"queue": queue_for_task(name).value}


def declared_task_names() -> frozenset[str]:
    """Every Manor task name the registry declares (built-ins excluded)."""
    return frozenset(TASK_QUEUES)


__all__ = [
    "CELERY_BUILTIN_TASK_QUEUES",
    "CeleryQueue",
    "TASK_QUEUES",
    "UNDECLARED_TASK_QUEUE",
    "declared_task_names",
    "queue_for_task",
    "route_task",
]
