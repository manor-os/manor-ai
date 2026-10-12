"""expand Workspace Draft Blueprint references for platform-owned IDs

Revision ID: 20260909_02
Revises: deployment-specific predecessor
Create Date: 2026-09-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import column_exists, table_exists


revision: str = "20260909_02"
down_revision: Union[str, None] = "20260905_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = ("suggested_blueprint_id", "applied_blueprint_id")


def _resize_blueprint_ids(length: int, existing_length: int) -> None:
    if not table_exists("workspace_drafts"):
        return
    for column_name in _COLUMNS:
        if not column_exists("workspace_drafts", column_name):
            continue
        op.alter_column(
            "workspace_drafts",
            column_name,
            existing_type=sa.String(existing_length),
            type_=sa.String(length),
            existing_nullable=True,
        )


def upgrade() -> None:
    _resize_blueprint_ids(160, 26)


def downgrade() -> None:
    _resize_blueprint_ids(26, 160)
