"""Agent runtime permission checks.

Called from the tool-dispatch path before an agent is allowed to:
  * use an MCP server that requires an entity-scope integration
  * invoke a tool that requires a specific permission
  * write via a protected external integration

Separate from `packages/core/permissions.py` (which handles HTTP-level
RBAC) because the agent runtime needs to reason about:
  - "acting on behalf of user X", with X resolved from the chat context
  - integration scope (user OAuth vs entity credential vs env fallback)
  - per-MCP-server required permission declared on the Integration row

The returned Decision object carries both the allow/deny verdict and a
human-readable reason suitable for surfacing to the LLM or to audit logs.

Env-var fallback (explicitly enabled self-hosted / infrastructure defaults):
  Providers can be pre-seeded via environment variables so agents work
  end-to-end without anyone connecting an integration in the UI first.
  The env mapping is declared once in ``_ENV_TOKEN_VARS`` and consulted
  as a third credential scope ("env") after user-OAuth and entity
  integration paths. Gated by ``MANOR_ALLOW_ENV_TOKENS=1`` to prevent
  prod deployments from silently falling back to shared dev tokens.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.permissions import user_has_permission
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases


# provider → list of env-var names to check, in priority order.
# The first one that's set (and non-empty) wins.
_ENV_TOKEN_VARS: dict[str, list[str]] = {
    "slack":            ["SLACK_BOT_TOKEN"],
    "discord":          ["DISCORD_BOT_TOKEN"],
    "telegram":         ["TELEGRAM_BOT_TOKEN"],
    "wechat_personal":  ["WECHAT_BOT_TOKEN"],
    "wechat_official":  ["WECHAT_APP_SECRET"],
    "whatsapp":         ["WHATSAPP_TOKEN", "TWILIO_AUTH_TOKEN"],
    "twilio":           ["TWILIO_AUTH_TOKEN"],
    "quickbooks":       ["QUICKBOOKS_ACCESS_TOKEN"],
    "gmail":            ["GMAIL_OAUTH_TOKEN"],
    "google_calendar":  ["GOOGLE_CALENDAR_OAUTH_TOKEN", "GOOGLE_OAUTH_TOKEN"],
    "google_drive":     ["GOOGLE_DRIVE_OAUTH_TOKEN", "GOOGLE_OAUTH_TOKEN"],
    "github":           ["GITHUB_TOKEN"],
    "linkedin":         ["LINKEDIN_ACCESS_TOKEN"],
    "twitter_x":        ["X_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN"],
    "notion":           ["NOTION_API_KEY"],
    "webhook":          ["WEBHOOK_BEARER_TOKEN"],
    # Nango is infrastructure (one secret per Manor instance), not a
    # per-user OAuth token, so it's exempt from the MANOR_ALLOW_ENV_TOKENS
    # gate — see _env_token_for below.
    "nango":            ["NANGO_SECRET_KEY"],
}


# Providers whose env-var secret is treated as infrastructure config
# rather than a developer credential. They bypass the
# MANOR_ALLOW_ENV_TOKENS gate so prod deployments can ship the secret in
# their normal env without flipping a "dev mode" flag.
_INFRA_ENV_PROVIDERS: set[str] = {"nango"}

# First-party Manor MCPs use runtime context instead of external credentials.
_FIRST_PARTY_PROVIDER_PREFIXES: tuple[str, ...] = ("manor_mcp_",)


def _is_first_party_provider(provider: str) -> bool:
    return provider == "manor" or provider.startswith(_FIRST_PARTY_PROVIDER_PREFIXES)

def _env_token_for(provider: str) -> str | None:
    """Return the first non-empty env-var value for the provider, or None."""
    provider = canonical_provider_key(provider)
    if (
        provider not in _INFRA_ENV_PROVIDERS
        and os.getenv("MANOR_ALLOW_ENV_TOKENS", "").strip().lower() not in ("1", "true", "yes")
    ):
        return None
    for var in _ENV_TOKEN_VARS.get(provider, []):
        v = os.getenv(var, "").strip()
        if v:
            return v
    return None


@dataclass(frozen=True)
class ToolAccessDecision:
    allowed: bool
    reason: str
    scope: str = ""  # "user" | "entity" | "none" — which credential source resolved


def _entity_integration_has_usable_credentials(integration: Integration) -> bool:
    """Return whether an entity Integration row can actually authenticate.

    Some setup flows create an Integration row before credentials are
    attached. Treating that placeholder as callable lets MCP dispatch reach a
    provider with no token, so runtime gating mirrors the Integrations page's
    ``agent_can_use`` check here.
    """
    cfg = integration.config or {}
    return bool(
        integration.credentials
        or integration.credential_ref
        or cfg.get("nango")
    )


async def can_use_integration(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider: str,
    allow_env_fallback: bool = True,
) -> ToolAccessDecision:
    """Can the acting user use this integration via an agent right now?

    Resolution order:
      1. Personal connection in ``oauth_accounts(user_id, provider)``
         → always allowed if the token exists.
      2. Entity-scope credential in ``integrations(entity_id, provider)``
         → allowed only if the user's role satisfies
         ``integrations.required_permission`` (if set).
      3. Env fallback only when explicitly allowed by the caller and
         MANOR_ALLOW_ENV_TOKENS is enabled.
      4. Neither → denied with a "connect integration" hint.
    """
    provider = canonical_provider_key(provider)
    if _is_first_party_provider(provider):
        return ToolAccessDecision(
            allowed=True,
            reason=f"{provider} is a first-party Manor tool.",
            scope="internal",
        )

    # 1. Try user-scope
    provider_aliases = provider_key_aliases(provider)
    user_row = (
        await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user_id,
                OAuthAccount.provider.in_(provider_aliases),
            )
        )
    ).scalar_one_or_none()

    if user_row and user_row.access_token:
        return ToolAccessDecision(
            allowed=True,
            reason=f"User has personal {provider} connection.",
            scope="user",
        )

    # 2. Fall back to entity-scope. Multi-account credential providers
    # (email inboxes, WhatsApp senders, …) can have several rows per
    # (entity, provider) — pick the most recent and hand off; the
    # caller's later resolution code (``_resolve_bearer_token``)
    # honours ``config.is_default`` if set.
    entity_row = (
        await db.execute(
            select(Integration).where(
                Integration.entity_id == entity_id,
                Integration.provider.in_(provider_aliases),
                Integration.status == "active",
            ).order_by(Integration.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if not entity_row:
        # 3. Dev / cloud-default env fallback (only when flag is set)
        if allow_env_fallback and _env_token_for(provider):
            return ToolAccessDecision(
                allowed=True,
                reason=f"Using {provider} dev credential from environment.",
                scope="env",
            )
        return ToolAccessDecision(
            allowed=False,
            reason=(
                f"No {provider} integration is connected. "
                f"Connect it under Settings → Integrations."
            ),
            scope="none",
        )

    if not _entity_integration_has_usable_credentials(entity_row):
        return ToolAccessDecision(
            allowed=False,
            reason=(
                f"The {provider} integration is configured but has no usable "
                "credentials. Reconnect it under Settings → Integrations."
            ),
            scope="entity",
        )

    required = entity_row.required_permission
    if required:
        granted = await user_has_permission(db, user_id, entity_id, required)
        if not granted:
            return ToolAccessDecision(
                allowed=False,
                reason=(
                    f"The {provider} integration requires the '{required}' permission, "
                    f"which your role doesn't have. Ask an admin to invite you with a "
                    f"higher role, or connect your own {provider} account."
                ),
                scope="entity",
            )

    return ToolAccessDecision(
        allowed=True,
        reason=f"Using entity-level {provider} credentials.",
        scope="entity",
    )


async def can_use_mcp_server(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    server_key: str,
    allow_env_fallback: bool = False,
) -> ToolAccessDecision:
    """Thin alias — MCP server keys equal integration provider keys.

    Kept as a separate helper so callers that model MCP distinctly don't
    leak "Integration" terminology into agent runtime code.
    """
    return await can_use_integration(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider=server_key,
        allow_env_fallback=allow_env_fallback,
    )


def parse_mcp_tool_name(name: str | None) -> tuple[str, str] | None:
    raw = str(name or "").strip()
    if not raw.startswith("mcp__"):
        return None
    parts = raw.split("__", 2)
    if len(parts) != 3:
        return None
    _prefix, server_key, tool_name = parts
    server_key = server_key.strip()
    tool_name = tool_name.strip()
    if not server_key or not tool_name:
        return None
    return server_key, tool_name


async def resolve_agent_direct_mcp_actions(
    db: AsyncSession,
    agent_id: str,
) -> dict[str, set[str]]:
    """Return MCP actions selected through Agent settings tool checkboxes."""

    from packages.core.models.mcp import MCPServer
    from packages.core.models.workspace import AgentToolBinding, ToolDefinition

    rows = (
        await db.execute(
            select(ToolDefinition.name)
            .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
            .where(
                AgentToolBinding.agent_id == agent_id,
                ToolDefinition.status == "active",
            )
        )
    ).scalars().all()
    selected: dict[str, set[str]] = {}
    for name in rows:
        parsed = parse_mcp_tool_name(name)
        if not parsed:
            continue
        server_key, tool_name = parsed
        selected.setdefault(server_key, set()).add(tool_name)
    if not selected:
        return {}

    active_keys = set((
        await db.execute(
            select(MCPServer.server_key).where(
                MCPServer.server_key.in_(list(selected)),
                MCPServer.status == "active",
            )
        )
    ).scalars().all())
    return {
        server_key: actions
        for server_key, actions in selected.items()
        if server_key in active_keys and actions
    }


async def resolve_agent_mcp_scope(
    db: AsyncSession,
    agent_id: str,
) -> dict[str, list[str] | None]:
    """Return the set of MCP server_keys an agent is bound to, with
    optional per-server tool allowlists.

    Returns:
        {
            "gmail": ["send_message", "list_messages"],  # explicit allowlist
            "linkedin": None,                             # all tools allowed
        }

    Callers use this to filter the tool-pool's MCP entries down to just
    what the agent is permitted to see. An empty dict means the agent
    has no MCP bindings — no MCP tools are exposed.
    """
    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    rows = (
        await db.execute(
            select(AgentMCPBinding, MCPServer)
            .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
            .where(
                AgentMCPBinding.agent_id == agent_id,
                AgentMCPBinding.status == "active",
                MCPServer.status == "active",
            )
        )
    ).all()

    scope: dict[str, list[str] | None] = {}
    for binding, server in rows:
        # Explicit agent allowlist wins over server default; None = all tools
        allowed = binding.allowed_tools
        if allowed is None:
            allowed = server.default_allowed_tools  # still may be None
        scope[server.server_key] = allowed
    direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
    for server_key, actions in direct_actions.items():
        existing = scope.get(server_key)
        if existing is None and server_key in scope:
            continue
        merged = set(str(name) for name in (existing or []) if str(name or "").strip())
        merged.update(actions)
        scope[server_key] = sorted(merged)
    return scope


def filter_mcp_tools_by_scope(
    tool_names: list[str],
    scope: dict[str, list[str] | None],
) -> set[str]:
    """Given a list of mcp__<server>__<tool> names and a scope mapping,
    return only the names an agent is permitted to see.

    Non-MCP names are dropped (filter_mcp_tools is MCP-only).
    A missing server in scope → tool excluded.
    A server in scope with allowed=None → every tool for that server allowed.
    A server with an allowlist → only those tool names allowed.
    """
    allowed: set[str] = set()
    for name in tool_names:
        if not name.startswith("mcp__"):
            continue
        parts = name.split("__", 2)
        if len(parts) < 3:
            continue
        _, server_key, tool_name = parts
        if server_key not in scope:
            continue
        server_allowlist = scope[server_key]
        if server_allowlist is None or tool_name in server_allowlist:
            allowed.add(name)
    return allowed
