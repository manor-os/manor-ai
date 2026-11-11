"""Attention items remember the stage people were last told about.

Revision ID: 20261101_91
Revises: 20261101_80
"""
import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import column_exists

revision = "20261101_91"
down_revision = "20261101_80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not column_exists("attention_items", "told_stage"):
        op.add_column("attention_items", sa.Column("told_stage", sa.Integer(), nullable=True))
    if not column_exists("attention_items", "told_at"):
        op.add_column("attention_items", sa.Column("told_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("attention_items", "told_at")
    op.drop_column("attention_items", "told_stage")
