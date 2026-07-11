"""Notification routing — pick which channels deliver a given event to a user.

This is the "where does this notification go?" half of ``notify()``. It
reads three layers of configuration and merges them into an ordered list
of channel choices, each paired with the concrete ``ChannelContact`` to
send to (or ``None`` for in-app, which doesn't need a contact row).

Layers (high → low precedence):

  1. ``user.preferences.notifications.by_kind[<kind>].channels``
  2. ``user.preferences.notifications.default_channels``
  3. ``workspace.settings.notification_policy.routes``
     (keyed by event kind; falls back to ``default_routes``)
  4. ``entity.settings.notification_policy.default_routes``
  5. ``["inapp"]`` — system fallback so every event reaches the bell icon

A user's selected channel is only usable if they actually have an active
``ChannelContact`` for that channel_type with ``user_id`` linked. Otherwise
the channel silently falls through — we never raise on missing bindings.

``severity == "critical"`` forces all configured channels to fire (fan-out
override). Quiet hours are honoured for ``info`` events but bypassed for
``warn`` / ``critical`` unless the user opts out with
``bypass_quiet_hours=false`` per-kind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import (
    SUPPORTED_CHANNELS,
    event_default_severity,
)
from packages.core.models.channel import ChannelContact
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass
class ChannelChoice:
    """One concrete delivery target for a notification.

    ``channel_type`` is the registry key (``"inapp"`` / ``"telegram"`` / …).
    ``contact`` is the ``ChannelContact`` for external channels; ``None``
    for in-app and implicit account-email delivery. ``address`` is used
    when email falls back to the user's registered email without a linked
    ``ChannelContact``.
    """
    channel_type: str
    contact: Optional[ChannelContact] = None
    address: Optional[str] = None


_INAPP = "inapp"


# ── User preference helpers ─────────────────────────────────────────────────

def _user_notification_prefs(user_prefs: dict | None) -> dict:
    if not isinstance(user_prefs, dict):
        return {}
    notif = user_prefs.get("notifications")
    return notif if isinstance(notif, dict) else {}


def _by_kind_override(notif_prefs: dict, kind: str) -> dict | None:
    by_kind = notif_prefs.get("by_kind")
    if not isinstance(by_kind, dict):
        return None
    override = by_kind.get(kind)
    return override if isinstance(override, dict) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str) and v]


# ── Quiet hours ────────────────────────────────────────────────────────────

def _is_in_quiet_hours(notif_prefs: dict, now: datetime | None = None) -> bool:
    quiet = notif_prefs.get("quiet_hours")
    if not isinstance(quiet, dict):
        return False
    start_raw = quiet.get("from")
    end_raw = quiet.get("to")
    tz_raw = quiet.get("tz") or "UTC"
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return False
    try:
        start = time.fromisoformat(start_raw)
        end = time.fromisoformat(end_raw)
    except ValueError:
        return False
    try:
        tz = ZoneInfo(str(tz_raw))
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    current = (now or datetime.now(timezone.utc)).astimezone(tz).time()
    # Same-day window (e.g. 14:00 → 18:00)
    if start <= end:
        return start <= current < end
    # Overnight window (e.g. 22:00 → 08:00)
    return current >= start or current < end


# ── Entity / workspace policy helpers ──────────────────────────────────────

def _settings_routes(settings: dict | None) -> tuple[dict, list[str]]:
    """Return (per-kind routes, default routes) from a settings dict."""
    if not isinstance(settings, dict):
        return {}, []
    policy = settings.get("notification_policy")
    if not isinstance(policy, dict):
        return {}, []
    routes = policy.get("routes") if isinstance(policy.get("routes"), dict) else {}
    defaults = _string_list(policy.get("default_routes"))
    return routes, defaults


def _normalise_channels(values: list[str]) -> list[str]:
    """Drop unknown channel keys + dedupe while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in SUPPORTED_CHANNELS or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


# ── Channel selection ─────────────────────────────────────────────────────

