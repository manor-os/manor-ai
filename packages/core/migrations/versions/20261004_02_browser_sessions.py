"""Add revocable browser login session families (no plaintext credentials)."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_02"
down_revision = "20261003_client_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("browser_sessions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("entity_id", sa.String(26), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("mfa_authenticated", sa.Boolean(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("previous_hash", sa.String(64)),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.create_index("ix_browser_sessions_user_id", "browser_sessions", ["user_id"])
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])


def downgrade():
    op.drop_table("browser_sessions")
