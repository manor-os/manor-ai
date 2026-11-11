"""Attention items: situations that may need a person's notice.

Revision ID: 20261101_80
Revises: 20261101_17 (cloud: 20261101_70)
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import table_exists

revision = "20261101_80"
down_revision = "20261101_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("attention_items"):
        return
    op.create_table(
        "attention_items",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("urgency", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB, nullable=False, server_default="{}"),
        sa.Column("assignee_user_id", sa.String(26), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_attention_items_open_fingerprint",
        "attention_items",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index("ix_attention_items_workspace_state", "attention_items", ["workspace_id", "state"])
    op.create_index("ix_attention_items_source_state", "attention_items", ["source", "state"])


def downgrade() -> None:
    if table_exists("attention_items"):
        op.drop_table("attention_items")
