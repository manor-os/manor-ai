"""Route a Workspace's declared channels to the accounts that serve them.

Create Workspace finalize routes the channels whose integration is already
connected and flags the rest. Connecting that integration later routes the
flagged channels the same way, so inbound messages reach the Workspace without
the user attaching the account by hand. Blueprint installs share this path for
channel requirements that had no account at install time.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.provider_keys import canonical_provider_key

logger = logging.getLogger(__name__)


def new_workspace_channel_config(
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str,
    role: str,
    block: dict[str, Any],
) -> ChannelConfig:
    """The Workspace-scoped routing account finalize creates for a channel."""
    channel_type = str(block.get("channel_type") or "").strip()
    config = {
        "role": role,
        "purpose": block.get("purpose", ""),
        "login_required": bool(block.get("login_required", False)),
        "linked_service_key": block.get("linked_service_key") or "",
        "notes": block.get("notes", ""),
    }
    # Webchat channels get a public access token for QR/link sharing.
    if channel_type == "webchat":
        config["public_token"] = secrets.token_urlsafe(24)
    return ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user_id if channel_type == "webchat" else None,
        workspace_id=workspace_id,
        channel_type=channel_type,
        provider=block.get("provider") or channel_type,
        name=block.get("name") or f"{role}: {channel_type}",
        config=config,
    )


def new_workspace_channel_binding(
    channel_config: ChannelConfig,
    *,
    user_id: str | None,
    agent_id: str | None,
    agent_subscription_id: str | None,
    extra_config: dict[str, Any] | None = None,
) -> Channel:
    """The gateway's dispatch target binding a routing account to an agent."""
    config: dict[str, Any] = {"channel_config_id": channel_config.id}
    public_token = (channel_config.config or {}).get("public_token")
    if public_token:
        config["public_token"] = public_token
    config.update(extra_config or {})
    return Channel(
        id=generate_ulid(),
        entity_id=channel_config.entity_id,
        user_id=user_id if channel_config.channel_type == "webchat" else None,
        workspace_id=channel_config.workspace_id,
        type=channel_config.channel_type,
        name=channel_config.channel_type,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        config=config,
        status="active",
    )


async def route_declared_channels_after_connect(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    provider: str,
    credential_source_id: str,
) -> list[str]:
    """Route unrouted channels of the user's Workspaces to a new connection.

    A connection that produced a channel account (Slack, Telegram, ...) is
    attached to the declared channel, as a Blueprint install binds a selected
    account. A connection with no channel account model (Gmail on an email
    channel) gets the routing account finalize would have created. Only
    Workspaces the connecting user owns are touched. Returns the Workspace ids
    that were routed; a failure in one Workspace never fails the connection.
    """
    provider_key = canonical_provider_key(provider)
    if not provider_key or not user_id:
        return []
    from packages.core.services.workspace_access import owned_workspace_ids_for_user

    account_channel_types = set((await db.execute(
        select(ChannelConfig.channel_type).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == user_id,
            ChannelConfig.credential_source_id == credential_source_id,
            ChannelConfig.status == "active",
        )
    )).scalars().all())
    workspaces = list((await db.execute(
        select(Workspace).where(
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
            Workspace.status == "active",
        ).order_by(Workspace.created_at, Workspace.id)
    )).scalars().all())
    owned = await owned_workspace_ids_for_user(
        db, workspace_ids={workspace.id for workspace in workspaces}, user_id=user_id,
    )
    routed: list[str] = []
    for workspace in workspaces:
        if workspace.id not in owned:
            continue
        try:
            async with db.begin_nested():
                if await _route_workspace_channels(
                    db,
                    workspace=workspace,
                    user_id=user_id,
                    provider_key=provider_key,
                    account_channel_types=account_channel_types,
                ):
                    routed.append(workspace.id)
        except Exception:
            logger.exception(
                "Could not route %s channels of workspace %s after connect",
                provider_key, workspace.id,
            )
    return routed


