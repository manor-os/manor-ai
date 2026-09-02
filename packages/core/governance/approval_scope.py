"""Canonical scope keys for standing approval grants."""

from __future__ import annotations


_RESOURCE_SCOPE_SEPARATOR = "#resource:"


def approval_scope_key(
    action_key: str | None,
    resource_id: str | None = None,
) -> str | None:
    """Return the canonical standing-grant scope for an action.

    ``action_key`` is the primary identity. ``resource_id`` narrows the
    grant when a caller knows the approval should only apply to one
    concrete resource instance.
    """

    action = str(action_key or "").strip()
    if not action:
        return None
    resource = str(resource_id or "").strip()
    if not resource:
        return action
    return f"{action}{_RESOURCE_SCOPE_SEPARATOR}{resource}"


def approval_scope_candidates(
    action_key: str | None,
    resource_id: str | None = None,
) -> tuple[str, ...]:
    """Return matching keys in preference order.

    The exact action+resource scope is checked first, then the broader
    action-only grant.
    """

    action = str(action_key or "").strip()
    if not action:
        return ()
    resource = str(resource_id or "").strip()
    if resource:
        return (
            f"{action}{_RESOURCE_SCOPE_SEPARATOR}{resource}",
            action,
        )
    return (action,)
