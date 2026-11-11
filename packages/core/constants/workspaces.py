"""Stable Workspace setting ownership boundaries and lifecycle sets."""
from __future__ import annotations

from enum import Enum

LIVE_WORKSPACE_STATUSES = ("active", "paused", "provisioning", "needs_setup")


class WorkspaceServerSettingKey(str, Enum):
    """Top-level ``Workspace.settings`` keys ordinary updates cannot own."""

    BLUEPRINT = "_blueprint"
    BLOCKING_SETUP = "blocking_setup"
    CREATED_BY_USER_ID = "created_by_user_id"


WORKSPACE_SERVER_SETTING_KEYS: frozenset[str] = frozenset(
    key.value for key in WorkspaceServerSettingKey
)
