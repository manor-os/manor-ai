"""Write-ahead recovery records for Runtime file/Knowledge transactions.

This is infrastructure state, not a Task state or an authorization grant.
The committed marker participates in the same transaction as the Document.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class RuntimeFileCommit(Base):
    __tablename__ = "runtime_file_commits"
    __table_args__ = (
        Index("ix_runtime_file_commit_transaction", "transaction_id"),
        Index("ix_runtime_file_commit_effect", "entity_id", "effect_id",
              postgresql_where=text("effect_id IS NOT NULL")),
        Index("ix_runtime_file_commit_recovery", "entity_id", "created_at",
              postgresql_where=text("cleanup_pending = true")),
        Index("uq_runtime_file_commit_unsettled_path", "entity_id", "path_sha256", unique=True,
              postgresql_where=text("state IN ('prepared', 'reconciliation_required')")),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    transaction_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    rel_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    path_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    written_version: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(String(26))
    owner: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Derived only from the exact server-owned file-effect identity in owner.
    # Historical rows stay null; filenames/hashes cannot reconstruct lineage.
    effect_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
