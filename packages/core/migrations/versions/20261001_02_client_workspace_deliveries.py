"""Private customer Workspace delivery and direct owner membership.

Revision ID: 20261001_02
Revises: 20261001_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261001_02"
down_revision = "20261001_01"
branch_labels = None
depends_on = None


def upgrade():
    # User membership must not require creating an employee record.
    op.alter_column("workspace_staff", "staff_id", existing_type=sa.String(26), nullable=True)
    op.create_table(
        "client_contacts",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("client_id", sa.String(26), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(26)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("entity_id", "client_id", "email", name="uq_client_contact_email"),
    )
    op.create_index("ix_client_contacts_entity_id", "client_contacts", ["entity_id"])
    op.create_index("ix_client_contacts_client_id", "client_contacts", ["client_id"])
    op.create_table(
        "client_workspace_deliveries",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("client_id", sa.String(26), nullable=False),
        sa.Column("contact_id", sa.String(26), nullable=False),
        sa.Column("source_workspace_id", sa.String(26), nullable=False),
        sa.Column("created_by", sa.String(26), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("target_entity_id", sa.String(26)),
        sa.Column("target_workspace_id", sa.String(26)),
        sa.Column("accepted_by", sa.String(26)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_client_delivery_client", "client_workspace_deliveries", ["entity_id", "client_id"])


def downgrade():
    op.drop_table("client_workspace_deliveries")
    op.drop_table("client_contacts")
    # Keep nullable staff_id: restoring NOT NULL would strand direct members.
