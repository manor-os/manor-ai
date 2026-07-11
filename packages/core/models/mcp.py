"""MCP (Model Context Protocol) server registry and per-agent bindings.

Design
------
* ``MCPServer`` is a global registry row: what servers this deployment knows
  about, how to reach them, what auth flavor they expect.
* ``AgentMCPBinding`` is the agent-level allowlist: which servers a given
  template agent may call, optionally scoped to a subset of tools.
* Credentials (OAuth tokens, API keys) live elsewhere:
    - personal connections → ``oauth_accounts`` (user-scoped)
    - entity-wide systems  → ``integrations`` (entity-scoped, gated by
      ``integrations.required_permission``)
  The MCP runtime resolves credentials per-request based on the acting user
  and falls back to the entity-level integration when no personal one exists.
* There is intentionally no per-workspace MCP scoping. An agent carries its
  binding set with it; workspaces only decide *which agents* they hire.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, generate_ulid


class MCPServer(Base, TimestampMixin):
    """Global registry of MCP servers available to this deployment."""
    __tablename__ = "mcp_servers"
    __table_args__ = (
        Index("ix_mcp_servers_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)

    # Identity
    server_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Stable short name used in tool names (mcp__<server_key>__<tool>) and
    # as the OAuth provider lookup key into oauth_accounts / integrations.
    # Examples: "gmail", "google_calendar", "linkedin", "manor_pms".

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Transport
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    # "builtin" — in-process Python module (endpoint = import path)
    # "http"    — remote MCP over HTTP (endpoint = URL)
    # "stdio"   — child process (endpoint = JSON {"command": ..., "args": [...]})
    endpoint: Mapped[Optional[str]] = mapped_column(String(500))

    # Auth contract (informational; credentials live elsewhere)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # "none" | "oauth2" | "api_key" | "bearer"
    scopes: Mapped[Optional[str]] = mapped_column(String(500))
    # OAuth scopes, comma-separated. Used by the OAuth connect flow,
    # not enforced here.

    # Discovery cache — populated by a background refresh job.
    tools_cached: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    tools_cached_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Defaults applied when an AgentMCPBinding doesn't override.
    default_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    default_allowed_tools: Mapped[Optional[list]] = mapped_column(JSONB)
    # null = all tools; else JSON array of tool_name strings.

    # Vault-backed storage for system-level secrets (e.g. OAuth
    # client_secret). When set, ``default_config`` carries non-secret
    # config (client_id, scopes) and the secret is fetched via
    # CredentialService.lease_mcp_server. Null = legacy plaintext path
    # (secret in default_config.oauth_client_secret).
    #
    # Stored as TEXT (not VARCHAR) because Vault transit ciphertext
    # encoded under the standard envelope (``vault:v1:<base64>``)
    # routinely exceeds 200 chars — Stripe's OAuth client secret hits
    # ~280 chars. The original VARCHAR(200) silently aborted the
    # ``seed_oauth_clients_from_env`` transaction at startup, leaving
    # every provider after Stripe in dict iteration order without DB
    # rows. Matches ``integrations.credential_ref`` and
    # ``channel_configs.credential_ref`` (both already Text).
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[Optional[str]] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # "active" | "disabled" | "deprecated"

    # Type-specific operational details. 1-to-1 side tables; only one
    # is populated based on auth_type:
    #   auth_type="cli_worker"      → cli_spec row
    #   auth_type="browser_session" → browser_spec row
    #   else                        → both NULL (data lives in default_config)
    cli_spec = relationship(
        "CLIToolSpec",
        back_populates="server",
        uselist=False,
        cascade="all, delete-orphan",
    )
    browser_spec = relationship(
        "BrowserToolSpec",
        back_populates="server",
        uselist=False,
        cascade="all, delete-orphan",
    )


class AgentMCPBinding(Base, TimestampMixin):
    """Per-agent allowlist — which MCP servers an agent template may call."""
    __tablename__ = "agent_mcp_bindings"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "mcp_server_id", name="uq_agent_mcp_bindings_pair"
        ),
        Index("ix_agent_mcp_bindings_agent", "agent_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    mcp_server_id: Mapped[str] = mapped_column(String(26), nullable=False)

    allowed_tools: Mapped[Optional[list]] = mapped_column(JSONB)
    # null = inherit mcp_servers.default_allowed_tools; else per-agent allowlist.

    config_override: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Merged over mcp_servers.default_config at runtime.

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
