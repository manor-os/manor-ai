"""Document version model — immutable snapshot of a document at a point in time."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class DocumentVersion(Base):
    """Immutable snapshot of a document at a point in time."""
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    document_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    fs_path: Mapped[Optional[str]] = mapped_column(String(1000))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    change_summary: Mapped[Optional[str]] = mapped_column(String(500))
    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
