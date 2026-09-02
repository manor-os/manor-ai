"""Configurable external delivery for task domain events.

In-app notifications are created inside the event transaction. This module is
for post-commit channels such as email and Slack so network failures never
roll back task state changes.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig
from packages.core.models.task import Task
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services.channel_credentials import lease_channel_config_credentials

logger = logging.getLogger(__name__)

DEFAULT_EXTERNAL_EVENTS = [
    "task.failed",
    "task.hitl_requested",
    "task.hitl_reminder",
]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def task_notification_policy_config(
    entity_settings: dict[str, Any] | None = None,
    workspace_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve entity/workspace task event delivery policy.

    Supported shape:
        settings.notification_policy.task_events.{email,external_chat}

    Workspace settings override entity settings. Empty config means no external
    delivery; in-app notification/webhook behavior is controlled elsewhere.
    """
    entity_policy = _as_dict(_as_dict(_as_dict(entity_settings).get("notification_policy")).get("task_events"))
    workspace_policy = _as_dict(_as_dict(_as_dict(workspace_settings).get("notification_policy")).get("task_events"))
    return _merge_dicts(entity_policy, workspace_policy)


def task_event_external_channel_enabled(
    policy: dict[str, Any],
    channel: str,
    event_type: str,
) -> bool:
    channel_policy = _as_dict(policy.get(channel))
    if channel_policy.get("enabled") is not True:
        return False

    event_overrides = _as_dict(_as_dict(policy.get("events")).get(event_type))
    if event_overrides.get(channel) is False:
        return False
    if event_overrides.get(channel) is True:
        return True

    events = _as_str_list(channel_policy.get("events"))
    if not events:
        events = list(DEFAULT_EXTERNAL_EVENTS)
    return event_type in set(events)


async def deliver_task_external_notifications(
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    completed_sinks: dict[str, Any] | None = None,
    mark_sink_completed: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, int]:
    """Deliver configured post-commit task notifications."""
    if not event_type.startswith("task."):
        return {"email": 0, "external_chat": 0}

    from packages.core.database import async_session

    async with async_session() as db:
        return await deliver_task_external_notifications_in_session(
            db,
            entity_id,
            event_type,
            payload or {},
            completed_sinks=completed_sinks,
            mark_sink_completed=mark_sink_completed,
        )


async def deliver_task_external_notifications_in_session(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    completed_sinks: dict[str, Any] | None = None,
    mark_sink_completed: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, int]:
    entity = (await db.execute(
        select(Entity).where(Entity.id == entity_id)
    )).scalar_one_or_none()
    if not entity:
        return {"email": 0, "external_chat": 0}

    task = await _load_task(db, entity_id, payload)
    workspace = await _load_workspace(db, entity_id, task)
    policy = task_notification_policy_config(
        entity.settings if entity else None,
        workspace.settings if workspace else None,
    )
    if not policy:
        return {"email": 0, "external_chat": 0}

    counts = {"email": 0, "external_chat": 0}
    completed = completed_sinks if isinstance(completed_sinks, dict) else {}
    if task_event_external_channel_enabled(policy, "email", event_type):
        counts["email"] = await _deliver_email(
            db,
            entity_id,
            event_type,
            payload,
            task,
            completed_sinks=completed,
            mark_sink_completed=mark_sink_completed,
        )
    if task_event_external_channel_enabled(policy, "external_chat", event_type):
        counts["external_chat"] = await _deliver_external_chat(
            db,
            entity_id,
            event_type,
            payload,
            task,
            _as_dict(policy.get("external_chat")),
            completed_sinks=completed,
            mark_sink_completed=mark_sink_completed,
        )
    return counts


async def _load_task(db: AsyncSession, entity_id: str, payload: dict[str, Any]) -> Task | None:
    task_id = payload.get("task_id")
    if not task_id:
        return None
    return (await db.execute(
        select(Task).where(Task.id == task_id, Task.entity_id == entity_id)
    )).scalar_one_or_none()


async def _load_workspace(db: AsyncSession, entity_id: str, task: Task | None) -> Workspace | None:
    if not task or not task.workspace_id:
        return None
    return (await db.execute(
        select(Workspace).where(
            Workspace.id == task.workspace_id,
            Workspace.entity_id == entity_id,
        )
    )).scalar_one_or_none()


async def _deliver_email(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any],
    task: Task | None,
    *,
    completed_sinks: dict[str, Any] | None = None,
    mark_sink_completed: Callable[[str], Awaitable[None]] | None = None,
) -> int:
    from packages.core.services.email_service import send_notification_email
    from packages.core.services.task_event_notifications import (
        task_event_message,
        task_event_recipient_users,
        task_event_title,
    )

    _task, users = await task_event_recipient_users(db, entity_id, event_type, payload)
    message = task_event_message(event_type, payload, task or _task)
    title = task_event_title(event_type)

    delivered = 0
    failures = 0
    completed = completed_sinks if isinstance(completed_sinks, dict) else {}
    seen: set[str] = set()
    for user in users:
        email = (user.email or "").strip()
        if not email or email.lower() in seen:
            continue
        normalized_email = email.lower()
        seen.add(normalized_email)
        sink_key = f"task_external.email:{normalized_email}"
        if completed.get(sink_key) is True:
            continue
        try:
            if await send_notification_email(email, title, message):
                delivered += 1
                if mark_sink_completed is not None:
                    await mark_sink_completed(sink_key)
                completed[sink_key] = True
            else:
                failures += 1
        except Exception:
            failures += 1
            logger.debug("task external email failed for user=%s", user.id, exc_info=True)
    if failures:
        raise RuntimeError(
            f"task external email failed for {failures} recipient(s)"
        )
    return delivered


