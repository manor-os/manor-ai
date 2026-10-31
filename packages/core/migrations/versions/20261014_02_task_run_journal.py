"""Add the append-only execution journal of the runs that work a Task.

One row per committed transition of a Task run (round, plan revision, park,
wake, terminal), written in the run row's own CAS transaction. The Task comment
projector (W4c) reads it; nothing reads it yet.

Public: the public Task agent loop writes it, so it follows the public head
20261011_01; the Cloud chain folds it in at the 20261014_03 merge.

Revision ID: 20261014_02
Revises: 20261011_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import table_exists


revision = "20261014_02"
down_revision = "20261011_01"
branch_labels = None
depends_on = None

_KINDS = (
    "round", "plan_revision", "delegate", "park", "wake", "retry",
    "child_settled", "batch_settled", "terminal",
)


def upgrade() -> None:
    if table_exists("task_run_journal"):
        return
    op.create_table(
        "task_run_journal",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("task_id", sa.String(length=26), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("workspace_id", sa.String(length=26), nullable=False),
        sa.Column("run_kind", sa.String(length=8), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("root_run_id", sa.String(length=26), nullable=False),
        sa.Column("parent_run_id", sa.String(length=26), nullable=True),
        sa.Column("parent_tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("agent_id", sa.String(length=26), nullable=True),
        sa.Column("agent_subscription_id", sa.String(length=26), nullable=True),
        sa.Column("depth", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("lifecycle_generation", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projected_message_id", sa.String(length=26), nullable=True),
        sa.CheckConstraint("run_kind IN ('root', 'child')", name="ck_task_run_journal_run_kind"),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in _KINDS) + ")",
            name="ck_task_run_journal_kind",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_task_run_journal_seq"),
        sa.CheckConstraint("depth >= 0", name="ck_task_run_journal_depth"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_kind", "run_id", "seq", name="uq_task_run_journal_run_seq"),
    )
    op.create_index(
        "ix_task_run_journal_task", "task_run_journal", ["task_id", "created_at", "id"],
    )
    op.create_index(
        "ix_task_run_journal_unprojected", "task_run_journal", ["task_id", "created_at", "id"],
        postgresql_where=sa.text("projected_at IS NULL"),
    )
    op.create_index(
        "ix_task_run_journal_root_run", "task_run_journal", ["root_run_id", "seq"],
    )
    op.create_index("ix_task_run_journal_run", "task_run_journal", ["run_id", "seq"])


def downgrade() -> None:
    if not table_exists("task_run_journal"):
        return
    op.drop_index("ix_task_run_journal_run", table_name="task_run_journal")
    op.drop_index("ix_task_run_journal_root_run", table_name="task_run_journal")
    op.drop_index("ix_task_run_journal_unprojected", table_name="task_run_journal")
    op.drop_index("ix_task_run_journal_task", table_name="task_run_journal")
    op.drop_table("task_run_journal")
