"""Separate resident create credentials and explicit-confirmation draft receipts.

Revision ID: 20260917_06
Revises: 20260917_05
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op
from packages.core.migrations.helpers import table_exists

revision = "20260917_06"
down_revision = "20260917_05"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        for name in ("created_at", "updated_at")
    ]


def upgrade():
    if not table_exists("webchat_resident_create_grants"):
        op.create_table(
            "webchat_resident_create_grants",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column(
                "binding_id",
                sa.String(26),
                sa.ForeignKey("webchat_resident_bindings.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("issuer", sa.String(2048), nullable=False),
            sa.Column("credential_ref", sa.Text()),
            sa.Column("credential_scheme", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("state_hash", sa.String(64), unique=True),
            sa.Column("state_expires_at", sa.DateTime(timezone=True)),
            sa.Column("read_generation", sa.Integer(), nullable=False),
            *_timestamps(),
        )
    if not table_exists("webchat_resident_maintenance_drafts"):
        op.create_table(
            "webchat_resident_maintenance_drafts",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column(
                "binding_id",
                sa.String(26),
                sa.ForeignKey("webchat_resident_bindings.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source_call_id", sa.String(255)),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("fields", JSONB(), nullable=False),
            sa.Column("location_label", sa.String(500)),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("submission_key", sa.String(64), nullable=False, unique=True),
            sa.Column("confirmed_payload", JSONB()),
            sa.Column("confirmed_at", sa.DateTime(timezone=True)),
            sa.Column("attempt_id", sa.String(64)),
            sa.Column("attempt_expires_at", sa.DateTime(timezone=True)),
            sa.Column("request", JSONB()),
            sa.Column("error", JSONB()),
            *_timestamps(),
            sa.UniqueConstraint("binding_id", "source_call_id", name="uq_resident_draft_tool_call"),
        )
        op.create_index(
            "ix_webchat_resident_maintenance_drafts_binding_id", "webchat_resident_maintenance_drafts", ["binding_id"]
        )


def downgrade():
    for table in ("webchat_resident_maintenance_drafts", "webchat_resident_create_grants"):
        if table_exists(table):
            op.drop_table(table)
