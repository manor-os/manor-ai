"""Migration 20260914_02: legacy hard blocks the user never asked for become
approval gates.

Owner decision (2026-09-07): no hard block without the user's own request.
The architect honours it for new workspaces; this backfill honours it for the
ones created before. Prod task FQ2881 (2026-09-14) hit a ``draft_only`` rule
from August that derived a ``never_allow`` on ``social_post.publish``.
"""
from __future__ import annotations


import pytest

from packages.core.services.legacy_hard_block_backfill import (
    LEGACY_HARD_BLOCK_DOWNGRADE_MARKER,
    downgrade_unrequested_hard_blocks,
    downgrade_workspace_state,
    hard_block_was_requested,
    rebuild_governance_after_retype,
)



def _draft_only_rule(**extra):
    return {
        "rule_key": "draft_only_social",
        "description": "社交帖子先出草稿，经审批后再发布",
        "rule_type": "draft_only",
        "action_patterns": ["social_post.publish"],
        "capability_patterns": ["external.social"],
        "runtime_enforced": True,
        **extra,
    }


# ── who asked for it ──────────────────────────────────────────────────


def test_architect_authored_draft_only_without_a_request_is_not_requested():
    assert hard_block_was_requested(_draft_only_rule()) is False


@pytest.mark.parametrize(
    "signal",
    [
        {"source": "operator"},
        {"hard_block_requested_by_user": True},
        {"enforcement_scope": "all_matching_actions"},
        {"description": "禁止删除已发布的帖子"},
        {"description": "Never publish without a human sign-off"},
    ],
)
def test_any_persisted_user_request_signal_keeps_the_hard_block(signal):
    assert hard_block_was_requested(_draft_only_rule(**signal)) is True


# ── the retype ────────────────────────────────────────────────────────


def test_unrequested_hard_block_becomes_approval_required_with_an_audit_trail():
    rules = [_draft_only_rule(), {"rule_key": "tone", "description": "用创始人口吻"}]

    after, downgraded = downgrade_unrequested_hard_blocks(rules)

    assert [r["rule_key"] for r in downgraded] == ["draft_only_social"]
    changed = after[0]
    assert changed["rule_type"] == "approval_required"
    assert changed["downgraded_from"] == "draft_only"
    assert changed["downgraded_by"] == LEGACY_HARD_BLOCK_DOWNGRADE_MARKER
    assert changed["downgraded_at"]
    assert changed["runtime_enforced"] is True  # re-derived for the new type
    assert after[1] == rules[1]
    # input untouched
    assert rules[0]["rule_type"] == "draft_only"


@pytest.mark.parametrize("rule_type", ["deny", "never_allow", "block", "draft_only"])
def test_every_hard_block_type_is_covered(rule_type):
    after, downgraded = downgrade_unrequested_hard_blocks([_draft_only_rule(rule_type=rule_type)])
    assert downgraded and after[0]["downgraded_from"] == rule_type


def test_requested_and_already_downgraded_rules_pass_through():
    kept = _draft_only_rule(description="禁止发布", rule_key="user_ban")
    already = _draft_only_rule(rule_key="done", rule_type="approval_required", downgraded_by=LEGACY_HARD_BLOCK_DOWNGRADE_MARKER)
    after, downgraded = downgrade_unrequested_hard_blocks([kept, already])
    assert downgraded == []
    assert after[0]["rule_type"] == "draft_only" and after[1]["rule_type"] == "approval_required"


# ── the derived lists follow ──────────────────────────────────────────


def test_released_patterns_move_from_never_allow_to_hitl_required():
    before = [_draft_only_rule()]
    stored = {
        "never_allow_actions": ["social_post.publish"],
        "never_allow_capabilities": ["external.social"],
        "hitl_required_actions": ["email.send"],
        "hitl_required_capabilities": [],
        "auto_approve_actions": [],
        "auto_approve_capabilities": [],
        "max_risk_level": "high",
    }
    after, _ = downgrade_unrequested_hard_blocks(before)

    policy = rebuild_governance_after_retype(before, after, stored)

    assert policy["never_allow_actions"] == []
    assert policy["never_allow_capabilities"] == []
    assert set(policy["hitl_required_actions"]) == {"email.send", "social_post.publish"}
    assert "external.social" in policy["hitl_required_capabilities"]
    assert policy["max_risk_level"] == "high"


def test_a_pattern_a_surviving_hard_block_still_claims_stays_blocked():
    before = [
        _draft_only_rule(),
        _draft_only_rule(rule_key="user_ban", rule_type="deny", description="禁止发布任何帖子"),
    ]
    stored = {"never_allow_actions": ["social_post.publish"], "never_allow_capabilities": ["external.social"]}
    after, downgraded = downgrade_unrequested_hard_blocks(before)
    assert [r["rule_key"] for r in downgraded] == ["draft_only_social"]

    policy = rebuild_governance_after_retype(before, after, stored)

    assert policy["never_allow_actions"] == ["social_post.publish"]
    assert policy["never_allow_capabilities"] == ["external.social"]


def test_workspace_state_helper_reports_no_change_when_nothing_qualifies():
    rules = [{"rule_key": "tone", "description": "用创始人口吻"}]
    after, policy, keys = downgrade_workspace_state(rules, {"never_allow_actions": []})
    assert keys == [] and policy == {"never_allow_actions": []} and after == rules


# ── the migration against a real database ────────────────────────────


