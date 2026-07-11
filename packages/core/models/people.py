"""Client model.

Staff (employees, contractors, vendors, externals) lives in ``staff.py``.
The legacy ``StaffMember`` class was removed in migration 20260422_03
when the two tables were consolidated into the unified ``staff`` table.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, SoftDeleteMixin, generate_ulid


class Client(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    address: Mapped[Optional[str]] = mapped_column(String)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")
