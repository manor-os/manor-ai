"""Track group Agent queue handoff for bounded recovery.

Revision ID: 20260926_13
Revises: 20260926_12
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_13"
down_revision: Union[str, None] = "20260926_12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_room_agent_runs", sa.Column("last_enqueued_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("chat_room_agent_runs", "last_enqueued_at")
