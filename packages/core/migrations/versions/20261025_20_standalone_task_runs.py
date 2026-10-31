"""Run Tasks assigned directly to an Agent on the Task agent loop.

A Task outside any Workspace (assigned to an Agent or the master agent) now
owns a ``task_coordinator_runs`` row like a Workspace Task: its
``workspace_id`` is NULL, and ``root_subscription_id`` is NULL unless the Task
names the Agent's personal hire. Its ``task_run_journal`` rows carry the same
NULL ``workspace_id``.

The retired TaskRunner kept no durable transcript, so its in-flight work
cannot be continued by the loop. It is closed here once, in SQL:

* Tasks outside any Workspace, assigned to an Agent, with no run row, that
  the retired runner was executing or parking (``in_progress``,
  ``waiting_on_customer``, or ``pending`` with a queued approval resume)
  become ``blocked`` with a log line. Retrying the Task starts it on the
  agent loop; files it produced stay.
* Their pending cards (questions and approvals) are expired and closed,
  their queued approval-resume deliveries closed, and the retired runner's
  approval marker (``details._runtime_tool_hitl_v1``) finished as failed.

Tasks that never started (``created``/``pending``/``scheduled``) are left
alone: their next start runs on the agent loop.

Idempotent: a second run finds nothing to close (the closed Tasks are
blocked). Downgrade restores NOT NULL only when no row needs NULL; closed
work is not resurrected.

Revision ID: 20261025_20
Revises: 20261025_10
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import table_exists

revision = "20261025_20"
down_revision = "20261025_10"
branch_labels = None
depends_on = None

RETIRED_REASON = "task_runner_retired"
TASK_BLOCK_CODE = "runtime_migrated"
TASK_BLOCK_MESSAGE = (
    "This Task was running on the retired Agent task runner and was stopped when "
    "Manor moved every Task to the agent loop. Files it produced are kept. "
    "Retry the Task to continue it on the current runtime."
)
_RESUME_DEDUPE_PREFIX = "packages.core.tasks.ai_tasks.run_agent_task:approval:%"
_MASTER_AGENT_TYPE = "manor_agent"


_NULLABLE_COLUMNS = (
    ("task_coordinator_runs", "workspace_id"),
    ("task_coordinator_runs", "root_subscription_id"),
    ("task_run_journal", "workspace_id"),
)


def upgrade() -> None:
    relax_nullable_columns()
    _close_out_in_flight_work()


def relax_nullable_columns() -> None:
    for table, column in _NULLABLE_COLUMNS:
        if table_exists(table):
            op.alter_column(table, column, existing_type=sa.String(26), nullable=True)


def _close_out_in_flight_work() -> None:
    if not all(table_exists(name) for name in ("tasks", "task_coordinator_runs", "task_logs")):
        return
    bind = op.get_bind()
    task_ids = bind.execute(sa.text(
        "SELECT t.id FROM tasks t "
        "WHERE t.workspace_id IS NULL "
        "AND COALESCE(t.task_type, 'general') NOT IN ('interactive', 'approval') "
        "AND (t.agent_id IS NOT NULL OR t.agent_type = :master_type) "
        "AND (t.status IN ('in_progress', 'waiting_on_customer') "
        "     OR (t.status = 'pending' "
        "         AND t.details->'_runtime_tool_hitl_v1'->>'state' IN ('resume_queued', 'resuming'))) "
        "AND NOT EXISTS (SELECT 1 FROM task_coordinator_runs r WHERE r.task_id = t.id)"
    ), {"master_type": _MASTER_AGENT_TYPE}).scalars().all()
    if not task_ids:
        return
    now = datetime.now(timezone.utc)

    def _in(sql: str, *names: str) -> sa.TextClause:
        return sa.text(sql).bindparams(*(sa.bindparam(name, expanding=True) for name in names))

    if table_exists("hitl_requests"):
        expired = bind.execute(_in(
            "UPDATE hitl_requests SET status = 'expired', resolved_reason = :reason, updated_at = :now "
            "WHERE status = 'pending' AND origin_task_id IN :tasks RETURNING id",
            "tasks",
        ), {"tasks": list(task_ids), "reason": RETIRED_REASON, "now": now}).scalars().all()
        if expired and table_exists("messages"):
            # Their cards close too: question cards name the request,
            # approval cards carry it in their surface event id.
            bind.execute(_in(
                "UPDATE messages SET resolved_at = :now, resolution = CAST(:resolution AS jsonb) "
                "WHERE resolved_at IS NULL AND (pending_action->>'approval_request_id' IN :requests "
                "OR response_surface_event_id IN :events)",
                "requests", "events",
            ), {
                "requests": list(expired), "events": [f"task-runtime-hitl:{rid}" for rid in expired],
                "now": now, "resolution": json.dumps({"choice": "expired", "note": RETIRED_REASON}),
            })
    if table_exists("runtime_outbox_events"):
        bind.execute(_in(
            "UPDATE runtime_outbox_events SET delivered_at = :now, last_error = :reason "
            "WHERE delivered_at IS NULL AND aggregate_id IN :tasks AND dedupe_key LIKE :prefix",
            "tasks",
        ), {
            "tasks": list(task_ids), "now": now,
            "reason": f"permanent:{RETIRED_REASON}", "prefix": _RESUME_DEDUPE_PREFIX,
        })
    blocked = bind.execute(_in(
        "UPDATE tasks SET status = 'blocked', status_changed_at = :now, updated_at = :now, "
        # The retired runner's approval marker is finished, as a failed
        # approval leaves it: no recovery may treat the Task as parked on it.
        "details = CASE WHEN details ? '_runtime_tool_hitl_v1' THEN jsonb_set("
        "  details, '{_runtime_tool_hitl_v1}', "
        "  (details->'_runtime_tool_hitl_v1') || CAST(:marker AS jsonb)) ELSE details END "
        "WHERE id IN :tasks AND status IN ('in_progress', 'waiting_on_customer', 'pending') "
        "RETURNING id",
        "tasks",
    ), {
        "tasks": list(task_ids), "now": now,
        "marker": json.dumps({"state": "finished", "result_status": "failed", "failure_reason": RETIRED_REASON}),
    }).scalars().all()
    if blocked:
        from packages.core.models.base import generate_ulid

        metadata = json.dumps({"error": TASK_BLOCK_CODE, "reason": RETIRED_REASON})
        bind.execute(sa.text(
            "INSERT INTO task_logs (id, task_id, log_type, content, metadata, created_by, created_at) "
            "VALUES (:id, :task_id, 'ai_execution_failed', :content, CAST(:metadata AS jsonb), 'system', :now)"
        ), [
            {"id": generate_ulid(), "task_id": task_id, "content": TASK_BLOCK_MESSAGE, "metadata": metadata, "now": now}
            for task_id in blocked
        ])


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in _NULLABLE_COLUMNS:
        if not table_exists(table):
            continue
        needs_null = bind.execute(sa.text(
            f"SELECT 1 FROM {table} WHERE {column} IS NULL LIMIT 1"
        )).first()
        if needs_null is None:
            op.alter_column(table, column, existing_type=sa.String(26), nullable=False)
