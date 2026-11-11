"""Agent actions for the person's saved "Always allow" choices.

A person who picked "Always allow" on a question can see and undo it by
asking in chat ("ask me before adding to the list again"). The agent lists the
saved allow rules and removes the one the person names, acting as that
person: only someone who may edit these approval settings can remove a rule,
and nothing here can add one — "Always allow" is only ever the person's own
click on a question.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


async def _scope_and_actor(db: AsyncSession, *, entity_id: str, user_id: str | None, workspace_id: str | None):
    from packages.core.governance import approval_policy_service as policies
    from packages.core.models.user import User

    if not user_id:
        return None, None
    actor = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if actor is None:
        return None, None
    scope = await policies.resolve_scope(
        db, entity_id=entity_id, workspace_id=workspace_id,
        user_id=None if workspace_id else user_id,
    )
    return scope, actor


def _rule_view(rule: dict[str, Any]) -> dict[str, Any]:
    selector = rule.get("selector") or {}
    return {
        "rule_id": rule["id"],
        "action_key": selector.get("action_key"),
        "category": selector.get("category"),
        "resource_ids": selector.get("resource_ids"),
    }


async def runtime_list_always_allow(
    db: AsyncSession, *, entity_id: str, user_id: str | None, workspace_id: str | None,
) -> str:
    from packages.core.governance import approval_policy_service as policies
    from packages.core.governance.always_allow import list_always_allow_rules

    try:
        scope, actor = await _scope_and_actor(db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id)
        if actor is None:
            return _err("Only a signed-in person can see their approval choices")
        rules = await list_always_allow_rules(db, scope=scope, actor=actor)
    except policies.ApprovalPolicyAccessError as exc:
        return _err(str(exc))
    return json.dumps({"ok": True, "scope": scope.kind, "always_allowed": [_rule_view(rule) for rule in rules]})


async def runtime_stop_always_allow(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    params: dict[str, Any],
) -> str:
    from packages.core.governance import approval_policy_service as policies
    from packages.core.governance.always_allow import revoke_always_allow

    rule_id = str(params.get("rule_id") or "").strip() or None
    action_key = str(params.get("action_key") or "").strip() or None
    if not rule_id and not action_key:
        return _err("Pass rule_id or action_key from list_always_allow")
    try:
        scope, actor = await _scope_and_actor(db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id)
        if actor is None:
            return _err("Only a signed-in person can change their approval choices")
        removed = await revoke_always_allow(db, scope=scope, actor=actor, rule_id=rule_id, action_key=action_key)
    except policies.ApprovalPolicyAccessError as exc:
        await db.rollback()
        return _err(str(exc))
    except policies.ApprovalPolicyConflict:
        await db.rollback()
        return _err("Approval settings changed meanwhile; list them again and retry")
    if not removed:
        return json.dumps({"ok": True, "removed": [], "message": "Nothing matched; no saved choice changed."})
    await db.commit()
    return json.dumps({
        "ok": True,
        "removed": [_rule_view({"id": item["id"], "selector": item["selector"]}) for item in removed],
        "message": "Done. The next time this comes up, you will be asked first.",
    })
