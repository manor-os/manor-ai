"""Bring the public runtime schema forward after the group-chat migrations.

Revision ID: 20260927_01
Revises: 20260926_17

This public-only migration incorporates the Task coordinator, Plan submission,
file-commit journal, Stat assessment, and recovery-index changes from the
private migration branches. Published earlier OSS revisions stay immutable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260927_01"
down_revision = "20260926_17"
branch_labels = None
depends_on = None

_CONCURRENT_INDEXES = (
    ("ix_plans_wake_scan", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_plans_wake_scan ON execution_plans (status, updated_at, id)"),
    ("ix_plans_wake_runnable", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_plans_wake_runnable ON execution_plans (COALESCE(updated_at, created_at), id) WHERE status IN ('draft', 'pending', 'running')"),
    ("ix_leases_plan_status", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leases_plan_status ON work_leases (plan_id, status)"),
    ("ix_runtime_outbox_due_fair", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_outbox_due_fair ON runtime_outbox_events (available_at, created_at, id) WHERE delivered_at IS NULL"),
    ("ix_runtime_outbox_plan_due", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_outbox_plan_due ON runtime_outbox_events (available_at, created_at, id) WHERE delivered_at IS NULL AND event_type = 'runtime.dispatch_plan_cycle'"),
    ("ix_runtime_outbox_non_plan_due", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_outbox_non_plan_due ON runtime_outbox_events (available_at, created_at, id) WHERE delivered_at IS NULL AND event_type <> 'runtime.dispatch_plan_cycle'"),
    ("ix_runtime_outbox_aggregate", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_outbox_aggregate ON runtime_outbox_events (aggregate_id)"),
)


def _index_is_invalid(name: str) -> bool:
    return op.get_bind().execute(sa.text("""
        SELECT NOT i.indisvalid
          FROM pg_catalog.pg_index AS i
          JOIN pg_catalog.pg_class AS c ON c.oid = i.indexrelid
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
         WHERE c.relname = :name AND n.nspname = current_schema()
    """), {"name": name}).scalar_one_or_none() is True


def upgrade() -> None:
    # Build only indexes on pre-existing tables before the schema transaction.
    # If a concurrent build is interrupted, retry repairs the invalid index;
    # the new tables and Alembic stamp have not been partially committed.
    with op.get_context().autocommit_block():
        for name, sql in _CONCURRENT_INDEXES:
            if _index_is_invalid(name):
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(sa.text(sql))

    op.create_table(
        "task_coordinator_runs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("task_id", sa.String(26), nullable=False),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=False),
        sa.Column("root_subscription_id", sa.String(26), nullable=False),
        sa.Column("root_agent_id", sa.String(26), nullable=False),
        sa.Column("selection_fingerprint", sa.String(64), nullable=False),
        sa.Column("lifecycle_generation", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False, server_default="ready"),
        sa.Column("current_plan_id", sa.String(26)),
        sa.Column("checkpoint", postgresql.JSONB()),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wake_generation", sa.String(26)),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_generation", sa.String(26)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_cleanup_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state_version >= 0", name="ck_task_coordinator_runs_version"),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claim_generation IS NULL AND claim_expires_at IS NULL) "
            "OR (claim_token IS NOT NULL AND claim_generation IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_task_coordinator_runs_claim",
        ),
        sa.UniqueConstraint("task_id", name="uq_task_coordinator_runs_task"),
    )
    op.create_index("ix_task_coordinator_runs_workspace_phase", "task_coordinator_runs", ["workspace_id", "phase"])
    op.create_index("ix_task_coordinator_runs_recovery", "task_coordinator_runs", ["phase", "wake_generation", "updated_at"])
    op.create_index("ix_task_coordinator_runs_terminal_cleanup", "task_coordinator_runs", ["phase", "terminal_cleanup_at", "updated_at"])

    op.create_table(
        "task_plan_submissions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("task_id", sa.String(26), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=False),
        sa.Column("lifecycle_generation", sa.String(64), nullable=False),
        sa.Column("request_kind", sa.String(16), nullable=False),
        sa.Column("requested_by_user_id", sa.String(26), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("plan_dag", postgresql.JSONB()),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_submission_id", sa.String(26)),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("plan_id", sa.String(26)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(request_kind = 'manual' AND plan_dag IS NOT NULL) OR "
            "(request_kind = 'planner' AND plan_dag IS NULL)",
            name="ck_task_plan_submission_kind_dag",
        ),
        sa.UniqueConstraint("task_id", "lifecycle_generation", name="uq_task_plan_submission_generation"),
    )

    op.create_table(
        "runtime_file_commits",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("transaction_id", sa.String(26), nullable=False),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("rel_path", sa.String(2048), nullable=False),
        sa.Column("path_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("written_version", postgresql.JSONB()),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("document_id", sa.String(26)),
        sa.Column("owner", postgresql.JSONB(), nullable=False),
        sa.Column("effect_id", sa.String(64)),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_runtime_file_commit_transaction", "runtime_file_commits", ["transaction_id"])
    op.create_index("ix_runtime_file_commit_recovery", "runtime_file_commits", ["entity_id", "created_at"], postgresql_where=sa.text("cleanup_pending = true"))
    op.create_index("ix_runtime_file_commit_effect", "runtime_file_commits", ["entity_id", "effect_id"], postgresql_where=sa.text("effect_id IS NOT NULL"))
    op.create_index("uq_runtime_file_commit_unsettled_path", "runtime_file_commits", ["entity_id", "path_sha256"], unique=True, postgresql_where=sa.text("state IN ('prepared', 'reconciliation_required')"))

    op.alter_column("workspace_stat_observations", "value", existing_type=sa.Numeric(24, 6), nullable=True)
    op.add_column("workspace_stats", sa.Column("last_assessment", postgresql.JSONB()))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _sql in reversed(_CONCURRENT_INDEXES):
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM workspace_stat_observations WHERE value IS NULL)"
    )).scalar():
        raise RuntimeError("Cannot discard unknown Stat assessments during downgrade")
    op.drop_column("workspace_stats", "last_assessment")
    op.alter_column("workspace_stat_observations", "value", existing_type=sa.Numeric(24, 6), nullable=False)
    op.drop_table("runtime_file_commits")
    op.drop_table("task_plan_submissions")
    op.drop_table("task_coordinator_runs")
