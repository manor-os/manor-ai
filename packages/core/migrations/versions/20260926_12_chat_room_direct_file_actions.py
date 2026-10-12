"""Require a fresh owner opt-in for direct group file actions.

Revision ID: 20260926_12
Revises: 20260926_11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_12"
down_revision: Union[str, None] = "20260926_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing On rooms keep the previous per-action approval contract.
    op.add_column(
        "chat_rooms",
        sa.Column("ai_file_actions_direct_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_chat_rooms_direct_requires_enabled_group", "chat_rooms",
        "ai_file_actions_direct_enabled = false OR (kind = 'group' AND ai_tool_execution_enabled = true)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_rooms_direct_requires_enabled_group", "chat_rooms", type_="check")
    op.drop_column("chat_rooms", "ai_file_actions_direct_enabled")
