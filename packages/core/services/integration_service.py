"""Integration & Channel service — CRUD operations.

Credentials note: ``create_integration`` and ``update_integration`` route
the ``credentials`` argument through ``CredentialService`` so new rows
land encrypted (vault_transit / dev_fernet, depending on backend) rather
than as raw JSONB. Reads on the ORM model still expose the legacy
``credentials`` field for backward compatibility — callers that need
plaintext after a write should use ``CredentialService.lease_integration``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration, Channel


def _parse_csv_env(name: str) -> set[str]:
    """Parse a comma-separated env var into a set of stripped, lowercased
    tokens. Empty entries discarded; missing var → empty set.

    Used by the integration "preview" flag that lets non-prod
    environments unblock specific providers from the ``_COMING_SOON_*``
    sets without code changes."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


# ── Integrations ──

async def list_integrations(db: AsyncSession, entity_id: str) -> list[Integration]:
    result = await db.execute(
        select(Integration)
        .where(Integration.entity_id == entity_id)
        .order_by(Integration.created_at.desc())
    )
    return list(result.scalars().all())


async def get_integration(
    db: AsyncSession, integration_id: str, entity_id: str,
) -> Optional[Integration]:
    result = await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def create_integration(
    db: AsyncSession, entity_id: str, provider: str, *,
    config: dict | None = None, credentials: dict | None = None,
) -> Integration:
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        provider=provider,
        config=config or {},
        credentials={},
    )
    if credentials:
        # store_integration sets credential_ref + credential_scheme and
        # leaves the legacy JSONB empty. Needs the row to have an id +
        # entity + provider populated (above) so the context is stable.
        get_credential_service().store_integration(integration, credentials)
    db.add(integration)
    await db.flush()
    return integration


async def update_integration(
    db: AsyncSession, integration_id: str, entity_id: str, **kwargs,
) -> Optional[Integration]:
    integration = await get_integration(db, integration_id, entity_id)
    if not integration:
        return None
    # Pull credentials out of kwargs — those route through the vault.
    new_creds = kwargs.pop("credentials", None)
    for key, value in kwargs.items():
        if value is not None and hasattr(integration, key):
            setattr(integration, key, value)
    if new_creds is not None:
        get_credential_service().store_integration(integration, new_creds)
    await db.flush()
    await db.refresh(integration)
    return integration


async def delete_integration(
    db: AsyncSession, integration_id: str, entity_id: str,
) -> bool:
    integration = await get_integration(db, integration_id, entity_id)
    if not integration:
        return False
    await db.delete(integration)
    await db.flush()
    return True


async def list_accounts_by_provider(
    db: AsyncSession, entity_id: str, provider: str,
) -> list[Integration]:
    """Every Integration row for (entity, provider) — ordered with the
    default first, then newest.

    Used to show the "entity_accounts" list on an integration card when a
    provider supports multiple accounts (email inboxes, WhatsApp senders,
    WeChat bots, etc.).
    """
    rows = (await db.execute(
        select(Integration)
        .where(
            Integration.entity_id == entity_id,
            Integration.provider == provider,
            Integration.status == "active",
        )
        .order_by(Integration.created_at.desc())
    )).scalars().all()
    # Stable: default first, then creation time desc
    return sorted(
        rows,
        key=lambda r: (0 if (r.config or {}).get("is_default") else 1, -_ts(r.created_at)),
    )


def _ts(dt) -> float:
    try:
        return dt.timestamp() if dt else 0.0
    except Exception:
        return 0.0


