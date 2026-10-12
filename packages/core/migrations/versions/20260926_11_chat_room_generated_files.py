"""Add immutable Knowledge references for generated group files.

Revision ID: 20260926_11
Revises: 20260926_10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_11"
down_revision: Union[str, None] = "20260926_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_jobs", sa.Column(
        "room_agent_run_id", sa.String(26),
        sa.ForeignKey("chat_room_agent_runs.id", ondelete="SET NULL"),
    ))
    op.create_index("ix_media_jobs_room_agent_run", "media_jobs", ["room_agent_run_id"])
    op.alter_column("media_jobs", "file_size", type_=sa.BigInteger(), existing_type=sa.Integer())

    op.drop_constraint("ck_chat_room_files_size", "chat_room_files", type_="check")
    op.alter_column("chat_room_files", "size", type_=sa.BigInteger(), existing_type=sa.Integer())
    op.alter_column("chat_room_files", "content", nullable=True, existing_type=sa.LargeBinary())
    op.add_column("chat_room_files", sa.Column(
        "document_id", sa.String(26), sa.ForeignKey("documents.id", ondelete="CASCADE"),
    ))
    op.add_column("chat_room_files", sa.Column("document_entity_id", sa.String(26)))
    op.add_column("chat_room_files", sa.Column("content_sha256", sa.String(64)))
    op.add_column("chat_room_files", sa.Column(
        "source_run_id", sa.String(26),
        sa.ForeignKey("chat_room_agent_runs.id", ondelete="CASCADE"),
    ))
    op.add_column("chat_room_files", sa.Column("source_media_job_id", sa.String(26)))
    op.add_column("chat_room_files", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        "uq_chat_room_files_run_document", "chat_room_files", ["source_run_id", "document_id"],
    )
    op.create_check_constraint(
        "ck_chat_room_files_storage", "chat_room_files",
        "size > 0 AND ((content IS NOT NULL AND size <= 10485760 AND document_id IS NULL "
        "AND document_entity_id IS NULL AND content_sha256 IS NULL AND source_run_id IS NULL "
        "AND source_media_job_id IS NULL) OR (content IS NULL AND document_id IS NOT NULL "
        "AND document_entity_id IS NOT NULL AND content_sha256 IS NOT NULL "
        "AND length(content_sha256) = 64 AND source_run_id IS NOT NULL))",
    )


def downgrade() -> None:
    # Generated references have no DB bytes, so they cannot be represented by
    # the previous inline-only schema.
    op.execute("DELETE FROM chat_room_files WHERE document_id IS NOT NULL")
    op.drop_constraint("ck_chat_room_files_storage", "chat_room_files", type_="check")
    op.drop_constraint("uq_chat_room_files_run_document", "chat_room_files", type_="unique")
    op.drop_column("chat_room_files", "deleted_at")
    op.drop_column("chat_room_files", "source_media_job_id")
    op.drop_column("chat_room_files", "source_run_id")
    op.drop_column("chat_room_files", "content_sha256")
    op.drop_column("chat_room_files", "document_entity_id")
    op.drop_column("chat_room_files", "document_id")
    op.alter_column("chat_room_files", "content", nullable=False, existing_type=sa.LargeBinary())
    op.alter_column("chat_room_files", "size", type_=sa.Integer(), existing_type=sa.BigInteger())
    op.create_check_constraint("ck_chat_room_files_size", "chat_room_files", "size > 0 AND size <= 10485760")

    op.alter_column("media_jobs", "file_size", type_=sa.Integer(), existing_type=sa.BigInteger())
    op.drop_index("ix_media_jobs_room_agent_run", table_name="media_jobs")
    op.drop_column("media_jobs", "room_agent_run_id")
