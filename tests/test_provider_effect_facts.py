"""Provider-native approvals carry their category into Manor facts (C2).

A Chrome checkout is a purchase and a Chrome delete is a deletion, so the
user's own category rules match them; without rules the user's mode decides.
Categories with no factual Manor equivalent stay undeclared and ask.
"""
from __future__ import annotations

import pytest

from packages.core.governance.action_effects import (
    PROVIDER_CATEGORY_EFFECTS,
    provider_policy_effect,
)
from packages.core.models.base import generate_ulid


def _category_rule(category, effect):
    return {"effect": effect, "selector": {"kind": "category", "category": category}}


@pytest.mark.parametrize(("policy_category", "impact", "category"), [
    ("financial_transaction", "external", "purchases"),
    ("deletion", "destructive", "deletion"),
    ("representational_communication", "external", "communication"),
    ("software_installation", "external", "unknown"),
    ("authentication", "unknown", "unknown"),
    (None, "unknown", "unknown"),
])
def test_provider_categories_map_to_facts(policy_category, impact, category):
    assert provider_policy_effect("chrome", policy_category) == {"impact": impact, "category": category}


@pytest.mark.parametrize(("category", "impact", "fact_category"), [
    # The named external Chrome effects stay external (#1027 follow-up 095618379).
    ("file_upload", "external", "communication"),
    ("youtube_upload_start", "external", "publishing"),
    ("representational_communication", "external", "communication"),
    # Mapped categories now carry their fact, so category rules match.
    ("financial_transaction", "external", "purchases"),
    ("deletion", "destructive", "deletion"),
    # Anything else stays an unnamed effect.
    ("authentication", "unknown", "unknown"),
    (None, "unknown", "unknown"),
])
def test_chrome_confirm_action_uses_the_provider_category_table(category, impact, fact_category):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool

    args = {"approvalId": "a1", "confirmation_mode": "always_action_time"}
    if category:
        args["policy_category"] = category
    action = classify_runtime_tool("mcp__chrome__confirm_action", args).action
    assert (action.impact, action.category) == (impact, fact_category)


def test_every_provider_mapping_is_a_known_effect():
    assert PROVIDER_CATEGORY_EFFECTS
    for effect in PROVIDER_CATEGORY_EFFECTS.values():
        assert effect.impact != "unknown"


async def _account(db, *, mode, rules):
    from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
    from packages.core.governance.approval_policy_service import read_approval_settings, update_approval_policy
    from tests.approval_settings_test_utils import initialize_test_account_approval

    entity_id = generate_ulid()
    actor = await initialize_test_account_approval(db, entity_id=entity_id)
    scope = ApprovalScope(kind="account", id=entity_id, entity_id=entity_id)
    before = await read_approval_settings(db, scope, actor)
    await update_approval_policy(db, scope, PolicyDraft(mode=mode, user_rules=rules), before["version"], actor)
    await db.flush()
    return entity_id, actor.id


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy_category", "mode", "rules", "granted"), [
    # No rules: the user's mode decides.
    ("financial_transaction", "automatic", [], True),
    ("deletion", "automatic", [], True),
    ("financial_transaction", "risk_based", [], False),
    # The user's category rules now reach a Chrome checkout / delete.
    ("financial_transaction", "automatic", [_category_rule("purchases", "ask")], False),
    ("deletion", "automatic", [_category_rule("deletion", "deny")], False),
    ("representational_communication", "automatic", [_category_rule("purchases", "ask")], True),
    # No factual Manor category: unknown, which follows the mode (#1027):
    # automatic runs it, Confirm by risk asks.
    ("authentication", "automatic", [], True),
    ("authentication", "risk_based", [], False),
])
async def test_chrome_gate_follows_the_users_mode_and_category_rules(db_session, policy_category, mode, rules, granted):
    from packages.core.ai.runtime import approval_service

    entity_id, user_id = await _account(db_session, mode=mode, rules=rules)
    allowed = await approval_service._standing_grant(
        db_session, entity_id=entity_id, workspace_id=None, user_id=user_id,
        action_key="chrome.action", resource_id=None, capability_id="chrome.action",
        policy_category=policy_category,
    )
    assert allowed is granted
