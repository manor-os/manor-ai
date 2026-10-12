"""add durable conversation summary coverage

Revision ID: 20260926_01
Revises: 20260924_03_oss
"""
from __future__ import annotations

import sqlalchemy as sa

from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists, table_exists


revision = "20260926_01"
down_revision = "20260924_03_oss"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not table_exists("conversations"):
        return
    add_column_if_not_exists(
        "conversations",
        sa.Column("summary_covered_at", sa.DateTime(timezone=True), nullable=True),
    )
    add_column_if_not_exists(
        "conversations",
        sa.Column("summary_covered_message_id", sa.String(length=26), nullable=True),
    )
    add_column_if_not_exists(
        "conversations",
        sa.Column("summary_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if not table_exists("conversations"):
        return
    drop_column_if_exists("conversations", "summary_version")
    drop_column_if_exists("conversations", "summary_covered_message_id")
    drop_column_if_exists("conversations", "summary_covered_at")