async def list_channel_bindings(
    db: AsyncSession, entity_id: str,
) -> list[dict]:
    """Every ChannelConfig for this entity, joined with its Channel
    binding (if any) so the UI can render "which agent is this channel
    routing to".

    Returns a flat list of dicts — ChannelConfig fields + bound_channel_id,
    bound_agent_id, agent_name, last_inbound_at, last_outbound_at.
    """
    from sqlalchemy import func
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.models.document import Channel
    from packages.core.models.workspace import Agent

    ccs = (await db.execute(
        select(ChannelConfig)
        .where(ChannelConfig.entity_id == entity_id)
        .order_by(ChannelConfig.channel_type.asc(), ChannelConfig.created_at.asc())
    )).scalars().all()

    if not ccs:
        return []

    channels = (await db.execute(
        select(Channel)
        .where(
            Channel.entity_id == entity_id,
            Channel.type.in_([c.channel_type for c in ccs]),
        )
    )).scalars().all()

    # Map ChannelConfig.id → Channel binding
    bindings_by_cc: dict[str, Channel] = {}
    for ch in channels:
        cfg = ch.config or {}
        # Primary linkage: explicit channel_config_id
        cc_id = cfg.get("channel_config_id")
        if not cc_id:
            # Legacy: Channel.config.integration_id → ChannelConfig
            # (matched via ChannelConfig.config.integration_id)
            integ_id = cfg.get("integration_id")
            if integ_id:
                match = next(
                    (c for c in ccs if (c.config or {}).get("integration_id") == integ_id),
                    None,
                )
                cc_id = match.id if match else None
        if cc_id:
            bindings_by_cc.setdefault(cc_id, ch)

    agent_ids = {ch.agent_id for ch in channels if ch.agent_id}
    agents_by_id: dict[str, Agent] = {}
    if agent_ids:
        agents = (await db.execute(
            select(Agent).where(Agent.id.in_(agent_ids))
        )).scalars().all()
        agents_by_id = {a.id: a for a in agents}

    # Pull last-inbound + last-outbound timestamps per ChannelConfig
    # in one go. Single GROUP BY — cheap even with many bindings.
    cc_ids = [c.id for c in ccs]
    last_activity: dict[tuple[str, str], Any] = {}
    if cc_ids:
        rows = (await db.execute(
            select(
                MessageLog.channel_config_id,
                MessageLog.direction,
                func.max(MessageLog.created_at).label("last_at"),
            )
            .where(
                MessageLog.channel_config_id.in_(cc_ids),
                MessageLog.direction.in_(("inbound", "outbound")),
            )
            .group_by(MessageLog.channel_config_id, MessageLog.direction)
        )).all()
        for cc_id, direction, last_at in rows:
            last_activity[(cc_id, direction)] = last_at

    out: list[dict] = []
    for cc in ccs:
        ch = bindings_by_cc.get(cc.id)
        creds = cc.credentials or {}
        cfg = cc.config or {}
        display_name = (
            cfg.get("name")
            or creds.get("from_address")
            or creds.get("username")
            or creds.get("phone_number")
            or creds.get("account_sid")
            or creds.get("app_id")
            or creds.get("url")
            or cc.name
            or cc.provider
            or cc.channel_type
            or "Configured channel"
        )
        agent = agents_by_id.get(ch.agent_id) if ch and ch.agent_id else None
        last_in = last_activity.get((cc.id, "inbound"))
        last_out = last_activity.get((cc.id, "outbound"))
        out.append({
            "channel_config_id": cc.id,
            "channel_type": cc.channel_type,
            "provider": cc.provider,
            "name": cfg.get("name") or cc.name,
            "display_name": str(display_name),
            "status": cc.status,
            "bound_channel_id": ch.id if ch else None,
            "bound_agent_id": ch.agent_id if ch else None,
            "agent_name": agent.name if agent else None,
            "binding_status": ch.status if ch else None,
            "last_inbound_at": last_in.isoformat() if last_in else None,
            "last_outbound_at": last_out.isoformat() if last_out else None,
        })
    return out


