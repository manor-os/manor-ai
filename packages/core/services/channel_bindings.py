from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel

SUPPORTED_CHANNEL_LANGUAGES = {"en", "zh", "es", "de"}


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
        select(ChannelConfig).where(ChannelConfig.id == channel_config_id)
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
        ).order_by(desc(Channel.updated_at)).limit(1)
    )
    row = direct.scalar_one_or_none()
    if row:
        return row

    integration_id = (cc.config or {}).get("integration_id")
    if integration_id:
        predicates = [
            Channel.entity_id == cc.entity_id,
            Channel.type == cc.channel_type,
            Channel.status == "active",
            Channel.config["integration_id"].astext == integration_id,
        ]
        if cc.workspace_id:
            predicates.append(Channel.workspace_id == cc.workspace_id)
        fallback = await db.execute(
            select(Channel).where(*predicates).order_by(desc(Channel.updated_at)).limit(1)
        )
        row = fallback.scalar_one_or_none()
        if row:
            return row

    predicates = [
        Channel.entity_id == cc.entity_id,
        Channel.type == cc.channel_type,
        Channel.status == "active",
    ]
    if cc.workspace_id:
        predicates.append(Channel.workspace_id == cc.workspace_id)
    else:
        predicates.append(Channel.workspace_id.is_(None))
    broad = await db.execute(
        select(Channel).where(*predicates).order_by(desc(Channel.updated_at)).limit(1)
    )
    return broad.scalar_one_or_none()


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

    binding = (await db.execute(
        select(Channel).where(
            Channel.entity_id == cc.entity_id,
            Channel.type == "webchat",
            Channel.config["channel_config_id"].astext == cc.id,
            Channel.status == "active",
        )
    )).scalar_one_or_none()
    return cc, binding
