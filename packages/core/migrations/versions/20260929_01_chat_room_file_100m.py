"""Allow group chat file uploads up to 100 MiB.

Revision ID: 20260929_01
Revises: 20260927_01
"""
from __future__ import annotations

from alembic import op


revision = "20260929_01"
down_revision = "20260927_01"
branch_labels = None
depends_on = None


_STORAGE_CHECK = (
    "size > 0 AND ((content IS NOT NULL AND size <= {max_size} AND document_id IS NULL "
    "AND document_entity_id IS NULL AND content_sha256 IS NULL AND source_run_id IS NULL "
    "AND source_media_job_id IS NULL) OR (content IS NULL AND document_id IS NOT NULL "
    "AND document_entity_id IS NOT NULL AND content_sha256 IS NOT NULL "
    "AND length(content_sha256) = 64 AND source_run_id IS NOT NULL))"
)


def upgrade() -> None:
    op.drop_constraint("ck_chat_room_files_storage", "chat_room_files", type_="check")
    op.create_check_constraint(
        "ck_chat_room_files_storage", "chat_room_files",
        _STORAGE_CHECK.format(max_size=104857600),
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_room_files_storage", "chat_room_files", type_="check")
    op.create_check_constraint(
        "ck_chat_room_files_storage", "chat_room_files",
        _STORAGE_CHECK.format(max_size=10485760),
    )
