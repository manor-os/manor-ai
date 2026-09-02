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

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.credentials import Requester, get_credential_service
from packages.core.integrations.registry import register_integration
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Integration, Channel
from packages.core.services.integration_access import resolve_integration_access
from packages.core.services.email_credentials import normalize_email_credentials
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_references,
)


class IntegrationProviderImmutableError(ValueError):
    """Raised when a caller tries to change a credential-bound provider."""


class IntegrationCredentialConflictError(ValueError):
    """Raised when a credential update was based on a stale secret snapshot."""


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

async def list_integrations(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
) -> list[Integration]:
    """List only connections the actor owns or is explicitly allowed to use."""
    result = await db.execute(
        select(Integration)
        .where(Integration.entity_id == entity_id)
        .order_by(Integration.created_at.desc())
    )
    rows = list(result.scalars().all())
    visible: list[Integration] = []
    for row in rows:
        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=row.id,
            entity_id=entity_id,
            user_id=user_id,
            action="view",
        )
        if decision.allowed:
            visible.append(row)
    return visible


async def get_integration(
    db: AsyncSession,
    integration_id: str,
    entity_id: str,
    user_id: str,
    *,
    action: str = "view",
) -> Optional[Integration]:
    result = await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        return None
    decision = await resolve_integration_access(
        db,
        kind="integration",
        connection_id=integration.id,
        entity_id=entity_id,
        user_id=user_id,
        action=action,
    )
    return integration if decision.allowed else None


async def create_integration(
    db: AsyncSession, entity_id: str, provider: str, *,
    config: dict | None = None, credentials: dict | None = None,
    created_by_user_id: str | None = None,
    owner_user_id: str | None = None,
) -> Integration:
    provider = canonical_provider_key(provider)
    register_integration(provider)
    resolved_config = dict(config or {})
    # Nango pointers are issued and persisted only by the dedicated OAuth sync
    # flow. Generic CRUD must never mint a runtime credential pointer.
    resolved_config.pop("nango", None)
    owner_user_id = owner_user_id or created_by_user_id
    resolved_config.pop("is_default", None)
    resolved_config["is_default"] = False
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        created_by_user_id=created_by_user_id,
        provider=provider,
        config=resolved_config,
        credentials={},
    )
    if credentials:
        if provider == "email":
            credentials = normalize_email_credentials(credentials)
        # store_integration sets credential_ref + credential_scheme and
        # leaves the legacy JSONB empty. Needs the row to have an id +
        # entity + provider populated (above) so the context is stable.
        get_credential_service().store_integration(integration, credentials)
    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        lock_runtime_integration_account_scope,
        normalize_runtime_integration_account_defaults,
    )

    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=provider,
    )
    db.add(integration)
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=provider,
    )
    await db.refresh(integration)
    return integration


async def update_integration(
    db: AsyncSession,
    integration_id: str,
    entity_id: str,
    user_id: str,
    **kwargs,
) -> Optional[Integration]:
    integration = await get_integration(
        db,
        integration_id,
        entity_id,
        user_id,
        action="manage",
    )
    if not integration:
        return None
    original_provider = canonical_provider_key(integration.provider)
    requested_provider = kwargs.pop("provider", None)
    if (
        requested_provider is not None
        and canonical_provider_key(requested_provider) != original_provider
    ):
        raise IntegrationProviderImmutableError(
            "An existing integration's provider cannot be changed; create a new integration instead."
        )
    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        lock_runtime_integration_account_scope,
        normalize_runtime_integration_account_defaults,
    )

    owner_user_id = integration.owner_user_id or user_id
    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=original_provider,
    )
    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.owner_user_id == owner_user_id,
            Integration.provider.in_(provider_key_aliases(original_provider)),
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if integration is None:
        return None
    # Pull credentials out of kwargs — those route through the vault.
    new_creds = kwargs.pop("credentials", None)
    expected_credentials = kwargs.pop("expected_credentials", None)
    if new_creds is not None and expected_credentials is not None:
        current_credentials = get_credential_service().lease_integration(
            integration,
            requester=Requester(kind="user", id=user_id),
            reason="integration_update_compare_credentials",
        )
        if current_credentials != expected_credentials:
            raise IntegrationCredentialConflictError(
                "Integration credentials changed while this update was being prepared."
            )
    for key, value in kwargs.items():
        if value is not None and hasattr(integration, key):
            if key == "config" and isinstance(value, dict):
                value = dict(value)
                value.pop("nango", None)
                existing_config = (
                    integration.config
                    if isinstance(integration.config, dict)
                    else {}
                )
                server_nango = existing_config.get("nango")
                if isinstance(server_nango, dict):
                    value["nango"] = dict(server_nango)
                value.pop("is_default", None)
                value["is_default"] = bool(
                    existing_config.get("is_default")
                )
            setattr(integration, key, value)
    if new_creds is not None:
        if integration.provider == "email":
            new_creds = normalize_email_credentials(new_creds)
        get_credential_service().store_integration(integration, new_creds)
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=original_provider,
    )
    await db.refresh(integration)
    return integration


