"""Company B2B CRM and contact invitation lifecycle.

Revision ID: 20261001_03
Revises: 20261001_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20261001_03"
down_revision = "20261001_02"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("phone", sa.String(50)),
        sa.Column("job_title", sa.String(255)),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("invite_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("client_contacts", column)
    op.create_index("ix_client_contacts_deleted_at", "client_contacts", ["deleted_at"])
    op.add_column(
        "client_workspace_deliveries", sa.Column("contact_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "client_workspace_deliveries", sa.Column("client_version", sa.Integer(), nullable=False, server_default="0")
    )

    def shared():
        return [
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("client_id", sa.String(26), nullable=False),
            sa.Column("created_by", sa.String(26), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
        ]

    op.create_table(
        "client_activities",
        *shared(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "client_opportunities",
        *shared(),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=False),
        sa.Column("expected_close", sa.Date()),
        sa.Column("owner_id", sa.String(26)),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("loss_reason", sa.String(1000), nullable=False),
    )
    op.create_table(
        "client_followups",
        *shared(),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.String(26)),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    for table in ("client_activities", "client_opportunities", "client_followups"):
        op.create_index(f"ix_{table}_deleted_at", table, ["deleted_at"])
    op.create_index("ix_client_activity_scope", "client_activities", ["entity_id", "client_id", "occurred_at"])
    op.create_index("ix_client_opportunity_scope", "client_opportunities", ["entity_id", "client_id", "stage"])
    op.create_index("ix_client_followup_due", "client_followups", ["entity_id", "status", "due_at"])


def downgrade():
    for table in ("client_followups", "client_opportunities", "client_activities"):
        op.drop_table(table)
    for name in ("client_version", "contact_version"):
        op.drop_column("client_workspace_deliveries", name)
    op.drop_index("ix_client_contacts_deleted_at", table_name="client_contacts")
    for name in ("deleted_at", "invite_version", "is_primary", "job_title", "phone"):
        op.drop_column("client_contacts", name)
