"""Group room agents keep transcripts too.

Revision ID: 20261002_01
Revises: 20261001_01
"""
from alembic import op
import sqlalchemy as sa

revision = "20261002_01"
down_revision = "20261001_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("conversation_transcript_items", "conversation_id", existing_type=sa.String(26), nullable=True)
    op.add_column(
        "conversation_transcript_items",
        sa.Column(
            "room_id", sa.String(26),
            sa.ForeignKey("chat_rooms.id", ondelete="CASCADE", name="fk_conversation_transcript_room"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_conversation_transcript_room_seq", "conversation_transcript_items", ["room_id", "scope_key", "seq"],
    )
    op.create_check_constraint(
        "ck_conversation_transcript_owner", "conversation_transcript_items",
        "(conversation_id IS NULL) <> (room_id IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM conversation_transcript_items WHERE room_id IS NOT NULL")
    op.drop_constraint("ck_conversation_transcript_owner", "conversation_transcript_items", type_="check")
    op.drop_constraint("uq_conversation_transcript_room_seq", "conversation_transcript_items", type_="unique")
    op.drop_column("conversation_transcript_items", "room_id")
    op.alter_column("conversation_transcript_items", "conversation_id", existing_type=sa.String(26), nullable=False)
