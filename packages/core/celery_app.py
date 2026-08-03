"""Manor AI Celery application — async task execution."""
from __future__ import annotations

import asyncio
import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown
from kombu import Queue

from packages.core.queues import CeleryQueue, route_task
from packages.core.services.step_deadline import (
    CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
)

# Broker and result backend from environment
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/2")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/3")

celery_app = Celery(
    "manor",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)

# Configuration
celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Time limits (seconds)
    task_soft_time_limit=300,
    task_time_limit=600,
    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # ``task_acks_late`` only means "ack when the task finishes" — it is the
    # BROKER that decides how long it waits for that ack before handing the
    # message to somebody else. Redis has no server-side ack, so kombu emulates
    # one with ``visibility_timeout``, whose default is ONE HOUR: without this
    # setting every step still running at the 60-minute mark was re-delivered
    # to a second worker while the first was still executing it. The value is
    # derived from the step-deadline ceiling so the ordering
    # "visibility timeout > celery hard limit > step deadline" cannot silently
    # invert — see packages/core/services/step_deadline.py.
    broker_transport_options={
        "visibility_timeout": CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
    },
    # The result backend speaks the same Redis dialect and applies the same
    # default; keep them aligned so a long task's bookkeeping isn't reaped early.
    result_backend_transport_options={
        "visibility_timeout": CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
    },
    # Worker
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    # Queues — work is separated from the control plane so concurrent long
    # steps can no longer starve internal_worker_tick / cleanup_expired_leases /
    # scheduler.tick. Every task's queue comes from the explicit registry in
    # packages/core/queues.py (a dotted-name lookup, never a prefix match); the
    # default queue keeps its historical name so a worker started without -Q
    # still consumes the whole control plane.
    # ``routing_key`` is pinned to the queue name on purpose: the Redis
    # transport resolves a direct-exchange message to the list named by the
    # routing key, and celery's default routing key is "celery" — leaving it
    # implicit would silently deliver every work task back into the control
    # plane's list.
    task_queues=tuple(
        Queue(queue.value, routing_key=queue.value) for queue in CeleryQueue
    ),
    task_default_queue=CeleryQueue.CONTROL.value,
    task_routes=(route_task,),
    # Timezone
    timezone="UTC",
    enable_utc=True,
)

# Register task modules explicitly (files are named *_tasks.py, not tasks.py,
# so autodiscover_tasks won't find them).
celery_app.conf.include = [
    "packages.core.tasks.ai_tasks",
    "packages.core.tasks.embedding_tasks",
    "packages.core.tasks.monitor_tasks",
    "packages.core.tasks.scheduler_tasks",
    "packages.core.tasks.oauth_refresh",
    "packages.core.tasks.channel_tasks",
    "packages.core.tasks.metrics_tasks",
    "packages.core.tasks.ops_tasks",
    "packages.core.tasks.deletion_tasks",
    "packages.core.tasks.maintenance_tasks",
    "packages.core.tasks.media_tasks",
]

