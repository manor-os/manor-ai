"""Add the durable delegated child runs of a Task agent loop.

One row per delegated item: a durable run that shares the Task run protocol
(claim, renew, CAS checkpoint, settle, release, outbox wake). Nothing creates
rows yet; the durable delegation producer arrives behind a flag that defaults
off.

Public: it follows the public head 20261014_02; the Cloud chain folds it
in at the 20261014_05 merge.

Revision ID: 20261014_04
Revises: 20261014_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import table_exists


revision = "20261014_04"
down_revision = "20261014_02"
branch_labels = None
depends_on = None

# Frozen copies of models.task_agent_child: TASK_AGENT_CHILD_PHASES (the Task
# run phases without waiting_plan), its terminal phases, and the max depth.
_PHASES = (
    "cancelled", "completed", "failed", "ready", "running",
    "waiting_child", "waiting_human", "waiting_job", "waiting_timer",
)
_TERMINAL_PHASES = ("cancelled", "completed", "failed")
_MAX_DEPTH = 2


def _in(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    if table_exists("task_agent_child_runs"):
        return
    op.create_table(
        "task_agent_child_runs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("task_id", sa.String(length=26), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("workspace_id", sa.String(length=26), nullable=False),
        sa.Column("root_run_id", sa.String(length=26), nullable=False),
        sa.Column("parent_ref", sa.String(length=26), nullable=False),
        sa.Column("parent_child_id", sa.String(length=26), nullable=True),
        sa.Column("parent_tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("item_index", sa.SmallInteger(), nullable=False),
        sa.Column("batch_size", sa.SmallInteger(), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("agent_subscription_id", sa.String(length=26), nullable=False),
        sa.Column("agent_id", sa.String(length=26), nullable=False),
        sa.Column("service_key", sa.String(length=100), nullable=True),
        sa.Column("agent_label", sa.String(length=255), nullable=True),
        sa.Column("previous_child_id", sa.String(length=26), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("answer_max_chars", sa.Integer(), nullable=False),
        sa.Column("continue_interrupted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("phase", sa.String(length=24), server_default="ready", nullable=False),
        sa.Column("outcome_status", sa.String(length=24), nullable=True),
        sa.Column("interrupted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lifecycle_generation", sa.String(length=64), nullable=False),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("wake_generation", sa.String(length=26), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_generation", sa.String(length=26), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "usage", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state_version >= 0", name="ck_task_agent_child_runs_version"),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claim_generation IS NULL AND claim_expires_at IS NULL) "
            "OR (claim_token IS NOT NULL AND claim_generation IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_task_agent_child_runs_claim",
        ),
        sa.CheckConstraint(
            "item_index >= 0 AND item_index < batch_size",
            name="ck_task_agent_child_runs_item",
        ),
        sa.CheckConstraint(
            f"depth >= 1 AND depth <= {_MAX_DEPTH}",
            name="ck_task_agent_child_runs_depth",
        ),
        sa.CheckConstraint(
            "(parent_child_id IS NULL AND depth = 1 AND parent_ref = root_run_id) "
            "OR (parent_child_id IS NOT NULL AND depth > 1 AND parent_child_id = parent_ref)",
            name="ck_task_agent_child_runs_parent",
        ),
        sa.CheckConstraint(
            f"phase IN ({_in(_PHASES)})",
            name="ck_task_agent_child_runs_phase",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_ref", "parent_tool_call_id", "item_index",
            name="uq_task_agent_child_runs_call",
        ),
    )
    op.create_index(
        "uq_task_agent_child_runs_live_agent", "task_agent_child_runs",
        ["task_id", "agent_subscription_id"],
        unique=True,
        postgresql_where=sa.text(f"phase NOT IN ({_in(_TERMINAL_PHASES)})"),
    )
    op.create_index(
        "ix_task_agent_child_runs_task_phase", "task_agent_child_runs", ["task_id", "phase"],
    )
    op.create_index(
        "ix_task_agent_child_runs_recovery", "task_agent_child_runs",
        ["phase", "wake_generation", "updated_at"],
    )
    op.create_index(
        "ix_task_agent_child_runs_agent_history", "task_agent_child_runs",
        ["task_id", "agent_subscription_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_task_agent_child_runs_root", "task_agent_child_runs", ["root_run_id"],
    )


def downgrade() -> None:
    if not table_exists("task_agent_child_runs"):
        return
    op.drop_index("ix_task_agent_child_runs_root", table_name="task_agent_child_runs")
    op.drop_index("ix_task_agent_child_runs_agent_history", table_name="task_agent_child_runs")
    op.drop_index("ix_task_agent_child_runs_recovery", table_name="task_agent_child_runs")
    op.drop_index("ix_task_agent_child_runs_task_phase", table_name="task_agent_child_runs")
    op.drop_index("uq_task_agent_child_runs_live_agent", table_name="task_agent_child_runs")
    op.drop_table("task_agent_child_runs")
