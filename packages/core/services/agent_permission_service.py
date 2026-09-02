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
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    WorkerStatus,
)
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.integration_account_service import (
    RuntimeIntegrationRegistry,
    load_runtime_integration_registry,
    select_runtime_integration_account,
)


# provider → list of env-var names to check, in priority order.
# The first one that's set (and non-empty) wins.
_ENV_TOKEN_VARS: dict[str, list[str]] = {
    "slack":            ["SLACK_BOT_TOKEN"],
    "discord":          ["DISCORD_BOT_TOKEN"],
    "telegram":         ["TELEGRAM_BOT_TOKEN"],
    "wechat_personal":  ["WECHAT_BOT_TOKEN"],
    "wechat_official":  ["WECHAT_APP_SECRET"],
    "whatsapp":         ["WHATSAPP_TOKEN"],
    # Twilio credentials are always resolved from the user's Integration.
    # Keep this provider out of the env-token map to prevent shared-account access.
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



def provider_requires_integration_account_registry(provider: str) -> bool:
    """Return whether runtime auth for ``provider`` comes from account rows."""
    canonical = canonical_provider_key(provider)
    if _is_first_party_provider(canonical):
        return False
    return True


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
    scope: str = ""  # user/entity/platform/internal/cli_worker/env/none
    account_id: str | None = None


async def can_use_integration(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider: str,
    integration_account_id: str | None = None,
    integration_registry: RuntimeIntegrationRegistry | None = None,
    allow_env_fallback: bool = True,
) -> ToolAccessDecision:
    """Can the acting user use this integration via an agent right now?

    Resolution order:
      2. An owned or explicitly shared OAuth/account connection selected from
         ``list_runtime_integration_accounts``.
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

    # Resolve an explicit account or the ordered owner/shared default from the
    # user-scoped registry. There is no implicit Entity-wide credential access.
    registry_binding = None
    if integration_registry is not None and integration_registry.covers(provider):
        if (
            integration_registry.user_id != user_id
            or integration_registry.entity_id != entity_id
        ):
            return ToolAccessDecision(
                allowed=False,
                reason="The integration registry does not match the active runtime scope.",
                scope="none",
            )
        registry_binding = integration_registry.integration(provider)
        accounts = list(integration_registry.accounts_for(provider))
    else:
        resolved_registry = await load_runtime_integration_registry(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=[provider],
        )
        registry_binding = resolved_registry.integration(provider)
        accounts = list(resolved_registry.accounts_for(provider))
    if (
        registry_binding is not None
        and registry_binding.load_errors
        and not str(integration_account_id or "").strip()
    ):
        return ToolAccessDecision(
            allowed=False,
            reason=(
                f"The {provider} account registry is incomplete. "
                "Select an exact known account or retry after the integration "
                "service recovers."
            ),
            scope="none",
        )
    selected, selection_error = select_runtime_integration_account(
        accounts,
        integration_account_id,
    )
    if selection_error:
        selected_account_id = str(integration_account_id or "").strip()
        if (
            registry_binding is not None
            and selected_account_id
            in registry_binding.reconnect_required_account_ids
        ):
            return ToolAccessDecision(
                allowed=False,
                reason=(
                    f"The {provider} connection is no longer available in Nango. "
                    "Reconnect it under Settings → Integrations."
                ),
                scope="none",
            )
        return ToolAccessDecision(
            allowed=False,
            reason=selection_error,
            scope="none",
        )

    if selected:
        reason = (
            f"User has personal {provider} connection."
            if selected.ownership == "mine"
            else f"User has shared access to a {provider} connection."
        )
        return ToolAccessDecision(
            allowed=True,
            reason=reason,
            scope=selected.scope.value,
            account_id=selected.id,
        )

    if not accounts:
        if registry_binding is not None:
            if registry_binding.load_errors:
                return ToolAccessDecision(
                    allowed=False,
                    reason=(
                        f"The {provider} account registry is incomplete. "
                        "Retry after the integration service recovers."
                    ),
                    scope="none",
                )
            configured_count = registry_binding.configured_entity_account_count
            credentialed_count = registry_binding.credentialed_entity_account_count
            required = next(iter(registry_binding.denied_permissions), None)
        else:
            # A registry that covers this provider is authoritative even when
            # its full snapshot has no binding: no persisted account exists.
            configured_count = 0
            credentialed_count = 0
            required = None
        if configured_count:
            if registry_binding.reconnect_required_account_ids:
                return ToolAccessDecision(
                    allowed=False,
                    reason=(
                        f"The {provider} connection is no longer available in Nango. "
                        "Reconnect it under Settings → Integrations."
                    ),
                    scope="none",
                )
            if not credentialed_count:
                return ToolAccessDecision(
                    allowed=False,
                    reason=(
                        f"The {provider} integration is configured but has no usable "
                        "credentials. Reconnect it under Settings → Integrations."
                    ),
                    scope="none",
                )
            if required:
                return ToolAccessDecision(
                    allowed=False,
                    reason=(
                        f"The {provider} integration requires the '{required}' permission, "
                        "which your role doesn't have. Ask an admin to grant access, or "
                        f"connect your own {provider} account."
                    ),
                    scope="none",
                )
        # Dev / cloud-default env fallback (only when the caller allows it).
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

    return ToolAccessDecision(
        allowed=False,
        reason=f"No usable {provider} integration account is available.",
        scope="none",
    )


async def resolve_usable_mcp_providers(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str],
) -> frozenset[str]:
    """Batch 'which MCP servers can this user use right now'.

    Used by Tool Discovery v2 search-time pre-filtering. Account-backed
    providers share one credential-free, actor-scoped registry snapshot;
    special first-party/platform/CLI providers still reuse
    ``can_use_integration`` so their gates cannot drift from dispatch.
    Failures fail-open for discovery only; execution always revalidates.

    ``provider_keys`` is required (no default catalog import here): this
    file is a non-runtime production module and importing
    packages.core.ai.tools.mcp_builtin directly from it would violate the
    tool-to-tool import boundary enforced by
    test_production_tool_to_tool_imports_stay_runtime_owned. The caller
    (packages/core/ai/runtime/tool_search.py's search handler, which is
    runtime-owned and exempt from that scan) derives the provider keys
    from its own view of the tool schemas instead.
    """
    canonical_keys = tuple(dict.fromkeys(
        canonical
        for key in provider_keys
        if (canonical := canonical_provider_key(key))
    ))
    account_provider_keys = tuple(
        key for key in canonical_keys
        if provider_requires_integration_account_registry(key)
    )
    integration_registry: RuntimeIntegrationRegistry | None = None
    registry_failed = False
    if account_provider_keys:
        try:
            integration_registry = await load_runtime_integration_registry(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider_keys=account_provider_keys,
            )
        except Exception:
            # Search-time filtering is advisory. A registry outage must not
            # hide a potentially usable provider; the execution gate will
            # reload the exact account and fail closed before provider I/O.
            registry_failed = True

    usable: set[str] = set()
    for key in canonical_keys:
        if registry_failed and key in account_provider_keys:
            usable.add(key)
            continue
        try:
            decision = await can_use_integration(
                db, user_id=user_id, entity_id=entity_id,
                provider=key,
                integration_registry=integration_registry,
                allow_env_fallback=False,
            )
            if decision.allowed:
                usable.add(key)
        except Exception:
            usable.add(key)  # fail-open: never hide tools due to an error
    return frozenset(usable)


async def can_use_mcp_server(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    server_key: str,
    integration_account_id: str | None = None,
    integration_registry: RuntimeIntegrationRegistry | None = None,
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
        integration_account_id=integration_account_id,
        integration_registry=integration_registry,
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