def select_channels(
    *,
    kind: str,
    severity: str,
    user_prefs: dict | None,
    workspace_settings: dict | None = None,
    entity_settings: dict | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Pure function — work out which channels a notification should go to.

    Separated from the DB-touching resolver so tests can exercise the
    precedence rules without a database.
    """
    notif_prefs = _user_notification_prefs(user_prefs)
    override = _by_kind_override(notif_prefs, kind)

    chosen: list[str] = []
    # warn / critical events bypass quiet hours by default; per-kind
    # overrides can still flip this either direction.
    bypass_quiet = severity in {"warn", "critical"}

    if override is not None:
        if override.get("enabled") is False:
            # Even when the user disables a kind, the in-app bell still
            # shows it — they explicitly opted out of pushes, not out of
            # their own audit trail.
            return [_INAPP]
        chosen = _string_list(override.get("channels"))
        if "bypass_quiet_hours" in override:
            bypass_quiet = bool(override.get("bypass_quiet_hours"))

    if not chosen:
        chosen = _string_list(notif_prefs.get("default_channels"))

    if not chosen:
        ws_routes, ws_default = _settings_routes(workspace_settings)
        chosen = _string_list(ws_routes.get(kind)) or ws_default

    if not chosen:
        _e_routes, e_default = _settings_routes(entity_settings)
        chosen = e_default

    if not chosen:
        chosen = [_INAPP]
    elif _INAPP not in chosen:
        # Always include in-app: the bell icon is the user's audit trail
        # for every notification; external channels are layered on top.
        chosen = [_INAPP] + chosen

    chosen = _normalise_channels(chosen)

    # Critical events fan out to everything the user configured anywhere,
    # even if quiet hours would otherwise mute them.
    if severity == "critical":
        return chosen

    if _is_in_quiet_hours(notif_prefs, now=now) and not bypass_quiet:
        # Quiet hours — only keep the in-app trail; suppress all pushes.
        return [_INAPP]

    return chosen


# ── DB-backed resolution ───────────────────────────────────────────────────

async def resolve_channel_targets(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    kind: str,
    severity: str | None = None,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> list[ChannelChoice]:
    """Resolve channel selection for a user to concrete delivery targets.

    Returns at least one entry (the ``inapp`` fallback). External channel
    types are dropped silently when the user has no active linked
    ``ChannelContact`` — operators see no error, just a single in-app row,
    which is the desired fail-safe behaviour.
    """
    user = (await db.execute(
        select(User).outerjoin(
            UserMembership,
            and_(
                UserMembership.user_id == User.id,
                UserMembership.entity_id == entity_id,
                UserMembership.status == "active",
            ),
        ).where(
            User.id == user_id,
            User.status == "active",
            or_(
                User.entity_id == entity_id,
                UserMembership.id.is_not(None),
            ),
        )
    )).scalar_one_or_none()
    if not user:
        return [ChannelChoice(channel_type=_INAPP)]

    entity = (await db.execute(
        select(Entity).where(Entity.id == entity_id)
    )).scalar_one_or_none()

    workspace: Workspace | None = None
    if workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
            )
        )).scalar_one_or_none()

    effective_severity = severity or event_default_severity(kind)
    channel_types = select_channels(
        kind=kind,
        severity=effective_severity,
        user_prefs=user.preferences,
        workspace_settings=workspace.settings if workspace else None,
        entity_settings=entity.settings if entity else None,
        now=now,
    )

    external_types = [ct for ct in channel_types if ct != _INAPP]
    contacts_by_type: dict[str, list[ChannelContact]] = {}
    if external_types:
        rows = (await db.execute(
            select(ChannelContact).where(
                ChannelContact.entity_id == entity_id,
                ChannelContact.user_id == user_id,
                ChannelContact.status == "active",
                ChannelContact.channel_type.in_(external_types),
            )
        )).scalars().all()
        # Prefer the most-recently-seen contact when a user linked the same
        # channel_type twice (rare, but possible after re-binding).
        for row in sorted(
            rows,
            key=lambda r: r.last_seen_at or r.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        ):
            contacts_by_type.setdefault(row.channel_type, []).append(row)

    choices: list[ChannelChoice] = []
    for ct in channel_types:
        if ct == _INAPP:
            choices.append(ChannelChoice(channel_type=_INAPP))
            continue
        contacts = contacts_by_type.get(ct, [])
        if ct == "email":
            seen_addresses = {
                str(contact.source_id).strip().lower()
                for contact in contacts
                if str(contact.source_id).strip()
            }
            for contact in contacts:
                choices.append(ChannelChoice(channel_type=ct, contact=contact))
            user_email = str(user.email or "").strip().lower()
            if user_email and user_email not in seen_addresses:
                choices.append(ChannelChoice(channel_type=ct, address=user_email))
            if contacts or user_email:
                continue

        contact = contacts[0] if contacts else None
        if contact is None:
            logger.debug(
                "notification routing: user=%s has no linked %s contact — skipping",
                user_id, ct,
            )
            continue
        choices.append(ChannelChoice(channel_type=ct, contact=contact))
    return choices
