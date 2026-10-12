"""Code projects — a durable identity for a folder of source files.

A project maps one folder in the entity filesystem to a stable id so agents
can keep adding and editing files across tasks and conversations. The files
themselves stay in the filesystem and Knowledge; this row never mirrors them.
A published Site is one output of a project, and is linked through
``Site.project_id`` without inheriting the project's Workspace.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class CodeProject(Base):
    __tablename__ = "code_projects"
    __table_args__ = (
        Index("uq_code_projects_entity_root", "entity_id", "root_path", unique=True),
        Index("ix_code_projects_entity_workspace", "entity_id", "workspace_id", "archived_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Where the source lives and who maintains it. Independent of any Site's
    # Workspace connection.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Entity-relative folder; never changes once created (files never move).
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Key into packages.core.services.code_projects.PROFILES
    profile: Mapped[str] = mapped_column(String(32), nullable=False, default="generic")
    entry: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
