"""Per-kind email unsubscribe links.

A link turns email off for one notification kind; the in-app bell keeps the
notification. Each token is bound to the address it was sent to, so a link
stops working once the account's email changes.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import (
    EVENTS_BY_KIND,
    NotificationChannel,
    event_default_severity,
)
from packages.core.models.user import User
from packages.core.services.notification_routing import (
    by_kind_override,
    select_channels,
    user_notification_prefs,
)
from packages.core.services.settings_service import get_user_preferences, update_user_preferences
from packages.core.services.signed_email_links import app_link, sign_email_link, verify_email_link

TOKEN_TYPE = "notification_unsubscribe"
_EMAIL = NotificationChannel.EMAIL.value


def create_unsubscribe_token(user: User, kind: str) -> str:
    return sign_email_link(user, TOKEN_TYPE, kind=kind)


def unsubscribe_url(user: User, kind: str) -> str:
    return app_link(f"/api/v1/notifications/unsubscribe/{create_unsubscribe_token(user, kind)}")


async def resolve_unsubscribe_token(db: AsyncSession, token: str) -> tuple[User, str] | None:
    verified = await verify_email_link(db, token, TOKEN_TYPE, required=("kind",))
    if verified is None:
        return None
    user, claims = verified
    kind = str(claims.get("kind") or "")
    return (user, kind) if kind in EVENTS_BY_KIND else None


def _channels(user_prefs: dict | None, kind: str) -> list[str]:
    return select_channels(
        kind=kind, severity=event_default_severity(kind),
        user_prefs=user_prefs, apply_quiet_hours=False,
    )


def email_enabled_for_kind(user_prefs: dict | None, kind: str) -> bool:
    """Whether routing for ``kind`` includes email; quiet hours are the caller's call."""
    return _EMAIL in _channels(user_prefs, kind)


def email_explicitly_disabled(user_prefs: dict | None, kind: str) -> bool:
    """Whether the user's own per-kind state already excludes email.

    Unlike ``email_enabled_for_kind`` this ignores defaults and workspace or
    entity routes: an unsubscribe must still write the override when email would
    only reach the user through one of those.
    """
    override = by_kind_override(user_notification_prefs(user_prefs), kind) or {}
    if override.get("enabled") is False:
        return True
    channels = override.get("channels")
    return bool(channels) and isinstance(channels, list) and _EMAIL not in channels


async def unsubscribe_email(db: AsyncSession, user: User, kind: str) -> bool:
    """Drop email from ``kind``; False when the user already excluded it. Caller commits."""
    prefs = await get_user_preferences(db, user.id) or {}
    if email_explicitly_disabled(prefs, kind):
        return False
    notif = dict(prefs.get("notifications") or {})
    by_kind = dict(notif.get("by_kind") or {})
    # An explicit override outranks every default and workspace route, so it is
    # written even when the user-level channels never listed email. ``inapp`` is
    # always present, so the list is never empty (routing reads [] as "no override").
    by_kind[kind] = {
        **(by_kind_override(user_notification_prefs(prefs), kind) or {}),
        "enabled": True,
        "channels": [c for c in _channels(prefs, kind) if c != _EMAIL],
    }
    notif["by_kind"] = by_kind
    await update_user_preferences(db, user.id, {"notifications": notif})
    return True
