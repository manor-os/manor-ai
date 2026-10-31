"""Retire the Plan/Step Task engine: close out its in-flight work once.

Every Task now runs as one agent loop. Work still held by the retired engine
is closed here, in SQL, so no runtime code has to know the old engine:

* Task-owned Plans that are not terminal are cancelled; their live leases are
  revoked (claim and credentials cleared) and open Steps cancelled. Worker-job
  Plans (CLI, remote coding, local worker) have no Task and are untouched.
* ``plan_dag`` coordinator runs that are not terminal become ``failed``, with
  their claim and wake cleared and their pending wake closed.
* Pending cards those runs and Plans own are expired.
* Their Tasks that were still in progress or waiting on the customer become
  ``blocked`` with a log line. Completed Steps, files and receipts stay;
  retrying the Task starts it on the agent loop (the run's engine is switched
  on retry).
* ``task_coordinator_runs.engine`` defaults to ``agent_loop``.
* ``task_plan_submissions`` (manually authored Plans awaiting the retired
  planner) is dropped.

Idempotent: a second run finds nothing to close. Downgrade is a no-op; the
cancelled work is not resurrected.

Revision ID: 20261025_10
Revises: 20261025_03
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import table_exists

revision = "20261025_10"
down_revision = "20261025_03"
branch_labels = None
depends_on = None

RETIRED_REASON = "plan_dag_retired"
TASK_BLOCK_CODE = "runtime_migrated"
TASK_BLOCK_MESSAGE = (
    "This Task was running on the retired Plan/Step engine and was stopped when "
    "Manor moved every Task to the agent loop. Completed steps and files are kept. "
    "Retry the Task to continue it on the current runtime."
)
_WORKER_JOB_SOURCES: tuple[str, ...] = ("local_worker", "cli_worker")
_OPEN_PLAN = ("draft", "pending", "pending_approval", "running", "paused", "needs_attention")
_OPEN_STEP = ("pending", "running", "waiting_human", "paused")
_LIVE_LEASE = ("active", "needs_human", "suspended")
_OPEN_RUN_PHASES = ("ready", "running", "waiting_plan", "waiting_child", "waiting_human", "waiting_timer", "waiting_job")
_BLOCKABLE_TASK = ("in_progress", "waiting_on_customer")


def _ulid() -> str:
    from packages.core.models.base import generate_ulid

    return generate_ulid()


def upgrade() -> None:
    _close_out_in_flight_work()
    if table_exists("task_coordinator_runs"):
        op.alter_column("task_coordinator_runs", "engine", server_default="agent_loop")
    if table_exists("task_plan_submissions"):
        op.drop_table("task_plan_submissions")


def _close_out_in_flight_work() -> None:
    required = ("execution_plans", "execution_steps", "work_leases", "task_coordinator_runs", "tasks")
    if not all(table_exists(name) for name in required):
        return
    bind = op.get_bind()
    plans = bind.execute(sa.text(
        "SELECT id, task_id FROM execution_plans "
        "WHERE task_id IS NOT NULL AND status IN :open "
        "AND COALESCE(dispatcher_state->>'source', '') NOT IN :worker_sources"
    ).bindparams(
        sa.bindparam("open", expanding=True), sa.bindparam("worker_sources", expanding=True),
    ), {"open": list(_OPEN_PLAN), "worker_sources": list(_WORKER_JOB_SOURCES)}).all()
    runs = bind.execute(sa.text(
        "SELECT id, task_id FROM task_coordinator_runs "
        "WHERE engine = 'plan_dag' AND phase IN :open"
    ).bindparams(sa.bindparam("open", expanding=True)), {"open": list(_OPEN_RUN_PHASES)}).all()
    plan_ids = [row.id for row in plans]
    run_task_ids = [row.task_id for row in runs]
    task_ids = sorted({row.task_id for row in plans} | set(run_task_ids))
    if not task_ids:
        return
    now = datetime.now(timezone.utc)

    def _in(sql: str, **lists: list[str]) -> sa.TextClause:
        return sa.text(sql).bindparams(*(sa.bindparam(name, expanding=True) for name in lists))

    if plan_ids:
        bind.execute(_in(
            "UPDATE work_leases SET status = 'cancelled', execution_claim_id = NULL, "
            "execution_claimed_at = NULL, credential_leases = '[]'::jsonb, "
            "error = CAST(:error AS jsonb) "
            "WHERE plan_id IN :plans AND status IN :live",
            plans=plan_ids, live=list(_LIVE_LEASE),
        ), {
            "plans": plan_ids, "live": list(_LIVE_LEASE),
            "error": json.dumps({"type": "LeaseRevoked", "message": RETIRED_REASON}),
        })
        bind.execute(_in(
            "UPDATE execution_steps SET step_status = 'cancelled', current_lease_id = NULL, "
            "finished_at = :now WHERE plan_id IN :plans AND step_status IN :open",
            plans=plan_ids, open=list(_OPEN_STEP),
        ), {"plans": plan_ids, "open": list(_OPEN_STEP), "now": now})
        bind.execute(_in(
            "UPDATE execution_plans SET status = 'cancelled', completed_at = :now, "
            "last_error = CAST(:error AS jsonb) WHERE id IN :plans",
            plans=plan_ids,
        ), {"plans": plan_ids, "now": now, "error": json.dumps({"cancelled": RETIRED_REASON})})
        bind.execute(sa.text(
            "UPDATE runtime_outbox_events SET delivered_at = :now, last_error = :reason "
            "WHERE delivered_at IS NULL AND event_type = 'runtime.dispatch_plan_cycle'"
        ), {"now": now, "reason": f"permanent:{RETIRED_REASON}"})
    if run_task_ids:
        outcome = json.dumps({
            "contract": "tool_error", "status": "error", "tool": "task_coordinator",
            "error": TASK_BLOCK_CODE, "message": TASK_BLOCK_MESSAGE,
            "details": {"requires_human": False, "checkpoint_preserved": True},
        })
        bind.execute(_in(
            "UPDATE task_coordinator_runs SET phase = 'failed', claim_token = NULL, "
            "claim_generation = NULL, claim_expires_at = NULL, wake_generation = NULL, "
            "state_version = state_version + 1, "
            "checkpoint = COALESCE(checkpoint, '{}'::jsonb) "
            "|| jsonb_build_object('coordinator_outcome', CAST(:outcome AS jsonb)) "
            "WHERE engine = 'plan_dag' AND task_id IN :tasks AND phase IN :open",
            tasks=run_task_ids, open=list(_OPEN_RUN_PHASES),
        ), {"tasks": run_task_ids, "open": list(_OPEN_RUN_PHASES), "outcome": outcome})
        bind.execute(_in(
            "UPDATE runtime_outbox_events SET delivered_at = :now, last_error = :reason "
            "WHERE delivered_at IS NULL AND event_type = 'runtime.dispatch_task_coordinator' "
            "AND aggregate_id IN :tasks",
            tasks=run_task_ids,
        ), {"tasks": run_task_ids, "now": now, "reason": f"permanent:{RETIRED_REASON}"})
    if table_exists("hitl_requests"):
        bind.execute(_in(
            "UPDATE hitl_requests SET status = 'expired', resolved_reason = :reason, updated_at = :now "
            "WHERE status = 'pending' AND (origin_task_id IN :tasks OR origin_plan_id IN :plans)",
            tasks=task_ids, plans=plan_ids or [""],
        ), {"tasks": task_ids, "plans": plan_ids or [""], "reason": RETIRED_REASON, "now": now})
    blocked = bind.execute(_in(
        "UPDATE tasks SET status = 'blocked', status_changed_at = :now, updated_at = :now "
        "WHERE id IN :tasks AND status IN :blockable RETURNING id",
        tasks=task_ids, blockable=list(_BLOCKABLE_TASK),
    ), {"tasks": task_ids, "blockable": list(_BLOCKABLE_TASK), "now": now}).scalars().all()
    if blocked:
        metadata = json.dumps({"error": TASK_BLOCK_CODE, "reason": RETIRED_REASON})
        bind.execute(sa.text(
            "INSERT INTO task_logs (id, task_id, log_type, content, metadata, created_by, created_at) "
            "VALUES (:id, :task_id, 'ai_execution_failed', :content, CAST(:metadata AS jsonb), 'system', :now)"
        ), [
            {"id": _ulid(), "task_id": task_id, "content": TASK_BLOCK_MESSAGE, "metadata": metadata, "now": now}
            for task_id in blocked
        ])


def downgrade() -> None:
    # Closed-out work is not resurrected; the retired engine no longer exists.
    pass
