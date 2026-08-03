"""Phase 3 §4.6 — a `review` card must say what it is asking approval FOR.

``rules.replace`` REBUILDS the workspace's ``never_allow`` list from the
draft's rules, so an agent can delete its own hard blocks — the one governance
tier that has no approval path at all — through an operation draft. The draft
did go through a ``workspace_operation_review`` HITL gate, but the card said,
in full: "Apply this workspace operation draft?".

That is the incident's defect again: a card demanding approval without saying
what for. These tests pin that the review payload now carries a real diff, that
the diff is computed from the policy an apply would actually write (not the
draft's stale stored copy), and that a hard-block removal is called out.
"""
from __future__ import annotations

import json

import pytest

from packages.core.constants.approvals import HitlType
from packages.core.governance.approvals import validate_hitl_payload
from packages.core.models.base import generate_ulid
from packages.core.services.workspace_operation_service import (
    operation_review_diff,
    operation_review_hitl_payload,
)


#: A workspace whose author wrote one hard block ("never refund") and one
#: ordinary HITL rule. Shaped like ``_snapshot_workspace_state``'s output.
def _state(rules: list[dict], *, never_allow: list[str]) -> dict:
    return {
        "workspace_id": generate_ulid(),
        "operating_model": {"rules": rules},
        "rules": rules,
        "governance_policy": {
            "never_allow_actions": never_allow,
            "hitl_required_actions": ["social_post.publish"],
            "max_risk_level": "high",
        },
    }


_HARD_BLOCK_RULE = {
    "rule_key": "never_refund",
    "description": "Never issue refunds automatically.",
    "rule_type": "never_allow",
    "action_patterns": ["payments.refund"],
}
_HITL_RULE = {
    "rule_key": "review_posts",
    "description": "A human reviews every post before it goes out.",
    "rule_type": "approval_required",
    "action_patterns": ["social_post.publish"],
}


def _draft(*, before_rules, after_rules, never_allow):
    """A draft whose ``rules.replace`` swapped one rule set for another."""
    return {
        "id": generate_ulid(),
        "current_state": _state(after_rules, never_allow=never_allow),
        "patches": [{"op": "rules.replace", "payload": {"rules": after_rules}}],
        "diff": {
            "changed_keys": ["rules"],
            "changes": {
                "rules": {"before": before_rules, "after": after_rules},
            },
        },
        "validation": {"errors": [], "warnings": []},
    }


def test_dropping_a_never_allow_rule_is_reported_as_a_removed_hard_block():
    """The whole point. The draft's stored governance_policy still lists
    ``payments.refund``; only the policy an APPLY would write does not."""
    draft = _draft(
        before_rules=[_HARD_BLOCK_RULE, _HITL_RULE],
        after_rules=[_HITL_RULE],
        never_allow=["payments.refund"],
    )
    diff = operation_review_diff(draft)
    assert diff["removed_hard_blocks"] == ["payments.refund"]
    assert diff["modified"] == ["rules"]

    payload = operation_review_hitl_payload(draft)
    assert payload["diff"] == diff
    # The sentence a person reads. It has to name the pattern — "this draft
    # changes rules" is exactly the copy that hid the change.
    assert "payments.refund" in payload["why"]
    assert "REMOVES hard blocks" in payload["why"]


def test_a_draft_that_keeps_its_hard_blocks_reports_none():
    """No false alarm: a rules change that preserves never_allow is quiet."""
    draft = _draft(
        before_rules=[_HARD_BLOCK_RULE],
        after_rules=[_HARD_BLOCK_RULE, _HITL_RULE],
        never_allow=["payments.refund"],
    )
    diff = operation_review_diff(draft)
    assert diff["removed_hard_blocks"] == []
    payload = operation_review_hitl_payload(draft)
    assert "REMOVES hard blocks" not in payload["why"]
    assert "rules" in payload["why"]


def test_added_removed_modified_partition_the_changed_keys():
    draft = {
        "id": generate_ulid(),
        "current_state": _state([], never_allow=[]),
        "patches": [],
        "diff": {
            "changed_keys": ["goals", "channel_config", "services"],
            "changes": {
                "goals": {"before": [], "after": [{"title": "Grow"}]},
                "channel_config": {"before": {"slack": True}, "after": {}},
                "services": {"before": [{"key": "a"}], "after": [{"key": "b"}]},
            },
        },
    }
    diff = operation_review_diff(draft)
    assert diff["added"] == ["goals"]
    assert diff["removed"] == ["channel_config"]
    assert diff["modified"] == ["services"]