async def upsert_channel_binding(
    db: AsyncSession, *, entity_id: str, channel_config_id: str,
    agent_id: Optional[str],
) -> "Channel":
    """Bind a ChannelConfig to an agent (insert or update Channel row).

    ``agent_id=None`` means "unassigned" — valid state; inbound lands,
    dispatch_inbound returns status=unbound until an agent is set.
    """
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    cc = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == channel_config_id,
            ChannelConfig.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not cc:
        raise ValueError("Channel config not found")

    # Find existing Channel for this ChannelConfig
    existing = (await db.execute(
        select(Channel).where(
            Channel.entity_id == entity_id,
            Channel.type == cc.channel_type,
            Channel.config["channel_config_id"].astext == cc.id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.agent_id = agent_id
        existing.status = "active"
        await db.flush()
        return existing

    row = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        type=cc.channel_type,
        name=(cc.config or {}).get("name") or cc.name or cc.channel_type,
        agent_id=agent_id,
        config={"channel_config_id": cc.id},
        status="active",
    )
    db.add(row)
    await db.flush()
    return row


async def delete_channel_binding(
    db: AsyncSession, entity_id: str, channel_id: str,
) -> bool:
    from packages.core.models.document import Channel
    row = (await db.execute(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def set_default_integration(
    db: AsyncSession, entity_id: str, integration_id: str,
) -> Optional[Integration]:
    """Mark one Integration as the default for its (entity, provider)
    pair — agents fall back to it when no account is explicitly
    selected. Flips the flag off on every sibling row first.
    """
    target = await get_integration(db, integration_id, entity_id)
    if not target:
        return None

    # Load all siblings for the same provider
    siblings = (await db.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == target.provider,
        )
    )).scalars().all()

    for row in siblings:
        cfg = dict(row.config or {})
        cfg["is_default"] = row.id == integration_id
        row.config = cfg
    await db.flush()
    await db.refresh(target)
    return target


# ── Channels ──

async def list_channels(
    db: AsyncSession, entity_id: str, *,
    workspace_id: str | None = None,
) -> list[Channel]:
    q = select(Channel).where(Channel.entity_id == entity_id)
    if workspace_id is not None:
        q = q.where(Channel.workspace_id == workspace_id)
    q = q.order_by(Channel.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_channel(
    db: AsyncSession, channel_id: str, entity_id: str,
) -> Optional[Channel]:
    result = await db.execute(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def create_channel(
    db: AsyncSession, entity_id: str, type: str, *,
    name: str | None = None, user_id: str | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None, config: dict | None = None,
) -> Channel:
    channel = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        type=type,
        name=name,
        workspace_id=workspace_id,
        agent_id=agent_id,
        config=config or {},
    )
    db.add(channel)
    await db.flush()
    return channel


async def update_channel(
    db: AsyncSession, channel_id: str, entity_id: str, **kwargs,
) -> Optional[Channel]:
    channel = await get_channel(db, channel_id, entity_id)
    if not channel:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(channel, key):
            setattr(channel, key, value)
    await db.flush()
    await db.refresh(channel)
    return channel


async def delete_channel(
    db: AsyncSession, channel_id: str, entity_id: str,
) -> bool:
    channel = await get_channel(db, channel_id, entity_id)
    if not channel:
        return False
    await db.delete(channel)
    await db.flush()
    return True


# ── Integration Inventory ──────────────────────────────────────────────

# Channel type → required integration provider mapping
_CHANNEL_TO_PROVIDER = {
    "telegram": "telegram",
    "slack": "slack",
    "discord": "discord",
    "whatsapp": "whatsapp",
    "email": "email",
    "wechat": "wechat_official",
    "wechat_personal": "wechat_personal",
    "twilio_sms": "twilio",
    "twilio_voice": "twilio",
    "facebook": "facebook",
    "webchat": None,   # built-in, no integration needed
    "in_app": None,    # built-in
    "inapp": None,     # built-in (alias)
}

# MCP servers that are not yet production-ready.
# Excluded from the integration inventory so agents/architects don't
# suggest them. Remove from this set as each ships, OR allowlist
# per-environment via ``MANOR_PREVIEW_INTEGRATIONS`` (see
# ``coming_soon_servers()``).
_COMING_SOON_SERVERS_BASE = {
    # CLI workers - need a paired worker daemon on the user's machine
    "claude_code", "codex_cli", "gemini_cli", "cursor_cli", "aider", "continue_cli",
    # facebook + gmail + google_calendar + google_drive ship by default
    # for screencast / App Review demo on the test server. Production
    # gating is now handled at the provider level (Meta App Review,
    # Google OAuth verification) — non-approved users see the upstream
    # warning + scope grant fails for advanced scopes, which is the
    # correct UX surface for that state.
    # PayPal — sandbox-only until Live App approval issued by PayPal
    "paypal",
    # Image/video generation — gateway not shipped yet
    "jimeng",
    # QuickBooks — Intuit App Assessment required before Production
    # OAuth client can serve real users (sandbox + Test mode work, but
    # production users get blocked). Unblock per-environment via the
    # MANOR_PREVIEW_INTEGRATIONS env (PR #21) for sandbox testing.
    "quickbooks",
    # Microsoft 365 — needs Azure AD App Registration + the deploy's
    # MS_CLIENT_ID/SECRET env. All 5 share one app registration so
    # they unlock together.
    "outlook", "onedrive", "ms_calendar", "ms_teams", "ms_excel",
}

# Channel types that are coming soon (adapter exists but not production-ready).
_COMING_SOON_CHANNELS_BASE: set[str] = set()


def coming_soon_servers() -> set[str]:
    """Effective coming-soon set for THIS environment.

    Subtracts anything listed in the ``MANOR_PREVIEW_INTEGRATIONS`` env
    var (comma-separated provider keys, case-insensitive) from the base
    set. Lets dev / staging environments enable specific providers for
    Test User work while production stays locked until each provider's
    real review (Meta App Review, Google OAuth verification, etc.) lands.

    Example:
        MANOR_PREVIEW_INTEGRATIONS=facebook,gmail
    """
    return _COMING_SOON_SERVERS_BASE - _parse_csv_env("MANOR_PREVIEW_INTEGRATIONS")


def coming_soon_channels() -> set[str]:
    """Effective coming-soon channel set for this environment.

    Same envelope as ``coming_soon_servers`` but reads
    ``MANOR_PREVIEW_CHANNELS``. Most callers should use
    ``MANOR_PREVIEW_INTEGRATIONS`` since channel + provider visibility
    usually unlocks together — keep this separate so a deployment can
    surface the OAuth provider for testing without exposing the channel
    to the workspace setup wizard before it's wired."""
    return _COMING_SOON_CHANNELS_BASE - _parse_csv_env("MANOR_PREVIEW_CHANNELS")


# Backward-compat aliases — existing call sites read these as sets.
# Computed once at import; if a deployment toggles the env var at runtime
# it needs to reload the process (the same is true for every other env-
# driven constant in this codebase).
_COMING_SOON_SERVERS = coming_soon_servers()
_COMING_SOON_CHANNELS = coming_soon_channels()

_CHANNEL_LABELS = {
    "telegram": "Telegram",
    "slack": "Slack",
    "discord": "Discord",
    "whatsapp": "WhatsApp",
    "email": "Email",
    "webchat": "Webchat (embeddable widget)",
    "wechat": "WeChat Official",
    "wechat_personal": "WeChat Personal",
    "twilio_sms": "Twilio SMS",
    "twilio_voice": "Twilio Voice",
    "facebook": "Facebook Messenger",
    "in_app": "In-App Notification",
    "inapp": "In-App Notification",
}


async def get_integration_inventory(
    db: AsyncSession,
    entity_id: str,
) -> dict:
    """Return a full picture of the entity's integration status.

    Used by:
      - Workspace architect (to select channels during setup)
      - Manor integration tools (so agents can query what's available)
      - Admin dashboards

    Returns::

        {
            "integrations": [
                {
                    "provider": "telegram",
                    "type": "entity_credential",
                    "status": "active",
                    "healthy": true,
                    "is_default": true,
                    "ready": true,
                },
                ...
            ],
            "channels": [
                {
                    "key": "telegram",
                    "name": "Telegram",
                    "ready": true,
                    "needs_integration": false,
                },
                {
                    "key": "slack",
                    "name": "Slack",
                    "ready": false,
                    "needs_integration": true,
                    "required_provider": "slack",
                },
                ...
            ],
        }
    """
    import logging
    logger = logging.getLogger(__name__)

    integrations: list[dict] = []

    # 1. Entity-level integrations (API keys, browser sessions, etc.)
    try:
        int_rows = list((await db.execute(
            select(Integration).where(Integration.entity_id == entity_id)
        )).scalars().all())

        for i in int_rows:
            health = (i.config or {}).get("last_health_check", {})
            has_credentials = bool(
                i.credentials
                or i.credential_ref
                or (i.config or {}).get("nango")
            )
            integrations.append({
                "id": i.id,
                "provider": i.provider,
                "type": "entity_credential",
                "status": i.status,
                "healthy": health.get("ok") if health else None,
                "has_credentials": has_credentials,
                "is_default": (i.config or {}).get("is_default", False),
                "ready": (
                    i.status == "active"
                    and has_credentials
                    and health.get("ok") is not False
                ),
            })
    except Exception:
        logger.debug("Failed to load entity integrations", exc_info=True)

    # 2. User OAuth connections (personal tokens — e.g. Google, GitHub)
    try:
        from packages.core.models.user import OAuthAccount, User
        oauth_rows = list((await db.execute(
            select(OAuthAccount)
            .join(User, User.id == OAuthAccount.user_id)
            .where(User.entity_id == entity_id)
        )).scalars().all())

        oauth_providers: set[str] = set()
        for o in oauth_rows:
            oauth_providers.add(o.provider)

        for provider in oauth_providers:
            if not any(c["provider"] == provider for c in integrations):
                integrations.append({
                    "provider": provider,
                    "type": "oauth_account",
                    "status": "active",
                    "healthy": True,
                    "ready": True,
                })
    except Exception:
        logger.debug("Failed to load OAuth accounts", exc_info=True)

    # 3. Build channel readiness from adapter registry + integrations
    channels: list[dict] = []
    try:
        from packages.core.services.channels.base import registered_channel_types
        ready_providers = {c["provider"] for c in integrations if c.get("ready")}

        for ct in registered_channel_types():
            if ct == "internal_chat":
                continue
            required_provider = _CHANNEL_TO_PROVIDER.get(ct)
            ch: dict = {
                "key": ct,
                "name": _CHANNEL_LABELS.get(ct, ct),
            }
            if ct in coming_soon_channels():
                ch["ready"] = False
                ch["coming_soon"] = True
                ch["needs_integration"] = False
            elif required_provider is None:
                ch["ready"] = True
                ch["needs_integration"] = False
            else:
                ch["ready"] = required_provider in ready_providers
                ch["needs_integration"] = required_provider not in ready_providers
                ch["required_provider"] = required_provider
            channels.append(ch)
    except Exception:
        logger.debug("Failed to build channel readiness", exc_info=True)

    # 4. Build MCP server catalog with coming-soon flags
    mcp_servers: list[dict] = []
    try:
        from packages.core.models.mcp import MCPServer
        server_rows = list((await db.execute(
            select(MCPServer).where(MCPServer.status == "active")
        )).scalars().all())

        for s in server_rows:
            coming_soon = s.server_key in coming_soon_servers()
            mcp_servers.append({
                "key": s.server_key,
                "name": s.name,
                "description": s.description,
                "auth_type": s.auth_type,
                "coming_soon": coming_soon,
                "available": not coming_soon,
            })
    except Exception:
        logger.debug("Failed to load MCP servers", exc_info=True)

    return {
        "integrations": integrations,
        "channels": channels,
        "mcp_servers": mcp_servers,
    }
