"""Authorization questions: "just this once" or "always", and which to recommend.

Every authorization a person is asked offers "Always allow" alongside the
one-time approval, worded as the scope it covers. Choosing it writes an
``allow`` rule through the versioned approval-settings service (the same rule
a person saves in Execution & approvals settings), so the next action of the
same kind passes the gate without a question. Saying "ask me again" in chat
removes that rule (:func:`revoke_always_allow`).

The recommendation follows two rules:

* an action that cannot be undone (destructive, or one that reaches other
  people: messages, publishing, purchases) recommends "just this once";
* otherwise, once a person approved the same action
  ``APPROVAL_ALWAYS_RECOMMEND_AFTER`` times and never declined it, "always"
  is recommended. Before that, "just this once".

Nothing here decides an approval. The person still chooses; this module only
shapes the question and writes the rule they chose.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.governance.approval_policy import (
    ActionSelector,
    ApprovalScope,
    PolicyDraft,
    RuleDraft,
)
from packages.core.services.decision_recommendation import recommendation

#: The wire choice that approves once and saves the allow rule.
ALWAYS_ALLOW_CHOICE = "approve_and_save_rule"
#: Categories whose effect reaches other people and cannot be taken back.
IRREVERSIBLE_CATEGORIES = frozenset({"communication", "publishing", "deletion", "purchases"})
#: ``decided_via`` of a release the policy made, not a person.
_NON_HUMAN_DECISIONS = frozenset({"policy_change"})


def always_recommend_after() -> int:
    """How many unrefused approvals make "always" the recommended answer."""
    try:
        return max(1, int(os.getenv("APPROVAL_ALWAYS_RECOMMEND_AFTER", "3")))
    except ValueError:
        return 3


def is_irreversible(*, action_key: str | None, impact: str | None = None, category: str | None = None) -> bool:
    """Whether doing the action once cannot be undone by Manor."""
    if impact is None or category is None:
        from packages.core.governance.action_effects import declared_action_effect

        declared = declared_action_effect(action_key)
        if declared is not None:
            impact = impact or declared.impact
            category = category or declared.category
    return impact == "destructive" or (category or "") in IRREVERSIBLE_CATEGORIES


async def decision_history(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None, action_key: str,
) -> tuple[int, int]:
    """(approved, declined) decisions people made on this action here.

    Counts unified approval requests and the message-backed external-reply
    cards (which have no request row). Releases a policy change made are not
    a person's approval and are not counted.
    """
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message

    scope = (
        HitlRequest.workspace_id == workspace_id if workspace_id
        else HitlRequest.workspace_id.is_(None)
    )
    rows = (await db.execute(
        select(HitlRequest.status, func.count()).where(
            HitlRequest.entity_id == entity_id, scope,
            HitlRequest.action_key == action_key,
            HitlRequest.hitl_type == HitlType.AUTHORIZE.value,
            HitlRequest.decided_by_user_id.is_not(None),
            or_(HitlRequest.decided_via.is_(None), HitlRequest.decided_via.notin_(_NON_HUMAN_DECISIONS)),
        ).group_by(HitlRequest.status)
    )).all()
    approved = sum(count for status, count in rows if status in {
        ApprovalStatus.GRANTED.value, ApprovalStatus.CONSUMED.value,
    })
    declined = sum(count for status, count in rows if status == ApprovalStatus.DENIED.value)
    if workspace_id:
        choice = Message.resolution["choice"].as_string()
        card_rows = (await db.execute(
            select(choice, func.count()).join(
                Conversation, Conversation.id == Message.conversation_id,
            ).where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
                Message.pending_action["action_key"].as_string() == action_key,
                Message.pending_action["approval_request_id"].as_string().is_(None),
                Message.resolved_by_user_id.is_not(None),
                Message.resolved_at.is_not(None),
            ).group_by(choice)
        )).all()
        for value, count in card_rows:
            normalized = str(value or "").lower()
            if normalized in {"approve", "confirm", ALWAYS_ALLOW_CHOICE}:
                approved += count
            elif normalized in {"reject", "rejected", "deny"}:
                declined += count
    return approved, declined


def recommend_authorization(
    *, irreversible: bool, approved: int, declined: int, threshold: int, standing: bool,
) -> dict[str, Any]:
    """Pure policy: which answer to recommend, and the sentence saying why."""
    if irreversible:
        return recommendation("approve", reason_code="irreversible_once")
    if standing and declined == 0 and approved >= threshold:
        return recommendation(
            ALWAYS_ALLOW_CHOICE, reason_code="repeated_always", reason_params={"count": approved},
        )
    return recommendation("approve", reason_code="approve_once")


async def authorization_decision(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    action_key: str | None,
    impact: str | None = None,
    category: str | None = None,
    standing: bool = True,
) -> dict[str, Any] | None:
    """The stored shape of an authorization question (no wording yet).

    Kept as data on the request so every card built from it later — the live
    stream, a reload, a reminder — offers the same choices.
    """
    if not action_key:
        return None
    approved, declined = await decision_history(
        db, entity_id=entity_id, workspace_id=workspace_id, action_key=action_key,
    )
    irreversible = is_irreversible(action_key=action_key, impact=impact, category=category)
    threshold = always_recommend_after()
    return {
        "standing": bool(standing),
        "approved": approved,
        "declined": declined,
        "threshold": threshold,
        "irreversible": irreversible,
        "recommendation": recommend_authorization(
            irreversible=irreversible, approved=approved, declined=declined,
            threshold=threshold, standing=standing,
        ),
    }


def always_allow_label(action_label: str | None) -> str:
    text = " ".join(str(action_label or "").split()).rstrip(".")
    if not text:
        return "Always allow this without asking"
    return f"Always {text[0].lower()}{text[1:]} without asking"


def authorization_card_fields(
    decision: dict[str, Any] | None, *, action_label: str | None, action_key: str | None = None,
) -> dict[str, Any]:
    """``options`` / ``option_details`` / ``recommendation`` for one card.

    ``batch_key`` names the kind of authorization: open cards sharing it are
    shown as one question ("3 replies are waiting — send all / review each /
    hold all"); each card still resolves through its own path.
    """
    if not isinstance(decision, dict) or not isinstance(decision.get("recommendation"), dict):
        return {}
    standing = bool(decision.get("standing"))
    rec = dict(decision["recommendation"])
    if not standing and rec.get("option") == ALWAYS_ALLOW_CHOICE:
        rec = recommendation("approve", reason_code="approve_once")
    options = ["approve", ALWAYS_ALLOW_CHOICE, "reject"] if standing else ["approve", "reject"]
    options = [rec["option"], *[option for option in options if option != rec["option"]]]
    details: dict[str, Any] = {}
    if standing:
        details[ALWAYS_ALLOW_CHOICE] = {
            "label": always_allow_label(action_label),
            "label_code": "authorize.always",
            "label_params": {"action": str(action_label or "")},
            "scope": "action",
        }
        if rec.get("option") == ALWAYS_ALLOW_CHOICE:
            details["approve"] = {"label": "Just this once", "label_code": "authorize.once"}
    fields: dict[str, Any] = {"options": options, "option_details": details, "recommendation": rec}
    if action_key:
        fields["batch_key"] = f"authorize:{action_key}"
    return fields


async def save_always_allow_rule(
    db: AsyncSession, *, scope: ApprovalScope, actor, action_key: str,
) -> dict[str, Any]:
    """Add (once) a user ``allow`` rule for ``action_key`` in ``scope``.

    Uses the settings service under its own owner lock and the version it
    just read, so a concurrent settings edit is merged, never overwritten.
    The caller commits.
    """
    from packages.core.governance import approval_policy_service as policies

    await policies._load(db, scope, lock=True)
    current = await policies.read_approval_settings(db, scope, actor)
    if not current["capabilities"]["can_save_rule"]:
        raise policies.ApprovalPolicyAccessError("You cannot save an approval rule here")
    rules = [
        RuleDraft(id=rule["id"], effect=rule["effect"], selector=rule["selector"])
        for rule in current["policy"]["rules"] if rule["source_kind"] == "user"
    ]
    selected = RuleDraft(effect="allow", selector=ActionSelector(action_key=action_key))
    if any(rule.effect == selected.effect and rule.selector == selected.selector for rule in rules):
        return current
    rules.append(selected)
    return await policies.update_approval_policy(
        db, scope, PolicyDraft(mode=current["policy"]["mode"], user_rules=rules), current["version"], actor,
    )


def _is_always_allow(rule: dict[str, Any]) -> bool:
    return rule.get("source_kind") == "user" and rule.get("effect") == "allow"


async def list_always_allow_rules(db: AsyncSession, *, scope: ApprovalScope, actor) -> list[dict[str, Any]]:
    """The person-saved ``allow`` rules in ``scope`` (what "always" saved)."""
    from packages.core.governance import approval_policy_service as policies

    current = await policies.read_approval_settings(db, scope, actor)
    return [
        {"id": rule["id"], "selector": rule["selector"]}
        for rule in current["policy"]["rules"] if _is_always_allow(rule)
    ]


async def revoke_always_allow(
    db: AsyncSession,
    *,
    scope: ApprovalScope,
    actor,
    rule_id: str | None = None,
    action_key: str | None = None,
) -> list[dict[str, Any]]:
    """Remove saved "always" rules by id or by the action they cover.

    Returns the removed rules; an empty list when nothing matched. The next
    action of that kind asks again. The caller commits.
    """
    from packages.core.governance import approval_policy_service as policies

    if not rule_id and not action_key:
        raise ValueError("name the rule or the action to stop allowing")
    await policies._load(db, scope, lock=True)
    current = await policies.read_approval_settings(db, scope, actor)
    kept: list[RuleDraft] = []
    removed: list[dict[str, Any]] = []
    for rule in current["policy"]["rules"]:
        if rule["source_kind"] != "user":
            continue
        selector = rule["selector"] if isinstance(rule["selector"], dict) else {}
        matches = _is_always_allow(rule) and (
            (rule_id and rule["id"] == rule_id)
            or (action_key and selector.get("action_key") == action_key)
        )
        if matches:
            removed.append({"id": rule["id"], "selector": selector})
        else:
            kept.append(RuleDraft(id=rule["id"], effect=rule["effect"], selector=rule["selector"]))
    if removed:
        await policies.update_approval_policy(
            db, scope, PolicyDraft(mode=current["policy"]["mode"], user_rules=kept), current["version"], actor,
        )
    return removed
