"""Workspace health metrics: per-Workspace daily rows, current snapshots,
and who cancelled a Task or Workflow run.

Revision ID: 20261028_20
Revises: 20261028_10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists, table_exists


revision = "20261028_20"
down_revision = "20261028_10"
branch_labels = None
depends_on = None


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer, nullable=False, server_default="0")


def upgrade() -> None:
    for table in ("tasks", "workflow_runs"):
        if table_exists(table):
            add_column_if_not_exists(table, sa.Column("cancel_source", sa.String(16), nullable=True))

    if not table_exists("metrics_workspace_daily"):
        op.create_table(
            "metrics_workspace_daily",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("day", sa.Date, nullable=False),
            sa.Column("workspace_id", sa.String(26), nullable=False),
            sa.Column("surface", sa.String(16), nullable=False),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("blueprint_key", sa.String(200)),
            sa.Column("blueprint_version", sa.String(40)),
            sa.Column("mode", sa.String(12), nullable=False),
            _count("succeeded"), _count("failed"), _count("cancelled"), _count("retried"),
            sa.Column("duration_sum_s", sa.BigInteger, nullable=False, server_default="0"),
            _count("duration_count"),
            sa.Column("duration_buckets", JSONB, nullable=False, server_default="{}"),
            sa.Column("top_errors", JSONB, nullable=False, server_default="[]"),
            _count("user_cancelled"), _count("reviews_accepted"), _count("reviews_rejected"),
            _count("reviews_ignored"), _count("feedback_up"), _count("feedback_down"),
            _count("proposals_accepted"), _count("proposals_rejected"),
            sa.Column("credits", sa.BigInteger, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("day", "workspace_id", "surface", name="uq_metrics_workspace_daily"),
        )
        op.create_index("ix_metrics_workspace_daily_ws_day", "metrics_workspace_daily", ["workspace_id", "day"])
        op.create_index("ix_metrics_workspace_daily_blueprint_day", "metrics_workspace_daily", ["blueprint_key", "day"])

    if not table_exists("workspace_health_snapshots"):
        op.create_table(
            "workspace_health_snapshots",
            sa.Column("workspace_id", sa.String(26), primary_key=True),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("blueprint_key", sa.String(200)),
            sa.Column("blueprint_version", sa.String(40)),
            sa.Column("mode", sa.String(12), nullable=False),
            sa.Column("status", sa.String(12), nullable=False),
            sa.Column("status_since", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reasons", JSONB, nullable=False, server_default="[]"),
            sa.Column("surfaces", JSONB, nullable=False, server_default="{}"),
            sa.Column("quality", JSONB, nullable=False, server_default="{}"),
            sa.Column("readiness_gaps", JSONB, nullable=False, server_default="[]"),
            _count("missed_schedules"), _count("pending_approvals_over_24h"),
            sa.Column("service_coverage", sa.Numeric(5, 4)),
            sa.Column("live_since", sa.DateTime(timezone=True)),
            sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_workspace_health_snapshots_status", "workspace_health_snapshots", ["status"])
        op.create_index(
            "ix_workspace_health_snapshots_blueprint", "workspace_health_snapshots",
            ["blueprint_key", "blueprint_version"],
        )


def downgrade() -> None:
    for table in ("workspace_health_snapshots", "metrics_workspace_daily"):
        if table_exists(table):
            op.drop_table(table)
    for table in ("workflow_runs", "tasks"):
        if table_exists(table) and column_exists(table, "cancel_source"):
            op.drop_column(table, "cancel_source")
