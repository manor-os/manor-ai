"""Unit tests for notification routing precedence rules.

Exercises ``select_channels`` directly (no DB) so the precedence logic is
locked in independently from the DB-touching ``resolve_channel_targets``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.core.services.notification_routing import select_channels


def _now(hour: int, minute: int = 0, tz: str = "UTC") -> datetime:
    return datetime(2024, 1, 1, hour, minute, 0, tzinfo=timezone.utc)


# ── No preferences anywhere ────────────────────────────────────────────────


def test_defaults_to_inapp_only_when_nothing_configured():
    chans = select_channels(
        kind="task_failed",
        severity="info",
        user_prefs=None,
    )
    assert chans == ["inapp"]


# ── User default_channels ──────────────────────────────────────────────────


def test_user_default_channels_layered_with_inapp():
    chans = select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram", "email"],
            }
        },
    )
    # Always layered with inapp at the front.
    assert chans == ["inapp", "telegram", "email"]


def test_user_default_channels_dedupe_and_drop_unknown():
    chans = select_channels(
        kind="task_failed",
        severity="info",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram", "telegram", "carrier_pigeon", "email"],
            }
        },
    )
    assert chans == ["inapp", "telegram", "email"]


# ── by_kind overrides ──────────────────────────────────────────────────────


def test_by_kind_override_wins_over_defaults():
    chans = select_channels(
        kind="task_hitl_requested",
        severity="warn",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram"],
                "by_kind": {
                    "task_hitl_requested": {"channels": ["email"]},
                },
            }
        },
    )
    assert chans == ["inapp", "email"]


def test_by_kind_disabled_drops_external():
    chans = select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram", "email"],
                "by_kind": {
                    "task_failed": {"enabled": False},
                },
            }
        },
    )
    # Disabled kind keeps the in-app audit row but drops all pushes.
    assert chans == ["inapp"]


# ── Workspace / entity fallback ────────────────────────────────────────────


def test_workspace_default_routes_apply_when_user_has_no_prefs():
    chans = select_channels(
        kind="task_failed",
        severity="info",
        user_prefs={},
        workspace_settings={
            "notification_policy": {
                "default_routes": ["slack"],
            }
        },
    )
    assert chans == ["inapp", "slack"]


def test_workspace_per_kind_routes_apply():
    chans = select_channels(
        kind="task_hitl_requested",
        severity="warn",
        user_prefs={},
        workspace_settings={
            "notification_policy": {
                "default_routes": ["slack"],
                "routes": {
                    "task_hitl_requested": ["telegram"],
                },
            }
        },
    )
    assert chans == ["inapp", "telegram"]


def test_entity_default_routes_apply_as_last_fallback():
    chans = select_channels(
        kind="video",
        severity="info",
        user_prefs=None,
        workspace_settings=None,
        entity_settings={
            "notification_policy": {
                "default_routes": ["email"],
            }
        },
    )
    assert chans == ["inapp", "email"]


# ── Quiet hours ────────────────────────────────────────────────────────────


def test_quiet_hours_suppresses_info_pushes():
    chans = select_channels(
        kind="video",
        severity="info",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram"],
                "quiet_hours": {"tz": "UTC", "from": "22:00", "to": "08:00"},
            }
        },
        now=_now(23),
    )
    assert chans == ["inapp"]


def test_quiet_hours_does_not_suppress_warn_by_default():
    chans = select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram"],
                "quiet_hours": {"tz": "UTC", "from": "22:00", "to": "08:00"},
            }
        },
        now=_now(23),
    )
    # warn / critical default to bypassing quiet hours.
    assert chans == ["inapp", "telegram"]


def test_quiet_hours_critical_fans_out_regardless():
    chans = select_channels(
        kind="system_health",
        severity="critical",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram", "email"],
                "quiet_hours": {"tz": "UTC", "from": "22:00", "to": "08:00"},
            }
        },
        now=_now(2),
    )
    assert chans == ["inapp", "telegram", "email"]


def test_quiet_hours_overnight_window():
    user = {
        "notifications": {
            "default_channels": ["telegram"],
            "quiet_hours": {"tz": "UTC", "from": "22:00", "to": "08:00"},
        }
    }
    # Middle of the window
    assert select_channels(
        kind="video",
        severity="info",
        user_prefs=user,
        now=_now(3),
    ) == ["inapp"]
    # Outside the window
    assert select_channels(
        kind="video",
        severity="info",
        user_prefs=user,
        now=_now(12),
    ) == ["inapp", "telegram"]


def test_per_kind_bypass_quiet_hours_overrides_info():
    chans = select_channels(
        kind="video",
        severity="info",
        user_prefs={
            "notifications": {
                "default_channels": ["telegram"],
                "by_kind": {
                    "video": {"channels": ["telegram"], "bypass_quiet_hours": True},
                },
                "quiet_hours": {"tz": "UTC", "from": "22:00", "to": "08:00"},
            }
        },
        now=_now(2),
    )
    assert chans == ["inapp", "telegram"]


# ── Precedence: by_kind beats workspace beats entity ───────────────────────


def test_full_precedence_chain():
    user_with_override = {
        "notifications": {
            "default_channels": ["telegram"],
            "by_kind": {"task_failed": {"channels": ["email"]}},
        }
    }
    workspace = {
        "notification_policy": {
            "routes": {"task_failed": ["slack"]},
            "default_routes": ["telegram"],
        }
    }
    entity = {"notification_policy": {"default_routes": ["whatsapp"]}}

    # user.by_kind wins
    assert select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs=user_with_override,
        workspace_settings=workspace,
        entity_settings=entity,
    ) == ["inapp", "email"]

    # Drop the user override → workspace.routes wins
    workspace_only_user = {"notifications": {}}
    assert select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs=workspace_only_user,
        workspace_settings=workspace,
        entity_settings=entity,
    ) == ["inapp", "slack"]

    # Drop workspace routes for this kind → workspace defaults win
    workspace_no_kind = {"notification_policy": {"default_routes": ["telegram"]}}
    assert select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs=workspace_only_user,
        workspace_settings=workspace_no_kind,
        entity_settings=entity,
    ) == ["inapp", "telegram"]

    # Drop workspace entirely → entity defaults win
    assert select_channels(
        kind="task_failed",
        severity="warn",
        user_prefs=workspace_only_user,
        workspace_settings=None,
        entity_settings=entity,
    ) == ["inapp", "whatsapp"]


@pytest.mark.parametrize("severity", ["info", "warn", "critical"])
def test_inapp_is_always_present(severity):
    chans = select_channels(
        kind="task_assigned",
        severity=severity,
        user_prefs={"notifications": {"default_channels": ["telegram"]}},
    )
    assert "inapp" in chans
