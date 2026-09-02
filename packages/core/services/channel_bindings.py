from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import Workspace
from packages.core.services.agent_subscription_service import resolve_subscription

SUPPORTED_CHANNEL_LANGUAGES = {"en", "zh", "es", "de"}

CHANNEL_BINDING_SNAPSHOT_FIELDS = (
    "entity_id", "workspace_id", "type", "name", "agent_id",
    "agent_subscription_id", "config", "status", "user_id",
)


def snapshot_channel_binding(binding: Channel) -> dict[str, Any]:
    """JSON-safe routing state only; never copy the account's credentials."""
    return {
        **{field: deepcopy(getattr(binding, field)) for field in CHANNEL_BINDING_SNAPSHOT_FIELDS},
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


def preserve_channel_binding_route_order(binding: Channel, updated_at: str | None) -> None:
    """Blueprint metadata edits must not elect a new shared inbound default.

    Runtime selects by updated_at DESC, id DESC (including legacy NULL timestamps).
    Explicitly write the old value to suppress TimestampMixin's onupdate,
    even when SQLAlchemy sees an assignment equal to the persisted value.
    """
    binding.updated_at = datetime.fromisoformat(updated_at) if updated_at else None
    flag_modified(binding, "updated_at")


@dataclass(frozen=True)
class ChannelBindingScope:
    binding: Channel
    agent_id: str | None
    workspace_id: str | None
    agent_subscription_id: str | None


def normalize_channel_language(value: Any) -> str:
    base = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return base if base in SUPPORTED_CHANNEL_LANGUAGES else "en"


def channel_runtime_config(
    cc: ChannelConfig,
    binding: Channel | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = dict(cc.config or {})
    if binding:
        cfg.update({
            key: value
            for key, value in dict(binding.config or {}).items()
            if key != "channel_config_id"
        })
    cfg["language"] = normalize_channel_language(cfg.get("language") or cfg.get("locale"))
    return cfg


async def load_channel_config(
    db: AsyncSession,
    channel_config_id: str,
) -> ChannelConfig | None:
    res = await db.execute(
        select(ChannelConfig)
        .where(ChannelConfig.id == channel_config_id)
        .execution_options(populate_existing=True)
    )
    return res.scalar_one_or_none()


async def load_channel_binding_for_config(
    db: AsyncSession,
    cc: ChannelConfig,
) -> Channel | None:
    """Find the active Channel row that binds this config to an agent."""
    direct = await db.execute(
        select(Channel).where(
            Channel.entity_id == cc.entity_id,
            Channel.type == cc.channel_type,
            Channel.status == "active",
            Channel.config["channel_config_id"].astext == cc.id,
        ).order_by(desc(Channel.updated_at), desc(Channel.id)).limit(1)
    )
    row = direct.scalar_one_or_none()
    if row:
        return row

    return None


async def load_channel_binding_scopes_for_config(
    db: AsyncSession,
    cc: ChannelConfig,
) -> list[ChannelBindingScope]:
    """Resolve every active binding through the runtime subscription contract."""
    rows = (await db.execute(
        select(Channel).where(
            Channel.entity_id == cc.entity_id,
            Channel.type == cc.channel_type,
            Channel.status == "active",
            Channel.config["channel_config_id"].astext == cc.id,
        ).order_by(desc(Channel.updated_at), desc(Channel.id))
    )).scalars().all()
    scopes: list[ChannelBindingScope] = []
    for binding in rows:
        if cc.owner_user_id and binding.user_id != cc.owner_user_id:
            continue
        subscription = await resolve_subscription(db, binding=binding)
        scopes.append(ChannelBindingScope(
            binding=binding,
            agent_id=subscription.agent_id,
            workspace_id=subscription.workspace_id,
            agent_subscription_id=subscription.id,
        ))
    return scopes


async def resolve_unique_channel_binding_scope(
    db: AsyncSession,
    cc: ChannelConfig,
    *,
    workspace_id: str | None = None,
) -> ChannelBindingScope | None:
    """Return one callable binding, failing explicitly when routing is ambiguous."""
    scopes = await load_channel_binding_scopes_for_config(db, cc)
    callable_scopes = [scope for scope in scopes if scope.agent_id]
    requested_workspace_id = str(workspace_id or "").strip() or None
    if requested_workspace_id:
        callable_scopes = [
            scope
            for scope in callable_scopes
            if scope.workspace_id == requested_workspace_id
        ]
    if len(callable_scopes) > 1:
        raise ValueError("Channel has ambiguous active Agent bindings")
    return callable_scopes[0] if callable_scopes else None


async def load_channel_binding_by_id_for_config(
    db: AsyncSession,
    cc: ChannelConfig,
    binding_id: str,
) -> Channel | None:
    """Load one persisted binding while revalidating its config and owner scope."""
    binding = (await db.execute(
        select(Channel).where(
            Channel.id == binding_id,
            Channel.entity_id == cc.entity_id,
            Channel.type == cc.channel_type,
            Channel.status == "active",
            Channel.config["channel_config_id"].astext == cc.id,
        )
    )).scalar_one_or_none()
    if binding is None:
        return None
    if cc.owner_user_id and binding.user_id != cc.owner_user_id:
        return None
    return binding


async def load_slack_channel_bindings_for_config(
    db: AsyncSession,
    cc: ChannelConfig,
) -> list[Channel]:
    """Load every active Slack binding so ambiguity cannot be hidden by sort order."""
    rows = await db.execute(
        select(Channel).where(
            Channel.entity_id == cc.entity_id,
            Channel.type == "slack",
            Channel.status == "active",
            Channel.config["channel_config_id"].astext == cc.id,
        ).order_by(desc(Channel.updated_at), desc(Channel.id))
    )
    return list(rows.scalars().all())


async def channel_workspace_is_routable(
    db: AsyncSession,
    workspace_id: str | None,
    *,
    entity_id: str,
) -> bool:
    """Fail closed for foreign, deleted, paused, or incomplete workspaces."""

    if not workspace_id:
        return True
    workspace = (await db.execute(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return bool(
        workspace
        and workspace.entity_id == entity_id
        and workspace.deleted_at is None
        and workspace.status == "active"
    )


async def resolve_public_webchat_channel_by_token(
    db: AsyncSession,
    token: str,
) -> tuple[ChannelConfig | None, Channel | None]:
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.channel_type == "webchat",
            ChannelConfig.status == "active",
            ChannelConfig.config["public_token"].astext == token,
        )
    )
    cc = result.scalar_one_or_none()
    if not cc:
        return None, None
    if not await channel_workspace_is_routable(
        db,
        cc.workspace_id,
        entity_id=cc.entity_id,
    ):
        return None, None

    binding = (await db.execute(
        select(Channel).where(
            Channel.entity_id == cc.entity_id,
            Channel.type == "webchat",
            Channel.config["channel_config_id"].astext == cc.id,
            Channel.status == "active",
        )
    )).scalar_one_or_none()
    if (
        binding
        and binding.workspace_id != cc.workspace_id
        and not await channel_workspace_is_routable(
            db,
            binding.workspace_id,
            entity_id=cc.entity_id,
        )
    ):
        return None, None
    return cc, binding
