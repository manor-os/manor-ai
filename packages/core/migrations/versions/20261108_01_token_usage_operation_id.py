"""Tie each token usage row to the run that made the call.

Revision ID: 20261108_01
Revises: 20261107_01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261108_01"
down_revision: Union[str, None] = "20261107_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable and additive: existing rows stay as they are. No index yet; the
    # run-level lookups go through the run's own usage summary.
    op.execute(
        "ALTER TABLE token_usage_logs ADD COLUMN IF NOT EXISTS operation_id VARCHAR(100)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE token_usage_logs DROP COLUMN IF EXISTS operation_id")
