"""Add the nullable document content digest after the public migration head.

Revision ID: 20261104_01
Revises: 20261101_92
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import column_exists, table_exists

revision = "20261104_01"
down_revision = "20261101_92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("documents") and not column_exists("documents", "content_sha256"):
        op.add_column("documents", sa.Column("content_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    if table_exists("documents") and column_exists("documents", "content_sha256"):
        op.drop_column("documents", "content_sha256")
