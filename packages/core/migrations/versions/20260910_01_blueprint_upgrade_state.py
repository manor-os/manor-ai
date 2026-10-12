"""Persist reviewed in-place Blueprint configuration baselines.

Revision ID: 20260910_01
Revises: 20260909_02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import table_exists

revision = "20260910_01"
down_revision = "20260909_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("workspace_blueprint_upgrade_states"):
        return
    op.create_table(
        "workspace_blueprint_upgrade_states",
        sa.Column("workspace_id", sa.String(26), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("baseline", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("restore_point", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workspace_blueprint_upgrade_states_entity_id",
        "workspace_blueprint_upgrade_states", ["entity_id"],
    )


def downgrade() -> None:
    if table_exists("workspace_blueprint_upgrade_states"):
        op.drop_table("workspace_blueprint_upgrade_states")
