"""Workspace operation patch ops must be discoverable and self-correcting.

Production incident (2026-09-07, trace ``e91c0d82a52d``): the master agent
tried to lift a ``never_allow: social_post.publish`` gate with a patch op it
invented — ``governance_policy.patch`` — and got back only
``unsupported workspace operation patch: governance_policy.patch``. Nothing in
the tool surface listed the real op names, and the error did not either, so
the agent concluded the *workspace* forbids governance changes and gave up.

The class of bug is "agent-visible op vocabulary is not derived from the
implementation". These tests pin the contract:

* one catalogue, derived from the dispatcher, is the single source of truth;
* every advertised op dispatches; every dispatched op is advertised;
* an unknown op is rejected with the full vocabulary plus the closest match;
* guardrail lists are derived from rules, so rules can be removed/retyped
  through the same patch vocabulary and the validator explains when a
  governance edit would be undone by a surviving rule.
"""

from __future__ import annotations

import copy

import pytest

from packages.core.services.workspace_operation_service import (
    OPERATION_PATCH_OP_CATALOG,
    SUPPORTED_OPERATION_PATCH_OPS,
    _apply_single_patch,
    _draft_rules_touched,
    describe_operation_patch_ops,
)


def _fresh_state() -> dict:
    return {
        "operating_model": {},
        "services": [],
        "goals": [],
        "rules": [],
        "automations": [],
        "evaluation": {},
        "knowledge_policy": {},
        "capability_bindings": [],
        "skill_bindings": [],
        "integration_scope": {},
        "channel_config": {},
        "processes": [],
        "strategist": {},
        "agent_mappings": [],
        "budget_policy": {},
        "heartbeat_policy": {},
        "governance_policy": {},
        "immediate_tasks": [],
    }


# ── catalogue ↔ dispatcher ────────────────────────────────────────────────


def test_catalog_is_non_empty_and_canonical_ops_are_unique():
    canonical = [entry.canonical for entry in OPERATION_PATCH_OP_CATALOG]
    assert canonical
    assert len(canonical) == len(set(canonical))
    assert "governance_policy.update" in canonical
    assert "capability_binding.upsert" in canonical
    assert "rule.add" in canonical
    assert "rule.remove" in canonical
    assert "rule.update" in canonical


def test_supported_ops_cover_every_catalog_name():
    names = {
        name
        for entry in OPERATION_PATCH_OP_CATALOG
        for name in (entry.canonical, *entry.aliases)
    }
    assert set(SUPPORTED_OPERATION_PATCH_OPS) == names


@pytest.mark.parametrize("op", sorted(SUPPORTED_OPERATION_PATCH_OPS))
def test_every_catalogued_op_dispatches(op: str):
    """Mutation guard: an op the catalogue advertises must reach a handler."""
    try:
        _apply_single_patch(_fresh_state(), {"op": op, "payload": {}})
    except ValueError as exc:
        # Handlers may reject an empty payload; they must never fall through
        # to the unsupported-op branch.
        assert "unsupported workspace operation patch" not in str(exc)


def test_unknown_op_error_lists_vocabulary_and_closest_match():
    with pytest.raises(ValueError) as exc:
        _apply_single_patch(_fresh_state(), {"op": "governance_policy.patch"})
    message = str(exc.value)
    assert "unsupported workspace operation patch: governance_policy.patch" in message
    assert "did you mean" in message.lower()
    assert "governance_policy.update" in message
    for entry in OPERATION_PATCH_OP_CATALOG:
        assert entry.canonical in message


def test_unknown_op_without_close_match_still_lists_vocabulary():
    with pytest.raises(ValueError) as exc:
        _apply_single_patch(_fresh_state(), {"op": "totally.bogus"})
    message = str(exc.value)
    assert "unsupported workspace operation patch: totally.bogus" in message
    assert "did you mean" not in message.lower()
    assert "governance_policy.update" in message


