"""Add private resident Webchat bindings; never backfill anonymous authority.

Revision ID: 20260917_05
Revises: 20260917_01
"""
import sqlalchemy as sa
from alembic import op
from packages.core.migrations.helpers import table_exists

revision = "20260917_05"
down_revision = "20260917_01"
branch_labels = None
depends_on = None


def upgrade():
    if table_exists("webchat_resident_bindings"):
        return
    op.create_table("webchat_resident_bindings",
        sa.Column("id", sa.String(26), primary_key=True),
        *[sa.Column(name, sa.String(26), nullable=False) for name in (
            "entity_id", "workspace_id", "channel_binding_id", "channel_config_id", "channel_contact_id", "conversation_id", "target_account_id")],
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("manor_user_id", sa.String(26)),
        sa.Column("target_version", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("pms_entity_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger()),
        sa.Column("subject", sa.String(255)),
        sa.Column("browser_proof_hash", sa.String(64), nullable=False),
        sa.Column("credential_ref", sa.Text()),
        sa.Column("credential_scheme", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state_hash", sa.String(64)),
        sa.Column("state_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for name, column, unique in (("uq_webchat_resident_conversation", "conversation_id", True),
                                 ("uq_webchat_resident_state", "state_hash", True),
                                 ("ix_webchat_resident_workspace", "workspace_id", False),
                                 ("ix_webchat_resident_contact", "channel_contact_id", True)):
        op.create_index(name, "webchat_resident_bindings", [column], unique=unique)


def downgrade():
    if table_exists("webchat_resident_bindings"):
        op.drop_table("webchat_resident_bindings")
