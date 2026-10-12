"""Persist Apify collection quotes, execution identity and credit settlement.

Revision ID: 20260917_01
Revises: deployment-specific predecessors
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260917_01"
down_revision = "20260911_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apify_collections",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26)),
        sa.Column("agent_id", sa.String(26)),
        sa.Column("conversation_id", sa.String(26)),
        sa.Column("credential_scope", sa.String(20), nullable=False),
        sa.Column("account_id", sa.String(100)),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("pricing", postgresql.JSONB(), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(18, 9), nullable=False),
        sa.Column("max_credits", sa.Integer(), nullable=False),
        sa.Column("quote_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("billing_status", sa.String(30), nullable=False),
        sa.Column("provider_run_id", sa.String(100)),
        sa.Column("provider_status", sa.String(30)),
        sa.Column("dataset_id", sa.String(100)),
        sa.Column("result_snapshot", postgresql.JSONB()),
        sa.Column("provider_cost_usd", sa.Numeric(18, 9)),
        sa.Column("charged_credits", sa.Integer()),
        sa.Column("uncovered_cost_usd", sa.Numeric(18, 9)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(26)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_apify_collections_owner", "apify_collections", ["entity_id", "user_id", "workspace_id"])
    op.create_index("ix_apify_collections_pending", "apify_collections", ["billing_status", "next_check_at"])
    op.create_index("ux_apify_collections_provider_run", "apify_collections", ["provider_run_id"], unique=True)


def downgrade() -> None:
    op.drop_table("apify_collections")