def test_describe_ops_is_compact_and_mentions_rule_derived_guardrails():
    text = describe_operation_patch_ops()
    for entry in OPERATION_PATCH_OP_CATALOG:
        assert entry.canonical in text
    # The agent needs to know a governance edit alone will not survive a rule
    # that still implies the gate.
    assert "rule.remove" in text and "rule.update" in text


def test_only_the_in_product_vocabulary_names_the_create_draft_call_shape():
    from packages.core.ai.runtime.workspace_operation_actions import (
        describe_operation_patch_ops as in_product_describe,
    )

    # The service text is shared with the MCP endpoint, whose call shape differs.
    assert "create_draft" not in describe_operation_patch_ops()
    assert '{"action":"create_draft","patches":' in in_product_describe()


# ── rule.remove / rule.update ────────────────────────────────────────────


def _rules_state() -> dict:
    state = _fresh_state()
    state["rules"] = [
        {
            "rule_key": "draft_only_social",
            "description": "Social posts are draft only",
            "rule_type": "draft_only",
            "action_patterns": ["social_post.publish"],
            "capability_patterns": ["external.social"],
            "runtime_enforced": True,
        },
        {
            "rule_key": "tone",
            "description": "Keep a friendly tone",
        },
    ]
    state["operating_model"] = {"rules": copy.deepcopy(state["rules"])}
    return state


def test_rule_remove_by_rule_key_syncs_operating_model():
    out = _apply_single_patch(
        _rules_state(),
        {"op": "rule.remove", "payload": {"rule_key": "draft_only_social"}},
    )
    assert [rule["rule_key"] for rule in out["rules"]] == ["tone"]
    assert [rule["rule_key"] for rule in out["operating_model"]["rules"]] == ["tone"]


def test_rule_remove_by_description_is_case_insensitive():
    out = _apply_single_patch(
        _rules_state(),
        {"op": "rule.remove", "description": "  social posts are DRAFT only "},
    )
    assert [rule["rule_key"] for rule in out["rules"]] == ["tone"]


def test_rule_remove_unknown_rule_raises_with_known_keys():
    with pytest.raises(ValueError) as exc:
        _apply_single_patch(_rules_state(), {"op": "rule.remove", "rule_key": "nope"})
    message = str(exc.value)
    assert "nope" in message
    assert "draft_only_social" in message and "tone" in message


def test_rule_update_retypes_rule_and_recomputes_enforcement():
    out = _apply_single_patch(
        _rules_state(),
        {
            "op": "rule.update",
            "payload": {
                "rule_key": "draft_only_social",
                "rule": {"rule_type": "approval_required"},
            },
        },
    )
    updated = next(r for r in out["rules"] if r["rule_key"] == "draft_only_social")
    assert updated["rule_type"] == "approval_required"
    assert updated["action_patterns"] == ["social_post.publish"]
    assert updated["runtime_enforced"] is True
    # untouched fields survive the merge
    assert updated["description"] == "Social posts are draft only"


def test_rule_update_accepts_flat_fields():
    out = _apply_single_patch(
        _rules_state(),
        {"op": "rule.update", "rule_key": "tone", "description": "Keep a warm tone"},
    )
    updated = next(r for r in out["rules"] if r["rule_key"] == "tone")
    assert updated["description"] == "Keep a warm tone"


def test_rule_mutations_count_as_rules_touched():
    assert _draft_rules_touched([{"op": "rule.remove", "rule_key": "x"}]) is True
    assert _draft_rules_touched([{"op": "rule.update", "rule_key": "x"}]) is True
    assert _draft_rules_touched([{
        "op": "operating_model.replace",
        "payload": {"operating_model": {"rules": []}},
    }]) is True
    assert _draft_rules_touched([{
        "op": "operating_model.update",
        "payload": {"operating_model": {"rules": []}},
    }]) is True
    assert _draft_rules_touched([{
        "op": "operating_model.update",
        "payload": {"operating_model": {"evaluation": {"enabled": True}}},
    }]) is False
    assert _draft_rules_touched([{"op": "budget_policy.update"}]) is False