async def delete_integration(
    db: AsyncSession, integration_id: str, entity_id: str, user_id: str,
) -> bool:
    integration = await get_integration(
        db,
        integration_id,
        entity_id,
        user_id,
        action="manage",
    )
    if not integration:
        return False
    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        lock_runtime_integration_account_scope,
        normalize_runtime_integration_account_defaults,
    )

    owner_user_id = integration.owner_user_id or user_id
    provider = integration.provider
    await lock_runtime_integration_account_scope(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=provider,
    )
    integration = (await db.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.entity_id == entity_id,
            Integration.owner_user_id == owner_user_id,
            Integration.provider.in_(provider_key_aliases(provider)),
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not integration:
        return False
    await db.delete(integration)
    await db.flush()
    await normalize_runtime_integration_account_defaults(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=owner_user_id,
        entity_id=entity_id,
        provider=provider,
    )
    return True


async def list_accounts_by_provider(
    db: AsyncSession, entity_id: str, user_id: str, provider: str,
) -> list[Integration]:
    """Every Integration row for (entity, provider) — ordered with the
    default first, then newest.

    Used to show the "entity_accounts" list on an integration card when a
    provider supports multiple accounts (email inboxes, WhatsApp senders,
    WeChat bots, etc.).
    """
    aliases = provider_key_aliases(provider)
    rows = (await db.execute(
        select(Integration)
        .where(
            Integration.entity_id == entity_id,
            Integration.provider.in_(aliases),
            Integration.status == "active",
        )
        .order_by(Integration.created_at.desc())
    )).scalars().all()
    visible: list[Integration] = []
    for row in rows:
        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=row.id,
            entity_id=entity_id,
            user_id=user_id,
            action="view",
        )
        if decision.allowed:
            visible.append(row)

    # Stable: default first, then creation time desc
    return sorted(
        visible,
        key=lambda r: (0 if (r.config or {}).get("is_default") else 1, -_ts(r.created_at)),
    )


def _ts(dt) -> float:
    try:
        return dt.timestamp() if dt else 0.0
    except Exception:
        return 0.0