async def _deliver_external_chat(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any],
    task: Task | None,
    policy: dict[str, Any],
    *,
    completed_sinks: dict[str, Any] | None = None,
    mark_sink_completed: Callable[[str], Awaitable[None]] | None = None,
) -> int:
    from packages.core.services.task_event_notifications import (
        task_event_message,
        task_event_title,
    )

    channel_types = _as_str_list(policy.get("channel_types")) or ["slack"]
    channel_ids = set(_as_str_list(policy.get("channel_config_ids")))
    # A task policy must name its intended connection. Falling back to every
    # active Entity channel would cross the user-owned integration boundary.
    if not channel_ids:
        raise RuntimeError(
            "task external chat target missing: channel_config_ids is required"
        )
    query = select(ChannelConfig).where(
        ChannelConfig.entity_id == entity_id,
        ChannelConfig.status == "active",
        ChannelConfig.channel_type.in_(channel_types),
        ChannelConfig.id.in_(channel_ids),
    )

    configs = list((await db.execute(query.order_by(ChannelConfig.workspace_id.desc().nullslast()))).scalars().all())
    if not configs:
        raise RuntimeError(
            "task external chat target missing: no active channel config found"
        )

    title = task_event_title(event_type)
    body = task_event_message(event_type, payload, task)
    text = f"*{title}*\n{body}"

    delivered = 0
    failures = 0
    completed = completed_sinks if isinstance(completed_sinks, dict) else {}
    for config in configs:
        sink_key = f"task_external.external_chat:{config.id}"
        if completed.get(sink_key) is True:
            continue
        try:
            sent = await _send_channel_message(config, text, policy)
            if sent:
                delivered += 1
                if mark_sink_completed is not None:
                    await mark_sink_completed(sink_key)
                completed[sink_key] = True
            else:
                failures += 1
        except Exception:
            failures += 1
            logger.debug(
                "task external chat failed for channel_config=%s",
                config.id,
                exc_info=True,
            )
    if failures:
        raise RuntimeError(
            f"task external chat failed for {failures} channel(s)"
        )
    return delivered


async def _send_channel_message(
    config: ChannelConfig,
    text: str,
    policy: dict[str, Any],
) -> bool:
    creds = await _channel_credentials(config)
    if config.channel_type == "slack":
        webhook_url = (
            str(policy.get("webhook_url") or "").strip()
            or str(_as_dict(config.config).get("webhook_url") or "").strip()
            or str(creds.get("webhook_url") or "").strip()
        )
        if webhook_url:
            return await _send_slack_webhook(webhook_url, text)
        target = _channel_target(config, creds, policy)
        bot_token = str(creds.get("bot_token") or os.getenv("SLACK_BOT_TOKEN", "")).strip()
        if target and bot_token:
            return await _send_slack_bot_message(bot_token, target, text)

    from packages.core.services.channels import get_adapter

    adapter = get_adapter(config.channel_type)
    if not adapter:
        return False
    target = _channel_target(config, creds, policy)
    if not target:
        logger.debug("task external chat: no target for channel_config=%s", config.id)
        return False
    await adapter.send_text(config, target, text)
    return True


async def _channel_credentials(config: ChannelConfig) -> dict[str, Any]:
    try:
        return await lease_channel_config_credentials(
            config,
            reason="task_event.external_notification",
        )
    except Exception:
        logger.debug("task external notification credential lease failed", exc_info=True)
        return {}


def _channel_target(
    config: ChannelConfig,
    credentials: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    config_dict = _as_dict(config.config)
    candidates = [
        policy.get("target"),
        policy.get("channel_id"),
        policy.get("slack_channel"),
        config_dict.get("notification_target"),
        config_dict.get("default_target"),
        config_dict.get("channel_id"),
        credentials.get("notification_target"),
        credentials.get("default_target"),
        credentials.get("default_channel"),
        credentials.get("channel_id"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _send_slack_webhook(webhook_url: str, text: str) -> bool:
    try:
        import httpx
    except ImportError:
        logger.warning("httpx is required for Slack webhook notifications")
        return False

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json={"text": text})
    if not resp.is_success:
        raise RuntimeError(f"Slack webhook HTTP {resp.status_code}: {resp.text[:160]}")
    return True


async def _send_slack_bot_message(bot_token: str, channel: str, text: str) -> bool:
    try:
        import httpx
    except ImportError:
        logger.warning("httpx is required for Slack notifications")
        return False

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel, "text": text},
        )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return True
