"""Full worker transcripts must not reclassify an already-settled tool call."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as actions
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory


def _exchange():
    arguments = {"kind": "document", "name": "result.md", "content": "original"}
    result = json.dumps({"created": True, "document_id": "doc-1", "path": "result.md"})
    messages = [{
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "function": {"name": "generate_file", "arguments": json.dumps(arguments)},
        }],
    }, {"role": "tool", "tool_call_id": "call-1", "content": result}]
    receipt = ToolEffectEvidenceFactory.bind_effect_id(ToolEffectEvidenceFactory.effect("action", {
        "tool": "generate_file", "tool_call_id": "call-1",
        "arguments_hash": actions._tool_arguments_hash(arguments),
        "owner_plan_id": "plan-1", "owner_step_id": "step-1", "effect_generation": 0,
        "classification": "action", "operation": "create", "intent_state": "returned",
        "result_sha256": hashlib.sha256(result.encode()).hexdigest(), "created": True,
    }))
    return messages, receipt


def _rescan(messages, evidence, **scope):
    return actions._delegation_receipts(
        messages, entity_id="entity-1", workspace_id="workspace-1", task_id="task-1",
        authoritative_intents=evidence,
        **{"owner_plan_id": "plan-1", "owner_step_id": "step-1", "effect_generation": 0, **scope},
    )


def test_exact_scoped_receipt_skips_historical_reclassification(monkeypatch):
    messages, receipt = _exchange()
    before = copy.deepcopy(receipt)

    def unexpected(*args, **kwargs):
        raise AssertionError("the completed create must not be reclassified after the file exists")

    monkeypatch.setattr(actions, "_delegation_effect_intent", unexpected)
    for _ in range(3):
        assert _rescan(messages, [receipt]) == ([], [], [])
    assert receipt == before


@pytest.mark.parametrize("change", [
    "plan", "step", "generation", "tool", "call", "arguments", "result",
    "child_owner", "effect_id_missing", "effect_id_invalid", "unscoped",
    "not_returned", "missing_current_scope",
])
def test_inexact_or_weak_receipt_cannot_hide_a_tool_result(monkeypatch, change):
    messages, receipt = _exchange()
    fields = receipt["fields"]
    updates = {
        "plan": ("owner_plan_id", "other-plan"),
        "step": ("owner_step_id", "other-step"),
        "generation": ("effect_generation", 1),
        "tool": ("tool", "write_file"),
        "call": ("tool_call_id", "nested_other_owner_call-1"),
        "arguments": ("arguments_hash", "other-arguments"),
        "result": ("result_sha256", "other-result"),
        "not_returned": ("intent_state", "started"),
    }
    if change in updates:
        key, value = updates[change]
        fields[key] = value
    elif change == "child_owner":
        receipt["coordinator_child_step_id"] = "other-child"
    elif change == "unscoped":
        fields.pop("owner_step_id")
        fields.pop("owner_plan_id")
    receipt = ToolEffectEvidenceFactory.bind_effect_id(receipt)
    if change == "effect_id_missing":
        receipt["fields"].pop("effect_id")
    elif change == "effect_id_invalid":
        receipt["fields"]["effect_id"] = "forged"
    seen = []

    def classify(tool_name, arguments, **kwargs):
        seen.append((tool_name, arguments))
        return ToolEffectEvidenceFactory.effect("pending", {
            "tool": tool_name, "tool_call_id": kwargs["tool_call_id"],
            "arguments_hash": actions._tool_arguments_hash(arguments),
            "operation": "modify", "classification": "action", "intent_state": "started",
        })

    monkeypatch.setattr(actions, "_delegation_effect_intent", classify)
    scope = {"owner_step_id": None} if change == "missing_current_scope" else {}
    evidence, _, _ = _rescan(messages, [receipt], **scope)
    effects = [ref for ref in evidence if ref.get("kind") == "tool_effect"]
    assert len(seen) == len(effects) == 1
    assert effects[0]["fields"]["operation"] == "modify"


def test_existing_pending_intent_retains_priority_over_a_returned_receipt(monkeypatch):
    messages, receipt = _exchange()
    pending = copy.deepcopy(receipt)
    pending["effect"] = "pending"
    pending["fields"]["intent_state"] = "started"
    pending["fields"].pop("result_sha256")

    def unexpected(*args, **kwargs):
        raise AssertionError("the exact pending intent must own settlement")

    monkeypatch.setattr(actions, "_delegation_effect_intent", unexpected)
    evidence, _, _ = _rescan(messages, [receipt, pending])
    effects = [ref for ref in evidence if ref.get("kind") == "tool_effect"]
    assert len(effects) == 1
    assert effects[0]["fields"]["effect_id"] == pending["fields"]["effect_id"]
    assert effects[0]["fields"]["operation"] == "create"
    assert effects[0]["fields"]["intent_state"] == "returned"


@pytest.mark.parametrize("change", ["plan", "step", "generation", "child_owner", "weak", "invalid"])
@pytest.mark.parametrize("include_returned", [False, True])
def test_foreign_or_weak_pending_cannot_be_settled_by_current_transcript(monkeypatch, change, include_returned):
    messages, receipt = _exchange()
    pending = copy.deepcopy(receipt)
    pending["effect"] = "pending"
    pending["fields"]["intent_state"] = "started"
    if change in {"plan", "step", "generation"}:
        field = {"plan": "owner_plan_id", "step": "owner_step_id", "generation": "effect_generation"}[change]
        pending["fields"][field] = 1 if change == "generation" else "foreign"
    elif change == "child_owner":
        pending["coordinator_child_step_id"] = "nested-owner"
    pending = ToolEffectEvidenceFactory.bind_effect_id(pending)
    if change == "weak":
        pending["fields"].pop("effect_id")
    elif change == "invalid":
        pending["fields"]["effect_id"] = "forged"
    existing = [pending, receipt] if include_returned else [pending]
    original = copy.deepcopy(existing)

    def unexpected(*args, **kwargs):
        raise AssertionError("an ambiguous pending marker must not fall back to classification")

    monkeypatch.setattr(actions, "_delegation_effect_intent", unexpected)
    assert _rescan(messages, existing) == ([], [], [])
    assert existing == original


def test_mixed_current_and_foreign_pending_intents_both_remain_pending(monkeypatch):
    messages, receipt = _exchange()
    current = copy.deepcopy(receipt)
    current["effect"] = "pending"
    current["fields"]["intent_state"] = "started"
    foreign = copy.deepcopy(current)
    foreign["fields"]["owner_step_id"] = "foreign"
    foreign = ToolEffectEvidenceFactory.bind_effect_id(foreign)
    existing = [current, foreign]

    def unexpected(*args, **kwargs):
        raise AssertionError("multiple candidate intents must remain ambiguous")

    monkeypatch.setattr(actions, "_delegation_effect_intent", unexpected)
    assert _rescan(messages, existing) == ([], [], [])
    assert ToolEffectEvidenceFactory.merge_untrusted(existing, []) == existing
