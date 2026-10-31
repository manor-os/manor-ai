"""Open/close a connected account's incident on the dict that holds its health.

An Integration keeps it in ``config``; an OAuthAccount keeps it in ``profile``.
Producers only mark state here. Delivery (account_incident_delivery) notifies
once per incident and stamps ``notified_at``. A repeat failure never reopens
or re-notifies an open incident; the next passing check closes it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

INCIDENT_KEY = "health_incident"


def open_incident(state: dict[str, Any], *, reason: str, now: datetime) -> dict[str, Any]:
    """``state`` with an open incident; an incident already open is kept as is."""
    if isinstance(state.get(INCIDENT_KEY), dict):
        return dict(state)
    return {
        **state,
        INCIDENT_KEY: {"opened_at": now.isoformat(), "reason": reason[:200], "notified_at": None},
    }


def close_incident(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != INCIDENT_KEY}


def apply_health_result(
    state: dict[str, Any], result: Any, *, now: datetime, credential_failure: bool,
) -> dict[str, Any]:
    """Open on a credential failure, close on ``ok is True``; anything else changes nothing.

    A failure that is not about the credentials (network error, 5xx, rate
    limit) says nothing about whether the account needs reconnecting, so it
    neither opens nor closes an incident. The caller decides which is which.
    """
    ok = result.get("ok") if isinstance(result, dict) else None
    if ok is False and credential_failure:
        reason = str(result.get("reason_code") or result.get("detail") or "health_check_failed")
        return open_incident(state, reason=reason, now=now)
    if ok is True:
        return close_incident(state)
    return dict(state)