def test_a_review_payload_without_a_diff_is_refused_by_the_record_layer():
    """``HITL_REQUIRED_PAYLOAD_FIELDS`` has listed ("diff", "why") for review
    since the type system shipped, and nothing produced a review payload — so
    the rule had never been exercised. The producer runs through the same
    validator the mint path uses, which is what makes it a rule."""
    with pytest.raises(ValueError, match="diff"):
        validate_hitl_payload(HitlType.REVIEW.value, {"why": "trust me"})
    with pytest.raises(ValueError, match="why"):
        validate_hitl_payload(HitlType.REVIEW.value, {"diff": {"changed_keys": []}})


def test_the_review_card_carries_the_payload_and_offers_no_always():
    """End to end through the card producer: the tool result the agent returns
    and the durable chat card built from it both carry the typed payload, and
    neither offers "Always" — a standing grant over "whatever the next draft
    says" is not something a person can consent to."""
    from packages.core.ai.runtime.workspace_operation_actions import (
        _operation_review_payload,
    )
    from packages.core.services.hitl_requests import (
        workspace_operation_pending_action_from_data,
    )

    draft = _draft(
        before_rules=[_HARD_BLOCK_RULE, _HITL_RULE],
        after_rules=[_HITL_RULE],
        never_allow=["payments.refund"],
    )
    draft["workspace_id"] = generate_ulid()
    draft["base_revision"] = 0
    draft["status"] = "open"

    result = _operation_review_payload(
        draft, prompt="Apply these workspace runtime changes?", content={},
    )
    assert result["hitl"]["hitl_type"] == HitlType.REVIEW.value
    assert result["hitl"]["payload"]["diff"]["removed_hard_blocks"] == [
        "payments.refund"
    ]
    assert "always_approve" not in result["hitl"]["options"]
    assert result["operation"]["removed_hard_blocks"] == ["payments.refund"]

    pending = workspace_operation_pending_action_from_data(result)
    assert pending["hitl_type"] == HitlType.REVIEW.value
    assert pending["payload"]["diff"]["removed_hard_blocks"] == ["payments.refund"]
    assert "always_approve" not in pending["options"]


def test_the_internal_worker_path_builds_the_same_card():
    """Two producers used to build this blob field by field. The one in the
    internal worker dropped the typed payload — so the warning existed in the
    tool result and nowhere the operator would ever look."""
    from packages.core.ai.runtime.workspace_operation_actions import (
        _operation_review_payload,
    )
    from packages.core.services.hitl_requests import (
        workspace_operation_pending_action_from_data,
    )
    from packages.core.workers.internal import _pending_action_from_tool_payload

    draft = _draft(
        before_rules=[_HARD_BLOCK_RULE, _HITL_RULE],
        after_rules=[_HITL_RULE],
        never_allow=["payments.refund"],
    )
    result = json.loads(json.dumps(_operation_review_payload(
        draft, prompt="Apply these workspace runtime changes?", content={},
    ), default=str))
    assert _pending_action_from_tool_payload(
        result
    ) == workspace_operation_pending_action_from_data(result)


def test_the_review_diff_is_the_policy_apply_would_write():
    """The card and ``apply_operation_draft`` share one resolver.

    Recomputing the prospective policy independently in the card would mean
    the card is showing a second opinion; the rebuild rule ("rules touched and
    governance_policy not explicitly changed ⇒ discard the inferred lists and
    rebuild") lives in exactly one function now.
    """
    from packages.core.services.workspace_operation_service import (
        _REBUILT_GOVERNANCE_POLICY_KEYS,
        _resolved_governance_policy,
    )
    from packages.core.services.workspace_setup_service import (
        _enrich_operating_rules,
    )

    state = _state([_HITL_RULE], never_allow=["payments.refund"])
    rules = _enrich_operating_rules(state["rules"])

    rebuilt = _resolved_governance_policy(
        state, operating_rules=rules, rules_touched=True, changed_keys={"rules"},
    )
    assert rebuilt.never_allow_actions == []
    # Non-rebuilt knobs survive the rebuild untouched.
    assert rebuilt.max_risk_level == "high"
    assert "max_risk_level" not in _REBUILT_GOVERNANCE_POLICY_KEYS

    # Same state, but the draft did not touch rules → nothing is rebuilt.
    kept = _resolved_governance_policy(
        state, operating_rules=rules, rules_touched=False, changed_keys={"goals"},
    )
    assert kept.never_allow_actions == ["payments.refund"]