async def list_channel_bindings(
    db: AsyncSession, entity_id: str, user_id: str,
) -> list[dict]:
    """The current owner's ChannelConfigs, joined with their Channel
    binding (if any) so the UI can render "which agent is this channel
    routing to".

    Returns a flat list of dicts — ChannelConfig fields + bound_channel_id,
    bound_agent_id, agent_name, last_inbound_at, last_outbound_at.
    """
    from sqlalchemy import func
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.models.document import Channel
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.models.user import OAuthAccount

    ccs = (await db.execute(
        select(ChannelConfig)
        .where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == user_id,
            ChannelConfig.status == "active",
        )
        .order_by(ChannelConfig.channel_type.asc(), ChannelConfig.created_at.asc())
    )).scalars().all()

    if not ccs:
        return []

    oauth_source_ids = {
        cc.credential_source_id
        for cc in ccs
        if cc.credential_source_kind == "oauth_account" and cc.credential_source_id
    }
    oauth_sources = {}
    if oauth_source_ids:
        oauth_sources = {
            row.id: row
            for row in (await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.id.in_(oauth_source_ids),
                    OAuthAccount.user_id == user_id,
                )
            )).scalars().all()
        }
        ccs = [
            cc for cc in ccs
            if cc.credential_source_kind != "oauth_account"
            or cc.credential_source_id in oauth_sources
        ]

    if not ccs:
        return []

    channels = (await db.execute(
        select(Channel)
        .where(
            Channel.entity_id == entity_id,
            Channel.type.in_([c.channel_type for c in ccs]),
            Channel.status == "active",
        )
    )).scalars().all()

    # Map ChannelConfig.id → Channel binding
    binding_groups: dict[str, list[Channel]] = {}
    for ch in channels:
        cfg = ch.config or {}
        # A binding must point directly to its ChannelConfig. Legacy broad
        # same-type fallback could route one user's inbound message to another
        # user's agent after connection sharing was introduced.
        cc_id = cfg.get("channel_config_id")
        if cc_id:
            binding_groups.setdefault(cc_id, []).append(ch)
    bindings_by_cc = {
        cc_id: rows[0]
        for cc_id, rows in binding_groups.items()
        if rows[0].type != "whatsapp" or len(rows) == 1
    }

    agent_ids = {ch.agent_id for ch in channels if ch.agent_id}
    agents_by_id: dict[str, Agent] = {}
    if agent_ids:
        agents = (await db.execute(
            select(Agent).where(Agent.id.in_(agent_ids))
        )).scalars().all()
        agents_by_id = {a.id: a for a in agents}

    subscription_ids = {
        ch.agent_subscription_id for ch in channels if ch.agent_subscription_id
    }
    subscriptions_by_id: dict[str, AgentSubscription] = {}
    if subscription_ids:
        subscriptions = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.id.in_(subscription_ids),
                AgentSubscription.entity_id == entity_id,
            )
        )).scalars().all()
        subscriptions_by_id = {
            subscription.id: subscription for subscription in subscriptions
        }

    workspace_ids = {ch.workspace_id for ch in channels if ch.workspace_id}
    workspaces_by_id: dict[str, Workspace] = {}
    if workspace_ids:
        workspaces = (await db.execute(
            select(Workspace).where(
                Workspace.id.in_(workspace_ids),
                Workspace.entity_id == entity_id,
            )
        )).scalars().all()
        workspaces_by_id = {workspace.id: workspace for workspace in workspaces}

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
        oauth_profile = (
            (oauth_sources.get(cc.credential_source_id).profile or {})
            if cc.credential_source_kind == "oauth_account"
            and cc.credential_source_id in oauth_sources
            else {}
        )
        source_display_name = (
            oauth_profile.get("team_name")
            or oauth_profile.get("team")
            or oauth_profile.get("display_name")
            or oauth_profile.get("name")
            or oauth_profile.get("email")
            or oauth_profile.get("user")
        )
        display_name = (
            source_display_name
            or cfg.get("display_name")
            or cfg.get("name")
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
        subscription = (
            subscriptions_by_id.get(ch.agent_subscription_id)
            if ch and ch.agent_subscription_id
            else None
        )
        workspace = (
            workspaces_by_id.get(ch.workspace_id)
            if ch and ch.workspace_id
            else None
        )
        last_in = last_activity.get((cc.id, "inbound"))
        last_out = last_activity.get((cc.id, "outbound"))
        out.append({
            "channel_config_id": cc.id,
            "channel_type": cc.channel_type,
            "provider": cc.provider,
            "name": cfg.get("name") or cc.name,
            "display_name": str(display_name),
            "owner_user_id": cc.owner_user_id,
            "credential_source_kind": cc.credential_source_kind,
            "credential_source_id": cc.credential_source_id,
            "can_manage": True,
            "status": cc.status,
            "bound_channel_id": ch.id if ch else None,
            "bound_agent_id": ch.agent_id if ch else None,
            "bound_agent_subscription_id": subscription.id if subscription else None,
            "bound_workspace_id": workspace.id if workspace else None,
            "agent_name": agent.name if agent else None,
            "workspace_name": workspace.name if workspace else None,
            "binding_status": ch.status if ch else None,
            "last_inbound_at": last_in.isoformat() if last_in else None,
            "last_outbound_at": last_out.isoformat() if last_out else None,
        })
    return out


async def upsert_channel_binding(
    db: AsyncSession, *, entity_id: str, channel_config_id: str,
    user_id: str,
    agent_id: Optional[str],
    agent_subscription_id: Optional[str] = None,
    user_role: Optional[str] = None,
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
            ChannelConfig.owner_user_id == user_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if not cc:
        raise ValueError("Channel config not found")

    binding_query = select(Channel).where(
        Channel.entity_id == entity_id,
        Channel.type == cc.channel_type,
        Channel.config["channel_config_id"].astext == cc.id,
    )
    if cc.channel_type == "whatsapp":
        binding_query = binding_query.where(Channel.status == "active")
        existing_rows = (await db.execute(
            binding_query.order_by(Channel.id).with_for_update()
        )).scalars().all()
        if len(existing_rows) > 1:
            raise ValueError("WhatsApp channel has ambiguous active bindings")
        existing = existing_rows[0] if existing_rows else None
    else:
        existing = (await db.execute(
            binding_query.with_for_update()
        )).scalar_one_or_none()

    resolved_subscription = None
    if cc.channel_type == "whatsapp" and not agent_subscription_id:
        raise ValueError("WhatsApp binding requires agent_subscription_id")
    if agent_subscription_id:
        from packages.core.services.agent_subscription_service import (
            resolve_exact_binding_subscription,
        )

        resolved_subscription = await resolve_exact_binding_subscription(
            db,
            subscription_id=agent_subscription_id,
            entity_id=entity_id,
            actor_user_id=user_id,
            actor_role=user_role,
        )
        if agent_id and agent_id != resolved_subscription.agent_id:
            raise ValueError("agent_id does not match agent_subscription_id")
        agent_id = resolved_subscription.agent_id
    if (
        agent_id
        and (existing is None or agent_id != existing.agent_id)
        and not is_master_agent(agent_id)
    ):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(agent_id,),
        )

    if existing:
        binding_scope_changed = (
            cc.channel_type == "twilio_voice"
            and (
                existing.agent_id != agent_id
                or existing.agent_subscription_id
                != (resolved_subscription.id if resolved_subscription else None)
                or existing.workspace_id
                != (
                    resolved_subscription.workspace_id
                    if resolved_subscription
                    else None
                )
                or existing.status != "active"
            )
        )
        if binding_scope_changed:
            await _cancel_unconnected_twilio_channel_binding(
                db,
                channel=existing,
                reason="Twilio Voice Agent binding changed before the call connected.",
            )
        existing.agent_id = agent_id
        existing.agent_subscription_id = (
            resolved_subscription.id if resolved_subscription else None
        )
        existing.workspace_id = (
            resolved_subscription.workspace_id if resolved_subscription else None
        )
        existing.status = "active"
        existing.user_id = cc.owner_user_id
        await db.flush()
        return existing

    row = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=cc.owner_user_id,
        workspace_id=(
            resolved_subscription.workspace_id if resolved_subscription else None
        ),
        type=cc.channel_type,
        name=(cc.config or {}).get("name") or cc.name or cc.channel_type,
        agent_id=agent_id,
        agent_subscription_id=(
            resolved_subscription.id if resolved_subscription else None
        ),
        config={"channel_config_id": cc.id},
        status="active",
    )
    db.add(row)
    await db.flush()
    return row


