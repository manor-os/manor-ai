from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select

from packages.core.governance.approval_scope import (
    approval_scope_candidates,
    approval_scope_key,
)
from packages.core.models.user import User
from packages.core.services.settings_service import update_user_preferences


RuntimeApprovalPreferenceMode = Literal["always_approve", "approval", "deny"]

RUNTIME_APPROVAL_PREF_KEY = "runtime_approval_policy"


def _normalized_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _policy_from_preferences(preferences: dict | None) -> dict:
    policy = (preferences or {}).get(RUNTIME_APPROVAL_PREF_KEY)
    return dict(policy) if isinstance(policy, dict) else {}


def _mode(value: Any) -> RuntimeApprovalPreferenceMode | None:
    normalized = _normalized_key(value)
    if normalized in {"always_approve", "always approve", "always_allow", "always allow", "allow"}:
        return "always_approve"
    if normalized in {"approval", "approve", "ask", "ask_each_time", "manual"}:
        return "approval"
    if normalized in {"deny", "denied", "never", "block", "blocked"}:
        return "deny"
    return None


async def runtime_approval_preference_mode(
    db,
    *,
    user_id: str | None,
    action_key: str | None,
    resource_id: str | None = None,
    capability_id: str | None = None,
) -> RuntimeApprovalPreferenceMode | None:
    """Direct-chat-only standing preference.

    Workspace conversations have no user-preference layer — their one standing
    store is the workspace policy auto-approve set (see the unified approval
    core). ``resource_id`` narrows an action grant when the caller knows one.
    """
    if not user_id:
        return None
    preferences = (
        await db.execute(select(User.preferences).where(User.id == user_id))
    ).scalar_one_or_none()
    if preferences is None:
        return None
    policy = _policy_from_preferences(preferences)
    scoped = policy.get("global")
    if isinstance(scoped, dict):
        actions = scoped.get("actions")
        if isinstance(actions, dict):
            for scope_key in approval_scope_candidates(action_key, resource_id):
                mode = _mode(actions.get(scope_key))
                if mode:
                    return mode
        capabilities = scoped.get("capabilities")
        if capability_id and isinstance(capabilities, dict):
            mode = _mode(capabilities.get(str(capability_id)))
            if mode:
                return mode
    return None


async def set_runtime_approval_preference(
    db,
    *,
    user_id: str,
    mode: RuntimeApprovalPreferenceMode,
    action_key: str | None = None,
    resource_id: str | None = None,
    capability_id: str | None = None,
) -> bool:
    if not user_id or mode not in {"always_approve", "approval", "deny"}:
        return False
    scope_key = approval_scope_key(action_key, resource_id)
    capability_id = str(capability_id or "").strip()
    if not scope_key and not capability_id:
        return False

    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if row is None:
        return False
    prefs = dict(row.preferences or {})
    policy = _policy_from_preferences(prefs)
    scope = "global"
    scoped = dict(policy.get(scope) or {})

    if scope_key:
        actions = dict(scoped.get("actions") or {})
        actions[scope_key] = mode
        scoped["actions"] = actions
    if capability_id:
        capabilities = dict(scoped.get("capabilities") or {})
        capabilities[capability_id] = mode
        scoped["capabilities"] = capabilities

    policy[scope] = scoped
    await update_user_preferences(
        db,
        user_id,
        {RUNTIME_APPROVAL_PREF_KEY: policy},
    )
    return True
