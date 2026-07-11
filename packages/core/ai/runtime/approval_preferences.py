from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from packages.core.models.user import User


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
    capability_id: str | None = None,
    workspace_id: str | None = None,
) -> RuntimeApprovalPreferenceMode | None:
    if not user_id:
        return None
    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if row is None:
        return None
    policy = _policy_from_preferences(row.preferences)
    scopes = []
    if workspace_id:
        scopes.append(f"workspace:{workspace_id}")
    scopes.append("global")

    for scope in scopes:
        scoped = policy.get(scope)
        if not isinstance(scoped, dict):
            continue
        actions = scoped.get("actions")
        if action_key and isinstance(actions, dict):
            mode = _mode(actions.get(str(action_key)))
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
    capability_id: str | None = None,
    workspace_id: str | None = None,
) -> bool:
    if not user_id or mode not in {"always_approve", "approval", "deny"}:
        return False
    action_key = str(action_key or "").strip()
    capability_id = str(capability_id or "").strip()
    if not action_key and not capability_id:
        return False

    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if row is None:
        return False
    prefs = dict(row.preferences or {})
    policy = _policy_from_preferences(prefs)
    scope = f"workspace:{workspace_id}" if workspace_id else "global"
    scoped = dict(policy.get(scope) or {})

    if action_key:
        actions = dict(scoped.get("actions") or {})
        actions[action_key] = mode
        scoped["actions"] = actions
    if capability_id:
        capabilities = dict(scoped.get("capabilities") or {})
        capabilities[capability_id] = mode
        scoped["capabilities"] = capabilities

    policy[scope] = scoped
    prefs[RUNTIME_APPROVAL_PREF_KEY] = policy
    row.preferences = prefs
    flag_modified(row, "preferences")
    return True