async def delete_channel_binding(
    db: AsyncSession, entity_id: str, user_id: str, channel_id: str,
) -> bool:
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    row = (await db.execute(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not row:
        return False
    channel_config_id = (row.config or {}).get("channel_config_id")
    if not channel_config_id:
        return False
    config = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == channel_config_id,
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == user_id,
        )
    )).scalar_one_or_none()
    if not config:
        return False
    if config.channel_type == "twilio_voice":
        await _cancel_unconnected_twilio_channel_binding(
            db,
            channel=row,
            reason="Twilio Voice Agent binding was removed before the call connected.",
        )
    await db.delete(row)
    await db.flush()
    return True


async def set_default_integration(
    db: AsyncSession, entity_id: str, user_id: str, integration_id: str,
) -> Optional[Integration]:
    """Mark one Integration as the default for its (entity, provider)
    pair — agents fall back to it when no account is explicitly
    selected. Flips the flag off on every sibling row first.
    """
    target = await get_integration(
        db,
        integration_id,
        entity_id,
        user_id,
        action="manage",
    )
    if not target:
        return None

    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        set_default_runtime_integration_account,
    )

    updated = await set_default_runtime_integration_account(
        db,
        kind=IntegrationAccountKind.INTEGRATION,
        user_id=user_id,
        entity_id=entity_id,
        provider=target.provider,
        account_id=integration_id,
    )
    if not updated:
        return None
    await db.refresh(target)
    return target


