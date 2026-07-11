"""1-to-1 side tables hanging off ``mcp_servers`` for non-MCP execution
mechanisms (CLI Worker dispatch, Playwright browser automation).

The base ``mcp_servers`` row carries the user-facing concept ("Claude
Code", "即梦"), the auth_type, and category. These spec tables carry
the operational details specific to each execution model. Keeping
them out of mcp_servers avoids forcing every SaaS row to carry
nullable cli_* / browser_* columns.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, generate_ulid


class CLIToolSpec(Base):
    """Drives ``auth_type='cli_worker'`` MCP server rows.

    Tells the dispatcher what command an external worker should run,
    what subcommands the agent layer may invoke, and how to capture output.
    """
    __tablename__ = "cli_tool_specs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    mcp_server_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )

    command_template: Mapped[str] = mapped_column(Text, nullable=False)
    supported_subcommands: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    requires_local_paths: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default="120")
    output_format: Mapped[str] = mapped_column(String(16), nullable=False, default="text", server_default="text")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    server = relationship("MCPServer", back_populates="cli_spec")


class BrowserToolSpec(Base):
    """Legacy ``auth_type='browser_session'`` MCP server metadata.

    Older deployments used these rows to point at per-site Playwright provider
    modules. New browser automation goes through Chrome/local browser MCP
    surfaces; the table remains for migrations and stale-row cleanup.
    """
    __tablename__ = "browser_tool_specs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    mcp_server_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )

    login_url: Mapped[str] = mapped_column(String(500), nullable=False)
    session_check_selector: Mapped[Optional[str]] = mapped_column(String(500))
    provider_module: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_actions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    cookie_ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    server = relationship("MCPServer", back_populates="browser_spec")
