"""Persist private Workspace invitation email results.
Revision ID: 20261002_client_email
Revises: 20261002_client_collab
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261002_client_email"
down_revision = "20261002_client_collab"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("client_workspace_deliveries", sa.Column("email_delivery", JSONB, nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("client_workspace_deliveries", "email_delivery")