# ── Channels ──

def _channel_config_id(config: dict | None) -> str | None:
    channel_config_id = (config or {}).get("channel_config_id")
    if not isinstance(channel_config_id, str):
        return None
    return channel_config_id.strip() or None


async def _cancel_unconnected_twilio_channel_binding(
    db: AsyncSession,
    *,
    channel: Channel,
    reason: str,
) -> None:
    channel_config_id = _channel_config_id(channel.config)
    if channel.type != "twilio_voice" or not channel_config_id:
        return
    from packages.core.services.voice.call_sessions import (
        cancel_unconnected_call_sessions_for_binding,
    )

    await cancel_unconnected_call_sessions_for_binding(
        db,
        channel_config_id=channel_config_id,
        channel_binding_id=channel.id,
        reason=reason,
    )


def _channel_visible_to_user(user_id: str):
    """Keep user-owned channel bindings private while retaining legacy rows."""
    config_id = Channel.config["channel_config_id"].astext
    return or_(
        ChannelConfig.owner_user_id == user_id,
        and_(ChannelConfig.id.is_(None), Channel.user_id == user_id),
        and_(
            ChannelConfig.id.is_(None),
            Channel.user_id.is_(None),
            config_id.is_(None),
        ),
    )


async def _validate_channel_config_owner(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    config: dict | None,
) -> None:
    channel_config_id = _channel_config_id(config)
    if not channel_config_id:
        return
    if not user_id:
        raise ValueError("A user-owned channel configuration requires an owner")
    result = await db.execute(
        select(ChannelConfig.id).where(
            ChannelConfig.id == channel_config_id,
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == user_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Channel configuration not found")

async def list_channels(
    db: AsyncSession, entity_id: str, user_id: str, *,
    workspace_id: str | None = None,
) -> list[Channel]:
    q = (
        select(Channel)
        .outerjoin(
            ChannelConfig,
            Channel.config["channel_config_id"].astext == ChannelConfig.id,
        )
        .where(Channel.entity_id == entity_id, _channel_visible_to_user(user_id))
    )
    if workspace_id is not None:
        q = q.where(Channel.workspace_id == workspace_id)
    q = q.order_by(Channel.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_channel(
    db: AsyncSession, channel_id: str, entity_id: str, user_id: str,
) -> Optional[Channel]:
    result = await db.execute(
        select(Channel)
        .outerjoin(
            ChannelConfig,
            Channel.config["channel_config_id"].astext == ChannelConfig.id,
        )
        .where(
            Channel.id == channel_id,
            Channel.entity_id == entity_id,
            _channel_visible_to_user(user_id),
        )
    )
    return result.scalar_one_or_none()


async def create_channel(
    db: AsyncSession, entity_id: str, type: str, *,
    name: str | None = None, user_id: str | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None, config: dict | None = None,
) -> Channel:
    await _validate_channel_config_owner(
        db, entity_id=entity_id, user_id=user_id, config=config,
    )
    if agent_id and not is_master_agent(agent_id):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(agent_id,),
        )
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
    db: AsyncSession, channel_id: str, entity_id: str, user_id: str, **kwargs,
) -> Optional[Channel]:
    channel = await get_channel(db, channel_id, entity_id, user_id)
    if not channel:
        return None
    if "config" in kwargs:
        await _validate_channel_config_owner(
            db, entity_id=entity_id, user_id=user_id, config=kwargs["config"],
        )
    next_agent_id = kwargs.get("agent_id")
    if (
        next_agent_id
        and next_agent_id != channel.agent_id
        and not is_master_agent(next_agent_id)
    ):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(next_agent_id,),
        )
    routing_changed = any(
        key in kwargs
        and kwargs[key] is not None
        and kwargs[key] != getattr(channel, key)
        for key in (
            "agent_id",
            "agent_subscription_id",
            "workspace_id",
            "type",
            "status",
        )
    ) or (
        "config" in kwargs
        and kwargs["config"] is not None
        and _channel_config_id(kwargs["config"])
        != _channel_config_id(channel.config)
    )
    if routing_changed:
        await _cancel_unconnected_twilio_channel_binding(
            db,
            channel=channel,
            reason="Twilio Voice Agent binding changed before the call connected.",
        )
    for key, value in kwargs.items():
        if value is not None and hasattr(channel, key):
            setattr(channel, key, value)
    await db.flush()
    await db.refresh(channel)
    return channel


