"""Retain public Agent execution progress in group chats."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261002_02"
down_revision = "20261002_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_room_agent_runs", sa.Column("assistant_blocks", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_room_agent_runs", "assistant_blocks")
