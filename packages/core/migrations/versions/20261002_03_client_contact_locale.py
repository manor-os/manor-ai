"""Add customer contact email language without changing existing preferences.
Revision ID: 20261002_03
Revises: 20261002_client_email
"""
from alembic import op
import sqlalchemy as sa

revision = "20261002_03"
down_revision = "20261002_client_email"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("client_contacts", sa.Column("locale", sa.String(10), nullable=True))


def downgrade():
    op.drop_column("client_contacts", "locale")
