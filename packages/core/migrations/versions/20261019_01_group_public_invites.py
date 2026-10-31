"""Add revocable public invitation links to group chats."""
from alembic import op
import sqlalchemy as sa

revision = "20261019_01"
down_revision = "20261018_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_rooms", sa.Column("public_invite_token", sa.String(64), nullable=True))
    op.create_index("uq_chat_rooms_public_invite_token", "chat_rooms", ["public_invite_token"], unique=True)
    op.create_check_constraint(
        "ck_chat_rooms_public_invite_group", "chat_rooms",
        "kind = 'group' OR public_invite_token IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_rooms_public_invite_group", "chat_rooms", type_="check")
    op.drop_index("uq_chat_rooms_public_invite_token", table_name="chat_rooms")
    op.drop_column("chat_rooms", "public_invite_token")