async def _route_workspace_channels(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user_id: str,
    provider_key: str,
    account_channel_types: set[str],
) -> bool:
    from packages.core.blueprints.installer import InstallError, _bind_blueprint_channel_configs
    from packages.core.blueprints.setup_preflight import (
        BlueprintSetupPreflightFactory,
        blueprint_channel_requirement_key,
    )
    from packages.core.services.workspace_integration_scope import disabled_workspace_providers
    from packages.core.services.workspace_readiness import (
        BUILT_IN_CHANNEL_TYPES,
        channel_type_has_account_model,
        list_configured_workspace_channels,
        workspace_declared_channels,
    )

    disabled = disabled_workspace_providers(workspace.operating_model)
    specs = []
    for spec in workspace_declared_channels(workspace):
        channel_type = str(spec.get("channel_type") or "").strip()
        spec_provider = canonical_provider_key(spec.get("provider") or channel_type)
        if (
            channel_type
            and channel_type not in BUILT_IN_CHANNEL_TYPES
            and channel_type_has_account_model(channel_type)
            and spec_provider not in disabled
            and (spec_provider == provider_key or channel_type in account_channel_types)
        ):
            specs.append({**spec, "channel_type": channel_type})
    if not specs:
        return False

    preflight = await BlueprintSetupPreflightFactory.from_contract(
        db,
        contract={"channels": specs},
        entity_id=workspace.entity_id,
        user_id=user_id,
        workspace_id=workspace.id,
    )
    states = {state.requirement_key: state for state in preflight.requirements}
    attached = {
        channel.get("channel_config_id")
        for channel in await list_configured_workspace_channels(db, workspace)
    }
    routed = False
    for index, spec in enumerate(specs):
        state = states.get(blueprint_channel_requirement_key(index, spec))
        if state is None or (state.resource_id and state.resource_id in attached):
            continue
        if state.resource_id:
            try:
                routed |= bool(await _bind_blueprint_channel_configs(
                    db,
                    workspace=workspace,
                    channel_requirements=[spec],
                    selected_channel_config_ids={
                        blueprint_channel_requirement_key(0, spec): state.resource_id,
                    },
                    user_id=user_id,
                ))
            except InstallError as exc:
                # The account cannot serve this route (its service is gone, or
                # it is bound elsewhere); the requirement keeps asking.
                logger.info("Left %s channel of workspace %s unrouted: %s", spec["channel_type"], workspace.id, exc)
        elif not account_channel_types and canonical_provider_key(
            spec.get("provider") or spec["channel_type"]
        ) == provider_key:
            routed |= await _create_routing_channel(db, workspace=workspace, spec=spec)
    return routed


async def _create_routing_channel(
    db: AsyncSession,
    *,
    workspace: Workspace,
    spec: dict[str, Any],
) -> bool:
    """Create finalize's routing account unless the Workspace already has one.

    An existing unbound routing account means its binding was removed on
    purpose, so it is not recreated behind the user's back.
    """
    channel_type = spec["channel_type"]
    provider = str(spec.get("provider") or channel_type)
    existing = (await db.execute(
        select(ChannelConfig.id).where(
            ChannelConfig.entity_id == workspace.entity_id,
            ChannelConfig.workspace_id == workspace.id,
            ChannelConfig.channel_type == channel_type,
            ChannelConfig.provider == provider,
        ).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return False
    subscriptions = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        ).order_by(AgentSubscription.created_at, AgentSubscription.id)
    )).scalars().all())
    linked_service_key = str(spec.get("linked_service_key") or "").strip()
    subscription = next(
        (sub for sub in subscriptions if linked_service_key and sub.service_key == linked_service_key),
        subscriptions[0] if subscriptions else None,
    )
    channel_config = new_workspace_channel_config(
        entity_id=workspace.entity_id,
        user_id=None,
        workspace_id=workspace.id,
        role=str(spec.get("role") or "channel"),
        block=spec,
    )
    db.add(channel_config)
    await db.flush()
    requirement_key = spec.get("blueprint_requirement_key")
    db.add(new_workspace_channel_binding(
        channel_config,
        user_id=None,
        agent_id=subscription.agent_id if subscription else None,
        agent_subscription_id=subscription.id if subscription else None,
        extra_config={"blueprint_requirement_key": requirement_key} if requirement_key else None,
    ))
    await db.flush()
    return True
