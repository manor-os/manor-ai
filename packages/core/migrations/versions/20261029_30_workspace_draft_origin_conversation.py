"""Workspace operation drafts record the conversation that drafted them.

Revision ID: 20261029_30
Revises: 20261028_20
"""

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists, table_exists


revision = "20261029_30"
down_revision = "20261029_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("workspace_operation_drafts"):
        add_column_if_not_exists(
            "workspace_operation_drafts",
            sa.Column("origin_conversation_id", sa.String(26), nullable=True),
        )


def downgrade() -> None:
    if table_exists("workspace_operation_drafts") and column_exists("workspace_operation_drafts", "origin_conversation_id"):
        op.drop_column("workspace_operation_drafts", "origin_conversation_id")
