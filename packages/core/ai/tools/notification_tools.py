"""Agent-callable tools for sending notifications + discovering recipients.

``notify_user`` is the agent's hook into the notification pipeline we
already built (``notify()`` → ``notification_routing`` → channel adapters).
The agent doesn't see ``ChannelContact`` rows or per-user preferences —
it just names a recipient and a message, and the dispatcher figures out
which channels to use based on that user's settings.

``find_team_members`` lets the agent resolve names/emails to user IDs.
Without it, the agent can't know whom to notify unless it has the id
already cached from a previous tool call.

Scope guardrails (enforced in handlers, not just docstrings):

  * recipient must belong to the same entity as the calling agent —
    no cross-tenant pings.
  * Severity defaults to ``info``; the handler clamps unknown values.
  * No ``actions`` / ``callback_kind`` here — actionable notifications
    are a separate, security-sensitive surface and need their own tool
    with explicit auth checks before we open them up.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.notification_actions import (
    runtime_find_team_members_action,
    runtime_notify_user_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

logger = logging.getLogger(__name__)


# ── Schemas ────────────────────────────────────────────────────────────────

NOTIFY_USER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "notify_user",
        "description": (
            "Send a notification to a Manor user. Lands in their in-app "
            "bell **and** any external channel they opted into (Telegram, "
            "WeChat, email, …). Use when you want to ping someone about "
            "something they need to see even if they're not currently in "
            "this conversation — completed jobs, new task assignments, "
            "alerts that need attention.\n\n"
            "Get the ``user_id`` from ``find_team_members`` or from prior "
            "tool calls. The recipient must be in your entity; cross-"
            "tenant pings are refused."
        ),
        "parameters": {
            "type": "object",
            "required": ["user_id", "title"],
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Manor user ID of the recipient.",
                },
                "title": {
                    "type": "string",
                    "description": "Short heading (~80 chars). Shows up "
                                   "as the bold line on the bell + the "
                                   "subject line on Telegram/email.",
                },
                "body": {
                    "type": "string",
                    "description": "Plain-text body. Markdown is rendered "
                                   "on the in-app bell; channels that "
                                   "don't support it see it as text.",
                },
                "link": {
                    "type": "string",
                    "description": "Optional Manor-internal path (e.g. "
                                   "``/tasks/01KQ...``) the user can "
                                   "follow to act on the notification.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "warn", "critical"],
                    "description": (
                        "Default ``info``. ``warn`` / ``critical`` "
                        "bypass quiet-hours and may fan out wider "
                        "channels per the recipient's preferences."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional event-catalog kind (e.g. "
                        "``task_assigned``, ``task_succeeded``, "
                        "``video``). Default ``agent_message``. The kind "
                        "controls per-event routing the user configured "
                        "in their notification preferences."
                    ),
                },
            },
        },
    },
}


FIND_TEAM_MEMBERS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find_team_members",
        "description": (
            "Search for Manor users in your entity. Use this to resolve "
            "names / emails / handles to a ``user_id`` you can hand to "
            "``notify_user``. With no ``query`` it returns the entity's "
            "active members (capped at ``limit``)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Substring match (case-insensitive) against "
                        "``display_name``, ``first_name``, ``last_name``, "
                        "``email``. Omit to list everyone."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional — when set, limit the results to "
                        "members assigned to this workspace via "
                        "``workspace_staff``."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
        },
    },
}


# ── Handlers ──────────────────────────────────────────────────────────────


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


_VALID_SEVERITIES = {"info", "warn", "critical"}


async def _notify_user(entity_id: str, **kwargs: Any) -> str:
    """Send a notification through the multi-channel pipeline.

    ``entity_id`` is injected by the tool runner from the agent's
    execution context — agents can't override it, which is what keeps
    cross-tenant calls impossible.
    """
    target_user_id = str(kwargs.get("user_id") or "").strip()
    title = str(kwargs.get("title") or "").strip()
    if not target_user_id:
        return _err("user_id is required")
    if not title:
        return _err("title is required")

    body = kwargs.get("body")
    link = kwargs.get("link")
    severity = str(kwargs.get("severity") or "info").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "info"
    kind = str(kwargs.get("kind") or "agent_message").strip() or "agent_message"

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_notify_user_action(
        entity_id=entity_id,
        params={
            "user_id": target_user_id,
            "title": title,
            "body": body,
            "link": link,
            "severity": severity,
            "kind": kind,
        },
        workspace_id=runtime_context.workspace_id,
        agent_id=runtime_context.agent_id or kwargs.get("agent_id"),
    )


async def _find_team_members(entity_id: str, **kwargs: Any) -> str:
    return await runtime_find_team_members_action(entity_id=entity_id, params=kwargs)


# ── Public ────────────────────────────────────────────────────────────────


def get_tools() -> list[tuple[dict, Any]]:
    return [
        (NOTIFY_USER_SCHEMA, _notify_user),
        (FIND_TEAM_MEMBERS_SCHEMA, _find_team_members),
    ]
