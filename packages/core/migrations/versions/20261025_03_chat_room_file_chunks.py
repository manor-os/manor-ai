"""Store large room attachments in bounded database chunks.

Revision ID: 20261025_03
Revises: 20261019_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20261025_03"
down_revision = "20261021_01"
branch_labels = None
depends_on = None


_LEGACY_STORAGE_CHECK = (
    "size > 0 AND ((content IS NOT NULL AND size <= 104857600 AND document_id IS NULL "
    "AND document_entity_id IS NULL AND content_sha256 IS NULL AND source_run_id IS NULL "
    "AND source_media_job_id IS NULL) OR (content IS NULL AND document_id IS NOT NULL "
    "AND document_entity_id IS NOT NULL AND content_sha256 IS NOT NULL "
    "AND length(content_sha256) = 64 AND source_run_id IS NOT NULL))"
)
_STORAGE_CHECK = (
    "size > 0 AND ((content IS NOT NULL AND size <= 104857600 AND chunk_count IS NULL "
    "AND document_id IS NULL AND document_entity_id IS NULL AND content_sha256 IS NULL "
    "AND source_run_id IS NULL AND source_media_job_id IS NULL) "
    "OR (content IS NULL AND chunk_count IS NOT NULL AND chunk_count > 0 AND size <= 1073741824 "
    "AND (chunk_count - 1) * 4194304 < size AND size <= chunk_count * 4194304 "
    "AND document_id IS NULL AND document_entity_id IS NULL AND content_sha256 IS NOT NULL "
    "AND length(content_sha256) = 64 AND source_run_id IS NULL AND source_media_job_id IS NULL) "
    "OR (content IS NULL AND chunk_count IS NULL AND document_id IS NOT NULL "
    "AND document_entity_id IS NOT NULL AND content_sha256 IS NOT NULL "
    "AND length(content_sha256) = 64 AND source_run_id IS NOT NULL))"
)


def upgrade() -> None:
    op.add_column("chat_room_files", sa.Column("chunk_count", sa.Integer(), nullable=True))
    op.drop_constraint("ck_chat_room_files_storage", "chat_room_files", type_="check")
    op.create_check_constraint("ck_chat_room_files_storage", "chat_room_files", _STORAGE_CHECK)
    op.create_table(
        "chat_room_file_chunks",
        sa.Column("file_id", sa.String(26), sa.ForeignKey("chat_room_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chunk_index", sa.Integer(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_chat_room_file_chunks_index"),
        sa.CheckConstraint(
            "length(content) > 0 AND length(content) <= 4194304",
            name="ck_chat_room_file_chunks_size",
        ),
    )


def downgrade() -> None:
    # A schema downgrade must never silently discard uploaded user files.
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM chat_room_files WHERE chunk_count IS NOT NULL)"
    )).scalar():
        raise RuntimeError("Cannot downgrade while chunked room attachments exist")
    op.drop_table("chat_room_file_chunks")
    op.drop_constraint("ck_chat_room_files_storage", "chat_room_files", type_="check")
    op.drop_column("chat_room_files", "chunk_count")
    op.create_check_constraint("ck_chat_room_files_storage", "chat_room_files", _LEGACY_STORAGE_CHECK)
