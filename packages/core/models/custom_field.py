"""Custom field definitions for extensible entity data."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class CustomFieldDefinition(Base, TimestampMixin):
    """Definition of a custom field that can be added to tasks, clients, etc."""
    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        Index("ix_cfd_entity_target", "entity_id", "target"),
        Index("ix_cfd_workspace", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))  # None = entity-wide
    name: Mapped[str] = mapped_column(String(100), nullable=False)  # internal: "property_address"
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)  # "Property Address"
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # text, number, date, select, multiselect, boolean, url, email, phone
    target: Mapped[str] = mapped_column(String(50), nullable=False)  # "task", "client", "workspace"
    options: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # for select/multiselect: ["Option A", "Option B"]
    default_value: Mapped[Optional[str]] = mapped_column(String(500))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