# Beat schedule — periodic jobs
celery_app.conf.beat_schedule = {
    "scheduler-tick": {
        "task": "scheduler.tick",
        "schedule": 60.0,  # every 60 seconds
    },
    "daily-health-briefing": {
        "task": "monitor.daily_health_briefing",
        "schedule": crontab(hour=8, minute=0),
    },
    "media-reference-cleanup": {
        # Provider input snapshots (uploads/media-references/<job id>/), not
        # deliverables. Daily sweep, 30-day retention by default.
        "task": "media.cleanup_media_references",
        "schedule": crontab(hour=4, minute=30),
    },
    "entity-chat-extraction-sweep": {
        # Entity-level (workspace-less) chats have no per-workspace
        # extraction job; this sweep gives the main assistant chat the
        # same memory pipeline. Bookmark-guarded, so quiet entities cost
        # one indexed query per pass.
        "task": "memory.entity_chat_extraction_sweep",
        "schedule": 6 * 3600.0,  # every 6h, matching workspace extraction
    },
    "heartbeat-check": {
        "task": "monitor.heartbeat_check",
        "schedule": 1800.0,  # every 30 minutes
    },
    "hitl-waiting-reminder": {
        # Dedicated HITL nudge: daily briefing counts waiting steps,
        # this one actively reminds task owners while a plan is paused.
        "task": "monitor.hitl_waiting_reminder",
        "schedule": 900.0,  # every 15 minutes
    },
    "sla-breach-check": {
        # Sweeps open tasks against their SLA policies, flips
        # ``sla_breached`` and fires the first escalation rule. Cheap
        # when no policies exist (joins filter out empties).
        "task": "monitor.sla_breach_check",
        "schedule": 600.0,  # every 10 minutes
    },
    "oauth-refresh": {
        "task": "oauth.refresh_tick",
        "schedule": 60.0,  # every minute — scans for tokens expiring <5 min
    },
    "notification-dispatch-due": {
        # Per-minute sweep for ``notify(deliver_at=…)`` scheduled rows.
        # Cheap when the queue is empty (indexed scan, exits in a few ms).
        "task": "notification.dispatch_due",
        "schedule": 60.0,
    },
    "integration-health-tick": {
        "task": "integrations.health_tick",
        "schedule": crontab(hour=4, minute=15),  # once daily at 04:15 UTC
    },
    "refresh-plans-cache": {
        # Every 5 min — keeps each worker's in-process PLANS cache fresh
        # so admin-side edits propagate across processes within 5 min
        # without needing pub/sub. Cheap (single SELECT).
        "task": "billing.refresh_plans_cache",
        "schedule": 300.0,
    },
    "sync-openrouter-pricing": {
        # Keep runtime pricing aligned with OpenRouter changes.
        # Override cadence with OPENROUTER_PRICING_SYNC_SECONDS.
        "task": "maintenance.sync_openrouter_pricing",
        "schedule": float(os.getenv("OPENROUTER_PRICING_SYNC_SECONDS", "43200")),
    },
    # M3 Worker / Dispatcher layer
    "internal-worker-tick": {
        # Heartbeat for in-process internal workers — checks out leases
        # and fans out per-lease execute_lease tasks. 5s trades a small
        # amount of plan-step latency for ~60% fewer ticks/day vs the
        # original 2s default; raise back to 2.0 if interactive plans
        # feel laggy.
        "task": "packages.core.tasks.ai_tasks.internal_worker_tick",
        "schedule": 5.0,
    },
    "cleanup-expired-leases": {
        "task": "packages.core.tasks.ai_tasks.cleanup_expired_leases",
        "schedule": 30.0,  # every 30s — leases default to 5min TTL
    },
    "workspace-readiness-check": {
        # Lightweight DB-only check: did any workspace become unblocked?
        # If so, triggers a Strategist review immediately.
        "task": "monitor.workspace_readiness_check",
        "schedule": 600.0,  # every 10 minutes
    },
    "experiment-guardrail-tick": {
        # M13: stop running experiments on consecutive cohort failures /
        # max_runs / expiry, remove their overlay, auto-evaluate. Pure DB
        # arithmetic — cheap no-op when no experiment is running.
        "task": "experiments.guardrail_tick",
        "schedule": 300.0,  # every 5 minutes
    },
    "budget-monthly-reset": {
        # Daily — first-of-month catches the calendar rollover, runs
        # on other days are cheap no-ops. Pairs with billing-cycle-check
        # above (different scope: per-workspace caps vs entity AI budgets).
        "task": "packages.core.tasks.ai_tasks.budget_monthly_reset",
        "schedule": crontab(hour=0, minute=5),
    },
    "metrics-daily-rollup": {
        # Runs 15 minutes after midnight UTC so the prior day's data is
        # fully written (no in-flight requests still landing rows dated
        # "yesterday" at the stroke of midnight).
        "task": "metrics.daily_rollup",
        "schedule": crontab(hour=0, minute=15),
    },
    "metrics-http-flush": {
        # Snapshot-sync the Redis HTTP traffic counters into
        # http_request_hourly (reads the last few hour buckets;
        # absolute-set upsert, so re-runs converge instead of
        # double-counting).
        "task": "metrics.http_flush",
        "schedule": 300.0,  # every 5 minutes
    },
    "embedding-sweep-pending": {
        # Picks up documents stuck at 'pending' (task dispatch lost).
        "task": "embeddings.sweep_pending",
        "schedule": 300.0,  # every 5 minutes
    },
    # Ops monitoring — host + Docker container snapshot + alerting.
    # Collector writes to Redis (ops:snapshot, TTL 2 min). Alerter
    # reads it and runs the rule engine. Snapshot drives the
    # /admin/ops dashboard too — endpoint reads Redis instead of
    # blocking 1-2s on Docker stats per request.
    "ops-collect-snapshot": {
        "task": "ops.collect_snapshot",
        "schedule": 30.0,
    },
    "ops-alert-tick": {
        "task": "ops.alert_tick",
        "schedule": 60.0,
    },
    "ops-log-scan": {
        # Per-container error rate detection. Tails ~1m of docker logs
        # per running container (~10 containers × small ms each). Updates
        # rolling 1h baseline + publishes spike map for alert_tick to
        # consume. Cheap when nothing is spiking.
        "task": "ops.log_scan",
        "schedule": 60.0,
    },
    "ops-send-digest": {
        # Daily 08:00 UTC — drains the suppressed-warnings queue
        # into one summary email. Cheap (no-op when queue empty).
        "task": "ops.send_digest",
        "schedule": crontab(hour=8, minute=0),
    },
    "ops-purge-soft-deleted-workspaces": {
        # Daily 02:00 UTC — hard-deletes workspaces past the soft-delete
        # grace window. Override grace via WORKSPACE_PURGE_GRACE_DAYS env.
        "task": "ops.purge_soft_deleted_workspaces",
        "schedule": crontab(hour=2, minute=0),
    },
    "ops-purge-soft-deleted-users": {
        # Daily 02:15 UTC — hard-deletes user accounts past the
        # soft-delete grace window (and cascade-deletes the entity if
        # the user was the entity's sole admin).
        "task": "ops.purge_soft_deleted_users",
        "schedule": crontab(hour=2, minute=15),
    },
    "maintenance-cleanup-chat-uploads": {
        # Daily 03:05 UTC - removes expired hidden chat attachments from
        # uploads/chat. Knowledge files and generated artifacts are excluded.
        "task": "maintenance.cleanup_chat_uploads",
        "schedule": crontab(hour=3, minute=5),
    },
    "maintenance-repair-missing-document-files": {
        # Periodic DB <-> filesystem consistency scan. Missing generated media
        # is restored from provider source URLs when available. Missing files
        # are recorded in file_integrity; vector_status is not changed unless
        # DOCUMENT_FILE_REPAIR_MARK_FAILED=true.
        "task": "maintenance.repair_missing_document_files",
        "schedule": float(os.getenv("DOCUMENT_FILE_REPAIR_SECONDS", "1800")),
    },
    "media-recover-stale-jobs": {
        # Every minute - resumes provider polling for video jobs stranded by
        # API reloads/worker exits and eventually marks them completed/failed.
        "task": "media.recover_stale_jobs",
        "schedule": 60.0,
    },
}


# ── OTEL tracing — opt-in (OTEL_ENABLED=true) ─────────────────────────
# Initialised per-process so each forked worker has its own tracer +
# OTLP exporter. shutdown_tracing flushes pending spans on exit.

@worker_process_init.connect
def _init_otel(**_kwargs: object) -> None:
    try:
        from packages.core.observability import init_tracing
        init_tracing(service_name="manor-celery-worker")
    except Exception:
        # Never block worker boot on tracing — log via celery's own
        # logger handle on the next step instead.
        pass

    try:
        from packages.core.services.openrouter_pricing_sync import sync_openrouter_pricing_cache
        asyncio.run(sync_openrouter_pricing_cache(timeout_s=10.0))
    except Exception:
        pass



@worker_process_shutdown.connect
def _shutdown_otel(**_kwargs: object) -> None:
    try:
        from packages.core.observability import shutdown_tracing
        shutdown_tracing()
    except Exception:
        pass
