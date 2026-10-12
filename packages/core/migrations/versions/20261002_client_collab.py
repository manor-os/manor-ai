"""Customer trials, acceptance and explicit external collaboration.
Revision ID: 20261002_client_collab
Revises: 20261001_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261002_client_collab"
down_revision = "20261001_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "client_workspace_deliveries", sa.Column("phase", sa.String(32), nullable=False, server_default="delivered")
    )
    op.add_column("client_workspace_deliveries", sa.Column("acceptance", JSONB, nullable=False, server_default="{}"))

    def base():
        return [
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("workspace_id", sa.String(26), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        ]

    op.create_table(
        "workspace_collaborations",
        *base(),
        sa.Column("delivery_id", sa.String(26)),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requested_by", sa.String(26)),
        sa.Column("authorized_by", sa.String(26)),
        sa.Column("user_id", sa.String(26)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("share_configuration", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("snapshot", JSONB, nullable=False, server_default="{}"),
        sa.Column("authority_fingerprint", sa.String(64)),
    )
    op.create_index("ix_workspace_collaboration_scope", "workspace_collaborations", ["entity_id", "workspace_id"])
    op.create_table(
        "workspace_collaboration_entries",
        *base(),
        sa.Column("collaboration_id", sa.String(26), nullable=False),
        sa.Column("created_by", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.String(26)),
        sa.Column("reply", sa.String()),
        sa.Column("draft_id", sa.String(26)),
        sa.Column("run_id", sa.String(26)),
    )
    op.create_index(
        "ix_workspace_collaboration_entry_scope",
        "workspace_collaboration_entries",
        ["workspace_id", "collaboration_id", "created_at"],
    )


def downgrade():
    op.drop_table("workspace_collaboration_entries")
    op.drop_table("workspace_collaborations")
    op.drop_column("client_workspace_deliveries", "acceptance")
    op.drop_column("client_workspace_deliveries", "phase")
