"""Track each room member's last displayed message.

Revision ID: 20260926_15
Revises: 20260926_14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_15"
down_revision: Union[str, None] = "20260926_14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_room_members", sa.Column("last_read_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE chat_room_members SET last_read_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.drop_column("chat_room_members", "last_read_at")