async def delete_channel(
    db: AsyncSession, channel_id: str, entity_id: str, user_id: str,
) -> bool:
    channel = await get_channel(db, channel_id, entity_id, user_id)
    if not channel:
        return False
    await _cancel_unconnected_twilio_channel_binding(
        db,
        channel=channel,
        reason="Twilio Voice Agent binding was removed before the call connected.",
    )
    await db.delete(channel)
    await db.flush()
    return True


async def list_integration_channels(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    integration_id: str,
    *,
    workspace_id: str | None = None,
) -> list[Channel]:
    config_ids = list((await db.execute(
        select(ChannelConfig.id).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
        )
    )).scalars().all())
    bindings = [Channel.config["integration_id"].astext == integration_id]
    if config_ids:
        bindings.append(Channel.config["channel_config_id"].astext.in_(config_ids))

    q = (
        select(Channel)
        .outerjoin(
            ChannelConfig,
            Channel.config["channel_config_id"].astext == ChannelConfig.id,
        )
        .where(
            Channel.entity_id == entity_id,
            _channel_visible_to_user(user_id),
            or_(*bindings),
        )
    )
    if workspace_id is not None:
        q = q.where(Channel.workspace_id == workspace_id)
    result = await db.execute(q.order_by(Channel.created_at.desc()))
    return list(result.scalars().all())


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
    "whatsapp": "WhatsApp Business",
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
    *,
    user_id: str | None = None,
) -> dict:
    """Return integration status visible to an actor or the Entity setup flow.

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

    # 1. User-owned non-OAuth integrations (API keys, browser sessions, etc.)
    try:
        int_rows = list((await db.execute(
            select(Integration).where(Integration.entity_id == entity_id)
        )).scalars().all())

        for i in int_rows:
            if user_id:
                access = await resolve_integration_access(
                    db,
                    kind="integration",
                    connection_id=i.id,
                    entity_id=entity_id,
                    user_id=user_id,
                    action="view",
                )
                if not access.allowed:
                    continue
            health = (i.config or {}).get("last_health_check", {})
            wiring = health.get("wiring") if isinstance(health, dict) else None
            whatsapp_config = (
                (i.config or {}).get("whatsapp")
                if canonical_provider_key(i.provider) == "whatsapp"
                else None
            )
            whatsapp_ready = True
            if canonical_provider_key(i.provider) == "whatsapp":
                if isinstance(wiring, dict):
                    whatsapp_ready = wiring.get("ok") is True
                else:
                    whatsapp_ready = (
                        isinstance(whatsapp_config, dict)
                        and whatsapp_config.get("subscription_status") == "ready"
                    )
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
                    and whatsapp_ready
                    and health.get("ok") is not False
                ),
            })
    except Exception:
        logger.debug("Failed to load entity integrations", exc_info=True)

    # 2. User OAuth connections (personal tokens — e.g. Google, GitHub)
    try:
        from sqlalchemy import or_

        from packages.core.models.permission import (
            GrantStatus,
            ResourceGrant,
            ResourceType,
            SubjectType,
        )
        from packages.core.models.user import OAuthAccount, User

        if user_id:
            shared_oauth_ids = select(ResourceGrant.resource_id).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.resource_type == ResourceType.OAUTH_ACCOUNT,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id == user_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
            oauth_query = select(OAuthAccount).where(
                or_(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.id.in_(shared_oauth_ids),
                )
            )
        else:
            oauth_query = (
                select(OAuthAccount)
                .join(User, User.id == OAuthAccount.user_id)
                .where(User.entity_id == entity_id)
            )
        oauth_rows = list((await db.execute(oauth_query)).scalars().all())

        oauth_providers: set[str] = set()
        for o in oauth_rows:
            if user_id:
                access = await resolve_integration_access(
                    db,
                    kind="oauth_account",
                    connection_id=o.id,
                    entity_id=entity_id,
                    user_id=user_id,
                    action="view",
                )
                if not access.allowed:
                    continue
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
