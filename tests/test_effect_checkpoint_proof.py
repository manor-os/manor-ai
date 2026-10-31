"""Replay admission binds the returned bytes to the exact effect invocation."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.nested_agent import _qualified_call_id, runtime_checkpoint_leaf_call_id
from packages.core.ai.runtime.workspace_delegation_actions import (
    _delegation_receipts,
    _tool_arguments_hash,
    _tool_result_canonical_sha256,
)
from packages.core.constants.tool_effects import ToolEffectSettlementOutcome
from packages.core.constants.execution import ACTION_EFFECT_GENERATION_PARAM_KEY
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.effect_safety import (
    _checkpoint_contains_tool_result,
    _checkpoint_proves_tool_effect,
    has_ambiguous_external_effect,
)


RESULT = '{"ok":true,"status":"completed","id":"receipt-1"}'


def _checkpoint():
    arguments = {"path": "documents/result.md", "content": "saved"}
    return RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "Save the document"},
            {"role": "assistant", "tool_calls": [{
                "id": "write", "function": {
                    "name": "write_file", "arguments": json.dumps(arguments),
                },
            }]},
        ],
        usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "write", "name": "write_file", "arguments": arguments},
        pending_tool_result={"id": "write", "content": RESULT},
    ).to_dict()


def _receipt(checkpoint, *, call_id="write", content=RESULT):
    pending = checkpoint["pending_tool_call"]
    return ToolEffectEvidenceFactory.bind_effect_id({
        "kind": "tool_effect", "effect": "action", "fields": {
            "tool": pending["name"], "tool_call_id": call_id,
            "arguments_hash": _tool_arguments_hash(pending["arguments"]),
            "owner_plan_id": "plan", "owner_step_id": "step", "effect_generation": "0",
            "intent_state": "returned",
            "result_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
    })


def _after(messages):
    """A Runtime checkpoint whose loop already moved past these messages."""
    return RuntimeAgentCheckpoint(
        messages=messages, usage={}, rounds=2, tool_calls_made=[],
        pending_tool_call={"id": "next", "name": "read_file", "arguments": {"path": "notes.md"}},
    ).to_dict()


def _transcript(checkpoint):
    return _after([
        *copy.deepcopy(checkpoint["messages"]),
        {"role": "tool", "tool_call_id": "write", "content": RESULT},
    ])


@pytest.mark.parametrize("mismatch", [None, "plan", "step", "generation", "invalid_generation"])
@pytest.mark.parametrize("generation", [0, 2])
def test_direct_proof_and_selector_require_current_scoped_owner(mismatch, generation):
    from packages.core.dispatcher.service import _failure_checkpoint

    checkpoint = _checkpoint()
    effect = _receipt(checkpoint)
    effect["fields"]["effect_generation"] = str(generation)
    params = {ACTION_EFFECT_GENERATION_PARAM_KEY: generation}
    if mismatch == "plan":
        effect["fields"]["owner_plan_id"] = "foreign-plan"
    elif mismatch == "step":
        effect["fields"]["owner_step_id"] = "foreign-step"
    elif mismatch == "generation":
        params[ACTION_EFFECT_GENERATION_PARAM_KEY] = generation + 1
    elif mismatch == "invalid_generation":
        params[ACTION_EFFECT_GENERATION_PARAM_KEY] = "invalid"
    effect = ToolEffectEvidenceFactory.bind_effect_id(effect)
    current = copy.deepcopy(checkpoint)
    current["pending_tool_result"] = None
    owner = SimpleNamespace(id="step", plan_id="plan", params=params, runtime_checkpoint=current, evidence_refs=[effect])
    assert _checkpoint_proves_tool_effect(checkpoint, effect, evidence_refs=[effect], owner_step=owner) is (mismatch is None)
    assert _failure_checkpoint(owner, checkpoint, persisted_evidence=[effect]) == (checkpoint if mismatch is None else current)


def test_legacy_direct_proof_needs_exact_result_without_inventing_generation():
    checkpoint = _checkpoint()
    effect = _receipt(checkpoint)
    for field in ("effect_id", "owner_plan_id", "owner_step_id", "effect_generation"):
        effect["fields"].pop(field)
    owner = SimpleNamespace(id="step", plan_id="plan", params={})
    assert _checkpoint_proves_tool_effect(checkpoint, effect, evidence_refs=[effect], owner_step=owner)
    owner.params = {ACTION_EFFECT_GENERATION_PARAM_KEY: 1}
    assert not _checkpoint_proves_tool_effect(checkpoint, effect, evidence_refs=[effect], owner_step=owner)
    owner.params = {}
    checkpoint["pending_tool_result"]["content"] = "unknown"
    assert not _checkpoint_proves_tool_effect(checkpoint, effect, evidence_refs=[effect], owner_step=owner)


def test_handoff_proof_rejects_owner_generation_advanced_after_mint():
    _, parent, effect, witness, owner = _nested_handoff_fixture()
    owner.params = {ACTION_EFFECT_GENERATION_PARAM_KEY: 1}
    assert not _checkpoint_proves_tool_effect(parent, effect, evidence_refs=[effect, witness], owner_step=owner)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("mismatch", [None, "name", "arguments", "result", "result_id", "schema"])
def test_pending_result_requires_exact_receipt(nested, mismatch):
    leaf = _checkpoint()
    tree = leaf
    if nested:
        tree = RuntimeAgentCheckpoint(
            messages=[], usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call={"id": "skill", "name": "invoke_skill", "arguments": {"skill_id": "skill-1"}},
            tool_continuations={"skill": {"nested_skill": {
                "version": 1, "skill_id": "skill-1", "kind": "pending", "checkpoint": leaf,
            }}},
        ).to_dict()
        leaf = tree["tool_continuations"]["skill"]["nested_skill"]["checkpoint"]
    identity = runtime_checkpoint_leaf_call_id(tree)
    effect = _receipt(leaf, call_id=identity)
    if mismatch == "name":
        leaf["pending_tool_call"]["name"] = "delete_file"
    elif mismatch == "arguments":
        leaf["pending_tool_call"]["arguments"]["path"] = "documents/other.md"
    elif mismatch == "result":
        leaf["pending_tool_result"]["content"] = '{"status":"completed","id":"different"}'
    elif mismatch == "result_id":
        leaf["pending_tool_result"]["id"] = "other"
    elif mismatch == "schema":
        leaf["schema_version"] = 999
    assert _checkpoint_contains_tool_result(tree, identity, effect_ref=effect) is (mismatch is None)


@pytest.mark.parametrize("result", [
    '{"error":"timeout after mutation"}',
    '{"status":"failed"}',
    '{"ok":false}',
    '{"status":"queued"}',
    '{"status":"processing"}',
    'The operation probably completed.',
])
def test_failure_or_nonterminal_bytes_cannot_prove_return_even_with_matching_digest(result):
    checkpoint = _checkpoint()
    checkpoint["pending_tool_result"]["content"] = result
    effect = _receipt(checkpoint, content=result)
    assert not _checkpoint_contains_tool_result(checkpoint, "write", effect_ref=effect)


@pytest.mark.parametrize("mismatch", [None, "arguments", "missing_call", "out_of_order", "reused_id", "pending", "digest"])
def test_transcript_proof_pairs_exact_call_and_result(mismatch):
    pending = _checkpoint()
    effect = _receipt(pending)
    transcript = _transcript(pending)
    if mismatch == "arguments":
        transcript["messages"][1]["tool_calls"][0]["function"]["arguments"] = '{"path":"other"}'
    elif mismatch == "missing_call":
        transcript["messages"].pop(1)
    elif mismatch == "out_of_order":
        transcript["messages"].reverse()
    elif mismatch == "reused_id":
        transcript["messages"].append(copy.deepcopy(transcript["messages"][1]))
    elif mismatch == "pending":
        effect["effect"] = "pending"
    elif mismatch == "digest":
        effect["fields"]["result_sha256"] = "a" * 64
    assert _checkpoint_contains_tool_result(transcript, "write", effect_ref=effect) is (mismatch is None)


def test_legacy_transcript_with_complete_receipt_keeps_proof():
    checkpoint = _checkpoint()
    effect = _receipt(checkpoint)
    effect["fields"].pop("effect_id")
    assert _checkpoint_contains_tool_result(_transcript(checkpoint), "write", effect_ref=effect)


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}',
    '{"a":Infinity}', '{"a":1e999}', '{"a":1} trailing',
    '["' + 'a' * 1_048_576 + '"]',
])
def test_canonical_digest_rejects_ambiguous_or_unbounded_json(raw):
    assert _tool_result_canonical_sha256(raw) is None


def test_host_canonical_receipt_survives_json_normalization_and_dedupes_rescan():
    checkpoint = _checkpoint()
    raw = '{"status":"completed","id":"receipt-1","_runtime_result_canonical_sha256":"forged"}'
    pending = _receipt(checkpoint, content=raw)
    pending["effect"] = "pending"
    pending["fields"]["intent_state"] = "started"
    messages = _transcript(checkpoint)["messages"]
    messages[-1]["content"] = raw
    receipts, _, _ = _delegation_receipts(
        messages, entity_id="entity", workspace_id="workspace", task_id=None,
        authoritative_intents=[pending],
    )
    receipt = next(ref for ref in receipts if ref.get("kind") == "tool_effect")
    assert receipt["fields"]["result_canonical_sha256"] == _tool_result_canonical_sha256(raw)
    assert receipt["fields"]["result_canonical_sha256"] != "forged"
    assert receipt["fields"]["result_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    normalized = json.dumps(json.loads(raw), sort_keys=True, indent=2)
    messages[-1]["content"] = normalized
    transcript = _after(messages)
    assert _checkpoint_contains_tool_result(transcript, "write", effect_ref=receipt)
    rescanned, _, _ = _delegation_receipts(
        messages, entity_id="entity", workspace_id="workspace", task_id=None,
        authoritative_intents=receipts, owner_plan_id="plan", owner_step_id="step", effect_generation=0,
    )
    assert rescanned == []
    messages[-1]["content"] = json.dumps({
        "status": "completed", "_tool_result_truncated": {"sha256": receipt["fields"]["result_sha256"]},
    })
    assert not _checkpoint_contains_tool_result(_after(messages), "write", effect_ref=receipt)


@pytest.mark.parametrize("pending_kind", ["pending_tool_call", "remaining_tool_calls"])
def test_pending_call_cannot_borrow_historical_same_id_result(pending_kind):
    checkpoint = _checkpoint()
    effect = _receipt(checkpoint)
    checkpoint["messages"] = _transcript(checkpoint)["messages"]
    checkpoint["pending_tool_result"] = None
    if pending_kind == "remaining_tool_calls":
        checkpoint["remaining_tool_calls"] = [checkpoint["pending_tool_call"]]
        checkpoint["pending_tool_call"] = {"id": "another", "name": "read_file", "arguments": {}}
    assert not _checkpoint_contains_tool_result(checkpoint, "write", effect_ref=effect)


@pytest.mark.asyncio
async def test_ambiguity_uses_terminal_receipt_and_preserves_host_no_effect_settlement():
    checkpoint = _checkpoint()
    receipt = _receipt(checkpoint)
    step = SimpleNamespace(id="step", plan_id="plan", step_status="running", runtime_checkpoint=checkpoint)
    assert not await has_ambiguous_external_effect(None, [receipt], owner_step=step)
    pending = copy.deepcopy(receipt)
    pending["effect"] = "pending"
    pending["fields"]["intent_state"] = "started"
    assert await has_ambiguous_external_effect(None, [pending], owner_step=step)
    settled = ToolEffectEvidenceFactory.append_server_owned([pending], [
        ToolEffectEvidenceFactory.settlement(
            ToolEffectSettlementOutcome.RUNTIME_CONFIRMED_NOT_APPLIED,
            {**pending["fields"], "reason": "pre_io_validation"},
        ),
    ])
    assert not await has_ambiguous_external_effect(None, settled, owner_step=step)
    assert not await has_ambiguous_external_effect(None, [], owner_step=step)


@pytest.mark.asyncio
async def test_proven_root_result_is_consumed_without_executing_pending_write(monkeypatch):
    import packages.core.ai.agentic_loop as loop_module

    checkpoint = _checkpoint()
    assert _checkpoint_contains_tool_result(checkpoint, "write", effect_ref=_receipt(checkpoint))
    execute = AsyncMock(side_effect=AssertionError("Saved write must not execute again"))
    model = AsyncMock(return_value=("done", [], {}))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="Save once", user_message="Save the document",
        tools=[{"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}}],
        tool_executor=execute, resume_checkpoint=checkpoint, max_rounds=3,
    )
    assert result.content == "done"
    execute.assert_not_awaited()
    assert any(
        message.get("role") == "tool"
        and json.loads(message.get("content"))["id"] == "receipt-1"
        for message in result.messages
    )


def _nested_handoff_fixture():
    child = _checkpoint()
    child_id = _qualified_call_id("write", [("skill", "skill-1")])
    effect = _receipt(child, call_id=child_id)
    arguments = {"skill_id": "skill-1", "input": "save"}
    result = "The child returned."
    parent = RuntimeAgentCheckpoint(
        messages=[{"role": "assistant", "tool_calls": [{
            "id": "skill", "function": {"name": "invoke_skill", "arguments": json.dumps(arguments)},
        }]}], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "skill", "name": "invoke_skill", "arguments": arguments},
        pending_tool_result={"id": "skill", "content": result},
    ).to_dict()
    witness = ToolEffectEvidenceFactory.result_handoff(effect, {
        "tool": "invoke_skill", "tool_call_id": "skill", "arguments_hash": _tool_arguments_hash(arguments),
        "owner_step_id": "step", "owner_plan_id": "plan", "effect_generation": "0",
        "intent_state": "returned", "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
    })
    owner = SimpleNamespace(id="step", plan_id="plan", step_status="running", runtime_checkpoint=parent)
    return child, parent, effect, witness, owner


@pytest.mark.parametrize("mismatch", [
    None, "no_witness", "foreign_step", "foreign_plan", "generation", "arguments",
    "result", "source_digest", "source_id", "self_cycle", "malformed_fields", "weak_source",
])
def test_handoff_proves_only_exact_host_parent_continuation(mismatch):
    _, parent, effect, witness, owner = _nested_handoff_fixture()
    if mismatch == "foreign_step":
        witness["fields"]["owner_step_id"] = "foreign"
    elif mismatch == "foreign_plan":
        witness["fields"]["owner_plan_id"] = "foreign"
    elif mismatch == "generation":
        witness["fields"]["effect_generation"] = "1"
    elif mismatch == "arguments":
        parent["pending_tool_call"]["arguments"]["input"] = "different"
    elif mismatch == "result":
        parent["pending_tool_result"]["content"] = "different"
    elif mismatch == "source_digest":
        witness["fields"]["source_effect_digest"] = "a" * 64
    elif mismatch == "source_id":
        witness["fields"]["source_effect_id"] = "foreign"
    elif mismatch == "self_cycle":
        witness["fields"]["tool_call_id"] = effect["fields"]["tool_call_id"]
    elif mismatch == "malformed_fields":
        witness["fields"] = ["not-an-object"]
    elif mismatch == "weak_source":
        effect["fields"].pop("effect_id")
    refs = [effect] if mismatch == "no_witness" else [effect, witness]
    assert _checkpoint_proves_tool_effect(parent, effect, evidence_refs=refs, owner_step=owner) is (mismatch is None)


def test_handoff_survives_parent_transcript_without_claiming_parent_prose_is_success():
    _, parent, effect, witness, owner = _nested_handoff_fixture()
    transcript = _after([
        *parent["messages"], {"role": "tool", "tool_call_id": "skill", "content": "The child returned."},
    ])
    assert _checkpoint_proves_tool_effect(transcript, effect, evidence_refs=[effect, witness], owner_step=owner)
    assert not _checkpoint_contains_tool_result(transcript, effect["fields"]["tool_call_id"], effect_ref=effect)
    failed = copy.deepcopy(effect)
    failed["effect"] = "pending"
    assert not _checkpoint_proves_tool_effect(transcript, failed, evidence_refs=[failed, witness], owner_step=owner)


def test_untrusted_worker_cannot_install_handoff_or_chain_witnesses():
    _, parent, effect, witness, owner = _nested_handoff_fixture()
    refs = ToolEffectEvidenceFactory.merge_untrusted([effect], [witness])
    assert refs == [effect]
    assert not _checkpoint_proves_tool_effect(parent, effect, evidence_refs=refs, owner_step=owner)
    with pytest.raises(ValueError):
        ToolEffectEvidenceFactory.result_handoff(witness, witness["fields"])
    weak = copy.deepcopy(effect)
    weak["fields"].pop("effect_id")
    with pytest.raises(ValueError):
        ToolEffectEvidenceFactory.result_handoff(weak, witness["fields"])


@pytest.mark.parametrize("kind", ["pending", "transcript", "completed"])
def test_historical_child_observation_is_scoped_to_its_real_replay_frame(kind):
    child, parent, effect, _, _ = _nested_handoff_fixture()
    messages = _transcript(child)["messages"]
    state = {"version": 1, "skill_id": "skill-1", "kind": kind}
    if kind == "pending":
        child["messages"] = messages
        child["pending_tool_call"] = {"id": "second", "name": "write_file", "arguments": {}}
        child["pending_tool_result"] = None
        state["checkpoint"] = child
    elif kind == "transcript":
        state.update(messages=messages, rounds=2, usage={}, tool_calls_made=["write_file"])
    else:
        state.update(messages=messages, response={"content": "child returned"})
    parent["pending_tool_result"] = None
    parent["tool_continuations"] = {"skill": {"nested_skill": state}}
    assert _checkpoint_contains_tool_result(parent, effect["fields"]["tool_call_id"], effect_ref=effect, within_parent_call_id="skill")
    assert not _checkpoint_contains_tool_result(parent, effect["fields"]["tool_call_id"], effect_ref=effect, within_parent_call_id="other")
    parent["tool_continuations"]["skill"]["nested_skill"]["skill_id"] = "different-skill"
    assert not _checkpoint_contains_tool_result(parent, effect["fields"]["tool_call_id"], effect_ref=effect)


@pytest.mark.parametrize("levels", [8, 9])
def test_nested_observation_proof_has_the_runtime_depth_bound(levels):
    leaf = _checkpoint()
    path = [(f"parent-{index}", f"skill-{index}") for index in range(levels)]
    call_id = _qualified_call_id("write", path)
    effect = _receipt(leaf, call_id=call_id)
    tree = leaf
    for parent_id, skill_id in reversed(path):
        tree = RuntimeAgentCheckpoint(
            messages=[], usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call={"id": parent_id, "name": "invoke_skill", "arguments": {"skill_id": skill_id}},
            tool_continuations={parent_id: {"nested_skill": {
                "version": 1, "skill_id": skill_id, "kind": "pending", "checkpoint": tree,
            }}},
        ).to_dict()
    assert _checkpoint_contains_tool_result(tree, call_id, effect_ref=effect) is (levels == 8)
