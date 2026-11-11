"""Content-free failure categories on the per-Workspace daily health rows.

Revision ID: 20261104_20
Revises: 20261104_01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists, table_exists

revision = "20261104_20"
down_revision = "20261104_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("metrics_workspace_daily"):
        add_column_if_not_exists(
            "metrics_workspace_daily",
            sa.Column("error_codes", JSONB, nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    if table_exists("metrics_workspace_daily") and column_exists("metrics_workspace_daily", "error_codes"):
        op.drop_column("metrics_workspace_daily", "error_codes")
