"""Index room message content for bounded substring search, including CJK.

Revision ID: 20260926_16
Revises: 20260926_15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260926_16"
down_revision: Union[str, None] = "20260926_15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_room_messages_content_trgm "
        "ON chat_room_messages USING GIN (content gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_room_messages_content_trgm")
