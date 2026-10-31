"""RuntimeToolEvidence keeps the host receipt ledger."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult


def _doc(document_id: str) -> str:
    return json.dumps({"created": True, "document": {
        "document_id": document_id, "fs_path": f"out/{document_id}.docx", "source_sha256": f"sha-{document_id}",
    }})


def _child_evidence(child: RuntimeToolEvidence) -> list[dict]:
    """A child ledger as the host hands it to its parent (carrier or channel)."""
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    return runtime_child_evidence_from_state(child.checkpoint_state())


def test_successful_artifact_call_gets_a_handle_and_receipt():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-1", _doc("d1"), {"kind": "document"})

    state = ledger.checkpoint_state()
    assert state["observations"][0]["effect_state"] == "confirmed"
    assert state["observations"][0]["receipt_handles"] == ["r1"]
    assert state["receipts"] == [{
        "handle": "r1", "call_id": "call-1", "tool": "generate_file", "effect_state": "confirmed",
        "kind": "file", "operation": "document", "document_id": "d1",
        "fs_path": "out/d1.docx", "sha256": "sha-d1",
    }]
    assert state["effect_state"] == "confirmed"
    assert state["unconfirmed_calls"] == []


def test_failed_action_is_recorded_as_unconfirmed_without_receipt():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-1", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})

    state = ledger.checkpoint_state()
    assert state["receipts"] == []
    assert state["effect_state"] == "unconfirmed"
    assert state["unconfirmed_calls"] == [{"call_id": "call-1", "tool": "generate_file", "operation": "generate_file"}]


def test_waited_media_jobs_yield_receipts_even_when_batch_status_failed():
    ledger = RuntimeToolEvidence()
    waited = json.dumps({"status": "failed", "jobs": [
        {"job_id": "j1", "status": "completed", "fs_path": "v.mp4", "document_id": "d9", "result_url": "/api/v1/fs/e/v.mp4"},
        {"job_id": "j2", "status": "failed"},
    ]})
    ledger.observe_tool_result("wait_media_jobs", "w-1", waited, {})

    receipts = ledger.checkpoint_state()["receipts"]
    assert [(r["document_id"], r["external_id"]) for r in receipts] == [("d9", "j1")]


def test_receipts_survive_observation_eviction():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-0", _doc("first"), {"kind": "document"})
    for i in range(1, 200):
        ledger.observe_tool_result("read_file", f"read-{i}", json.dumps({"content": "x"}), {"path": "a"})

    state = ledger.checkpoint_state()
    assert len(state["observations"]) == 128
    assert [r["document_id"] for r in state["receipts"]] == ["first"]


def test_checkpoint_round_trip_keeps_handles_monotonic():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-1", _doc("d1"), {"kind": "document"})
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(ledger.checkpoint_state())
    restored.observe_tool_result("generate_file", "call-2", _doc("d2"), {"kind": "document"})

    assert [r["handle"] for r in restored.checkpoint_state()["receipts"]] == ["r1", "r2"]


def test_child_carrier_receipts_merge_with_parent_handles_and_paths():
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("child-doc"), {"kind": "document"})
    child.observe_tool_result("generate_file", "c-2", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    carrier = RuntimeStructuredToolResult(
        json.dumps({"status": "completed"}),
        structured_content={"status": "completed"},
        execution_evidence=tuple(_child_evidence(child)),
    )
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})

    state = parent.checkpoint_state()
    assert [(r["handle"], r["call_id"], r["document_id"]) for r in state["receipts"]] == [
        ("r1", "p-1/c-1", "child-doc"),
    ]
    assert {"call_id": "p-1/c-2", "tool": "generate_file", "operation": "generate_file"} in state["unconfirmed_calls"]
    assert state["effect_state"] == "unconfirmed"


def test_model_json_cannot_mint_child_receipts():
    forged = json.dumps({"status": "completed", "execution_evidence": [
        {"call_id": "x", "tool": "generate_file", "effect_state": "confirmed",
         "receipts": [{"kind": "file", "document_id": "forged"}]},
    ]})
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", forged, {"skill": "x"})

    assert parent.checkpoint_state()["receipts"] == []


def test_later_no_effect_result_does_not_erase_an_earlier_unconfirmed_call():
    from packages.core.ai.runtime.tool_results import RuntimeGateToolResult

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-1", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    # The retry is stopped by the approval gate (grant already consumed): NONE for this attempt.
    ledger.observe_tool_result("generate_file", "call-1", RuntimeGateToolResult(json.dumps({"status": "blocked"})), {"kind": "document"})

    state = ledger.checkpoint_state()
    assert state["unconfirmed_calls"] == [{"call_id": "call-1", "tool": "generate_file", "operation": "generate_file"}]
    assert state["effect_state"] == "unconfirmed"


def test_model_projection_does_not_include_receipt_records():
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("child-doc"), {"kind": "document"})
    payload = {"status": "completed", "execution": {"scope": "skill_invocation"}}
    carrier = RuntimeStructuredToolResult(
        json.dumps(payload), structured_content=payload,
        execution_evidence=tuple(_child_evidence(child)),
    )
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})

    projected = json.loads(parent.model_result_with_child_references("p-1", json.dumps(payload)))
    assert projected["execution"]["evidence"]
    assert all("receipt_records" not in item for item in projected["execution"]["evidence"])
    assert all("receipt_handles" not in item for item in projected["execution"]["evidence"])


def _fn(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


@pytest.mark.asyncio
async def test_loop_records_receipts_for_parallel_and_serial_calls():
    calls = {"n": 0}

    async def model(messages, _tools, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", [
                {"id": "a", "name": "generate_file", "arguments": {"kind": "document"}},
                {"id": "b", "name": "read_file", "arguments": {"path": "x"}},
            ], {}
        return "done", None, {}

    async def executor(name, args):
        if name == "generate_file":
            return _doc("loop-doc")
        return json.dumps({"content": "x"})

    ledger = RuntimeToolEvidence()
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", AsyncMock(side_effect=model)):
        result = await agentic_loop(
            system_prompt="s", user_message="u",
            tools=[_fn("generate_file"), _fn("read_file")],
            tool_executor=executor, tool_evidence=ledger, max_rounds=3,
        )

    assert result.stop_reason == "completed"
    state = ledger.checkpoint_state()
    assert [r["document_id"] for r in state["receipts"]] == ["loop-doc"]
    assert {o["call_id"]: o["effect_state"] for o in state["observations"]} == {"a": "confirmed", "b": "none"}


def _evict(ledger, n=130):
    for i in range(n):
        ledger.observe_tool_result("read_file", f"evict-{i}", json.dumps({"content": "x"}), {"path": "a"})


def test_confirmed_effect_survives_observation_eviction():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("patch_file", "p-1", json.dumps({"patched": True}), {"path": "a.md"})
    assert ledger.effect_state == "confirmed"
    _evict(ledger)

    state = ledger.checkpoint_state()
    assert "p-1" not in {o["call_id"] for o in state["observations"]}
    assert state["effect_state"] == "confirmed"


def test_pending_media_then_waited_receipt_stays_confirmed_after_eviction():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result(
        "generate_file", "g-1", json.dumps({"status": "pending", "job_id": "j1"}), {"kind": "video"},
    )
    ledger.observe_tool_result("wait_media_jobs", "w-1", json.dumps({"status": "completed", "jobs": [
        {"job_id": "j1", "status": "completed", "fs_path": "v.mp4", "document_id": "d9"},
    ]}), {})
    _evict(ledger)

    state = ledger.checkpoint_state()
    assert [r["effect_state"] for r in state["receipts"]] == ["confirmed"]
    assert state["effect_state"] == "confirmed"


def test_merged_child_without_effect_state_is_unconfirmed_after_eviction():
    legacy_child = {
        "call_id": "c-1", "tool": "generate_file", "operation": "generate_file",
        "stage": "execution", "result": "returned",
    }
    carrier = RuntimeStructuredToolResult(
        json.dumps({"status": "completed"}),
        structured_content={"status": "completed"},
        execution_evidence=(legacy_child,),
    )
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})
    assert ledger.observations["p-1/c-1"]["effect_state"] == "unconfirmed"
    _evict(ledger)

    state = ledger.checkpoint_state()
    assert {"call_id": "p-1/c-1", "tool": "generate_file", "operation": "generate_file"} in state["unconfirmed_calls"]
    assert state["effect_state"] == "unconfirmed"


def test_effect_floor_round_trips_through_checkpoint():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("patch_file", "p-1", json.dumps({"patched": True}), {"path": "a.md"})
    _evict(ledger)
    state = ledger.checkpoint_state()
    assert state["effect_floor"] == "confirmed"

    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(state)
    assert restored.effect_state == "confirmed"
    _evict(restored)
    assert restored.checkpoint_state()["effect_floor"] == "confirmed"


def test_restored_observation_without_effect_state_is_unconfirmed():
    # A ledger-era checkpoint (it has effect_floor): a missing state proves nothing.
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state({"effect_floor": "none", "observations": [
        {"call_id": "old-1", "tool": "write_file", "operation": "write_file", "stage": "execution"},
    ]})
    assert restored.observations["old-1"]["effect_state"] == "unconfirmed"
    assert restored.effect_state == "unconfirmed"


def test_pre_ledger_checkpoint_infers_effect_state_from_result():
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state({"observations": [
        {"call_id": "ok-1", "tool": "write_file", "operation": "write_file", "stage": "execution", "result": "returned"},
        {"call_id": "bad-1", "tool": "patch_file", "operation": "patch_file", "stage": "execution", "result": "error"},
    ]})

    assert restored.observations["ok-1"]["effect_state"] == "confirmed"
    assert restored.observations["bad-1"]["effect_state"] == "unconfirmed"
    state = restored.checkpoint_state()
    assert state["unconfirmed_calls"] == [{"call_id": "bad-1", "tool": "patch_file", "operation": "patch_file"}]
    assert state["effect_state"] == "unconfirmed"


def test_pre_ledger_checkpoint_with_only_returned_calls_is_confirmed():
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state({"observations": [
        {"call_id": "ok-1", "tool": "write_file", "operation": "write_file", "stage": "execution", "result": "returned"},
        {"call_id": "ok-2", "tool": "read_file", "operation": "read_file", "stage": "execution", "result": "returned"},
    ]})

    assert restored.effect_state == "confirmed"
    assert restored.checkpoint_state()["unconfirmed_calls"] == []


def test_call_tool_gateway_projects_receipts_for_the_inner_tool():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result(
        "call_tool", "g-1", _doc("gw-doc"), {"name": "generate_file", "arguments": {"kind": "document"}},
    )
    ledger.observe_tool_result(
        "call_tool", "g-2", json.dumps({"content": "x"}), {"name": "read_file", "arguments": {"path": "a"}},
    )

    state = ledger.checkpoint_state()
    assert [(r["tool"], r["operation"], r["document_id"]) for r in state["receipts"]] == [
        ("generate_file", "document", "gw-doc"),
    ]
    assert {o["call_id"]: o["effect_state"] for o in state["observations"]} == {"g-1": "confirmed", "g-2": "none"}


def test_chat_validation_feedback_facts_carry_no_ledger_handles():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("child-doc"), {"kind": "document"})
    carrier = RuntimeStructuredToolResult(
        json.dumps({"status": "completed"}), structured_content={"status": "completed"},
        execution_evidence=tuple(_child_evidence(child)),
    )
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("generate_file", "call-1", _doc("d1"), {"kind": "document"})
    contract.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})
    assert contract.observations["call-1"]["receipt_handles"] == ["r1"]

    facts = contract._feedback("operation_evidence_insufficient", "m").diagnostic["details"]["facts"]
    assert {item["call_id"] for item in facts} == {"call-1", "p-1", "p-1/c-1"}
    assert all("receipt_handles" not in item and "receipt_records" not in item for item in facts)
    # The host ledger itself is untouched.
    assert contract.observations["call-1"]["receipt_handles"] == ["r1"]


def test_receipt_cap_marks_the_ledger_truncated_and_persists(monkeypatch):
    from packages.core.ai.runtime import tool_evidence

    monkeypatch.setattr(tool_evidence, "_MAX_RECEIPTS", 2)
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "call-1", _doc("d1"), {"kind": "document"})
    ledger.observe_tool_result("generate_file", "call-2", _doc("d2"), {"kind": "document"})
    assert ledger.checkpoint_state()["receipts_truncated"] is False
    ledger.observe_tool_result("generate_file", "call-3", _doc("d3"), {"kind": "document"})

    state = ledger.checkpoint_state()
    assert [r["document_id"] for r in state["receipts"]] == ["d1", "d2"]
    assert state["receipts_truncated"] is True
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(state)
    assert restored.checkpoint_state()["receipts_truncated"] is True


# --- Task 5: child receipts reach the parent ledger structurally -------------


def test_child_evidence_channel_collects_only_inside_scope():
    from packages.core.ai.runtime.tool_evidence import (
        runtime_child_evidence_scope,
        runtime_publish_child_evidence,
    )

    runtime_publish_child_evidence([{"call_id": "outside"}])  # no scope: dropped silently
    with runtime_child_evidence_scope() as sink:
        runtime_publish_child_evidence([{"call_id": "c-1", "tool": "generate_file"}])
    assert sink == [{"call_id": "c-1", "tool": "generate_file"}]


@pytest.mark.asyncio
async def test_loop_merges_child_evidence_published_by_a_tool():
    from packages.core.ai.runtime.tool_evidence import runtime_publish_child_evidence

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("delegated-doc"), {"kind": "docx"})
    calls = {"n": 0}

    async def model(messages, _tools, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", [{"id": "d-1", "name": "delegate_like", "arguments": {}}], {}
        return "done", None, {}

    async def executor(name, args):
        # A plain JSON result, exactly like delegation: the receipts travel
        # through the host channel, not through this string.
        runtime_publish_child_evidence(_child_evidence(child))
        return json.dumps({"delegated": True, "status": "completed"})

    ledger = RuntimeToolEvidence()
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", AsyncMock(side_effect=model)):
        await agentic_loop(
            system_prompt="s", user_message="u", tools=[_fn("delegate_like")],
            tool_executor=executor, tool_evidence=ledger, max_rounds=3,
        )

    receipts = ledger.checkpoint_state()["receipts"]
    assert [(r["call_id"], r["document_id"]) for r in receipts] == [("d-1/c-1", "delegated-doc")]
    # Channel children reach the ledger only, never the citable window.
    assert list(ledger.observations) == ["d-1"]


def test_parallel_children_with_same_local_call_ids_do_not_collide():
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("workspace_agent", "d-1", json.dumps({"delegated": True}), {"action": "delegate"})
    a, b = RuntimeToolEvidence(), RuntimeToolEvidence()
    a.observe_tool_result("generate_file", "call_1", _doc("from-a"), {"kind": "docx"})
    b.observe_tool_result("generate_file", "call_1", _doc("from-b"), {"kind": "docx"})
    published = []
    for run_id, child in (("run-a", a), ("run-b", b)):
        published += [
            {**item, "call_id": f"{run_id}/{item['call_id']}"}
            for item in _child_evidence(child)
        ]
    parent.merge_child_observations("d-1", published)

    assert sorted(r["document_id"] for r in parent.checkpoint_state()["receipts"]) == ["from-a", "from-b"]


def _child_with_early_receipt_and_reads(n_reads=200):
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-0", _doc("early"), {"kind": "document"})
    child.observe_tool_result("generate_file", "c-bad", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    for i in range(n_reads):
        child.observe_tool_result("read_file", f"read-{i}", json.dumps({"content": "x"}), {"path": "a"})
    state = child.checkpoint_state()
    assert {"c-0", "c-bad"}.isdisjoint(o["call_id"] for o in state["observations"])
    return state


def test_child_evidence_from_state_carries_the_whole_child_ledger():
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    evidence = runtime_child_evidence_from_state(_child_with_early_receipt_and_reads())
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("workspace_agent", "d-1", json.dumps({"delegated": True}), {"action": "delegate"})
    parent.merge_child_observations("d-1", evidence)

    state = parent.checkpoint_state()
    assert [(r["handle"], r["call_id"], r["document_id"]) for r in state["receipts"]] == [("r1", "d-1/c-0", "early")]
    assert {"call_id": "d-1/c-bad", "tool": "generate_file", "operation": "generate_file"} in state["unconfirmed_calls"]
    assert state["effect_state"] == "unconfirmed"


def test_skill_carrier_carries_the_whole_child_ledger():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    carrier = runtime_format_invoke_skill_result("x", {
        "content": "done", "stop_reason": "completed",
        "tool_evidence": _child_with_early_receipt_and_reads(),
    })
    assert isinstance(carrier, RuntimeStructuredToolResult)
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})

    state = parent.checkpoint_state()
    assert [(r["call_id"], r["document_id"]) for r in state["receipts"]] == [("p-1/c-0", "early")]
    assert {"call_id": "p-1/c-bad", "tool": "generate_file", "operation": "generate_file"} in state["unconfirmed_calls"]


def test_merging_the_same_child_evidence_twice_registers_receipts_once():
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("once"), {"kind": "document"})
    child.observe_tool_result("generate_file", "c-2", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    evidence = _child_evidence(child)
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", evidence)
    parent.merge_child_observations("d-1", evidence)

    state = parent.checkpoint_state()
    assert [(r["handle"], r["document_id"]) for r in state["receipts"]] == [("r1", "once")]
    assert [c["call_id"] for c in state["unconfirmed_calls"]] == ["d-1/c-2"]

    # The merged set is persisted: a resumed run does not register them again.
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(state)
    restored.merge_child_observations("d-1", evidence)
    assert [r["document_id"] for r in restored.checkpoint_state()["receipts"]] == ["once"]
    assert [c["call_id"] for c in restored.checkpoint_state()["unconfirmed_calls"]] == ["d-1/c-2"]


def test_merged_child_call_ids_are_capped_in_the_checkpoint():
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", [
        {"call_id": f"c-{i}", "tool": "read_file", "effect_state": "none"} for i in range(2100)
    ])
    merged = parent.checkpoint_state()["merged_child_call_ids"]
    assert len(merged) == 2048
    assert merged[-1].startswith("d-1/c-2099#")


def test_channel_and_carrier_for_the_same_child_register_once():
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_scope, runtime_publish_child_evidence

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("both"), {"kind": "document"})
    observations = _child_evidence(child)
    carrier = RuntimeStructuredToolResult(
        json.dumps({"status": "completed"}), structured_content={"status": "completed"},
        execution_evidence=tuple(observations),
    )
    parent = RuntimeToolEvidence()
    with runtime_child_evidence_scope() as sink:
        runtime_publish_child_evidence(observations)
    parent.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})
    parent.merge_child_observations("p-1", sink)

    assert [r["document_id"] for r in parent.checkpoint_state()["receipts"]] == ["both"]


@pytest.mark.asyncio
async def test_delegation_publishes_the_child_ledger_with_run_prefix(client, db_session, monkeypatch):
    child_state = _child_with_early_receipt_and_reads()

    async def fake_loop(**kwargs):
        return _child_loop_result(child_state, messages=[
            {"role": "user", "content": kwargs["user_message"]}, {"role": "assistant", "content": "Wrote it"},
        ])

    result, sink = await _delegate_with_fake_loop(
        client, db_session, monkeypatch, fake_loop, username="receipt_delegate",
    )

    assert result["status"] == "completed"
    run_id = result["run_id"]
    assert sink and all(item["call_id"].startswith(f"{run_id}/") for item in sink)
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("workspace_agent", "d-1", json.dumps(result), {"action": "delegate_service"})
    parent.merge_child_observations("d-1", sink)
    state = parent.checkpoint_state()
    assert [(r["call_id"], r["document_id"]) for r in state["receipts"]] == [(f"d-1/{run_id}/c-0", "early")]
    assert {"call_id": f"d-1/{run_id}/c-bad", "tool": "generate_file", "operation": "generate_file"} in state["unconfirmed_calls"]


def test_a_later_child_run_reusing_local_call_ids_still_merges():
    first, second = RuntimeToolEvidence(), RuntimeToolEvidence()
    first.observe_tool_result("generate_file", "call_0", _doc("run-1"), {"kind": "document"})
    second.observe_tool_result("generate_file", "call_0", _doc("run-2"), {"kind": "document"})
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("p-1", _child_evidence(first))
    parent.merge_child_observations("p-1", _child_evidence(first))  # retried merge: skipped
    parent.merge_child_observations("p-1", _child_evidence(second))  # new run, same local id

    assert [(r["handle"], r["call_id"], r["document_id"]) for r in parent.checkpoint_state()["receipts"]] == [
        ("r1", "p-1/call_0", "run-1"), ("r2", "p-1/call_0", "run-2"),
    ]


def test_child_effect_floor_survives_a_retry_inside_the_window():
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    child.observe_tool_result("generate_file", "c-1", _doc("retried"), {"kind": "document"})
    state = child.checkpoint_state()
    assert state["effect_floor"] == "unconfirmed"
    assert {o["call_id"]: o["effect_state"] for o in state["observations"]} == {"c-1": "confirmed"}

    evidence = runtime_child_evidence_from_state(state)
    assert evidence[-1] == {
        "call_id": "~effect_floor", "tool": "ledger", "operation": "effect_floor", "effect_state": "unconfirmed",
    }
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", evidence)
    parent.merge_child_observations("d-1", evidence)  # adopted once
    merged = parent.checkpoint_state()
    assert [r["document_id"] for r in merged["receipts"]] == ["retried"]
    assert merged["unconfirmed_calls"] == [
        {"call_id": "d-1/~effect_floor", "tool": "ledger", "operation": "effect_floor"},
    ]
    assert merged["effect_state"] == "unconfirmed"
    # The floor marker is ledger state only, never an observation the model sees.
    assert all(o.get("operation") != "effect_floor" for o in merged["observations"])


def test_no_synthetic_floor_item_when_an_unconfirmed_item_is_emitted():
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    failed = RuntimeToolEvidence()
    failed.observe_tool_result("generate_file", "c-1", json.dumps({"status": "error", "error": "x"}), {"kind": "document"})
    confirmed = RuntimeToolEvidence()
    confirmed.observe_tool_result("generate_file", "c-1", _doc("ok"), {"kind": "document"})

    for child in (failed, confirmed):
        evidence = runtime_child_evidence_from_state(child.checkpoint_state())
        assert all(item["call_id"] != "~effect_floor" for item in evidence)


# --- Child ledgers merge into the parent ledger, not its citable window -------


def _answer(status, claims, limitations=()):
    return json.dumps({"kind": "answer", "text": "Done.", "outcome": {
        "status": status, "claims": claims, "limitations": list(limitations),
        "evidence_refs": list(dict.fromkeys(ref for claim in claims for ref in claim["evidence_refs"])),
    }})


@pytest.mark.asyncio
async def test_skill_carrier_with_evicted_child_receipt_does_not_break_chat_validation():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
    from packages.core.ai.runtime.final_response import FinalResponseAction
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-0", _doc("early"), {"kind": "document"})
    _evict(child, 130)
    carrier = runtime_format_invoke_skill_result("x", {
        "content": "done", "stop_reason": "completed", "tool_evidence": child.checkpoint_state(),
    })
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})

    # The rebuilt (evicted) child call is ledger-only, never a citable observation.
    assert "p-1/c-0" not in contract.observations
    assert all("stage" in item for item in contract.observations.values())
    assert [(r["call_id"], r["document_id"]) for r in contract.checkpoint_state()["receipts"]] == [
        ("p-1/c-0", "early"),
    ]
    decision = await contract.validate(_answer("completed", [
        {"operation": "read_file", "status": "completed", "evidence_refs": ["p-1/evict-129"]},
    ]))
    assert decision.action == FinalResponseAction.ACCEPT


def _delegated_children(n_agents=20, calls_each=10):
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    published = []
    for agent in range(n_agents):
        child = RuntimeToolEvidence()
        child.observe_tool_result("generate_file", "c-0", _doc(f"agent-{agent}"), {"kind": "document"})
        for i in range(1, calls_each):
            child.observe_tool_result("web_search", f"c-{i}", json.dumps({"results": []}), {"query": "q"})
        published += [
            {**item, "call_id": f"run-{agent}/{item['call_id']}"}
            for item in runtime_child_evidence_from_state(child.checkpoint_state())
        ]
    return published


@pytest.mark.asyncio
async def test_channel_children_stay_out_of_the_citable_window():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
    from packages.core.ai.runtime.final_response import FinalResponseAction

    children = _delegated_children()
    assert len(children) == 200
    delegated = json.dumps({"delegated": True, "status": "completed"})
    contract = ChatEvidenceContract()
    contract.observe_tool_result("workspace_agent", "d-1", delegated, {"action": "delegate_service"})
    contract.merge_child_observations("d-1", children, observe=False)

    # The parent's own call stays citable; the children reach only the ledger.
    assert list(contract.observations) == ["d-1"]
    assert sorted(r["document_id"] for r in contract.checkpoint_state()["receipts"]) == sorted(
        f"agent-{i}" for i in range(20)
    )
    assert contract.effect_state == "confirmed"
    decision = await contract.validate(_answer("completed", [
        {"operation": "workspace_agent.delegate_service", "status": "completed", "evidence_refs": ["d-1"]},
    ]))
    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.diagnostic is None

    # Child web searches are not the parent's own research attempt.
    research = ChatEvidenceContract(requires_execution=True)
    research.observe_tool_result("workspace_agent", "d-1", delegated, {"action": "delegate_service"})
    research.merge_child_observations("d-1", children, observe=False)
    decision = await research.validate(_answer("completed", [
        {"operation": "workspace_agent.delegate_service", "status": "completed", "evidence_refs": ["d-1"]},
    ]))
    assert decision.action != FinalResponseAction.ACCEPT
    assert decision.diagnostic["error"] == "current_research_evidence_required"


def test_skill_result_string_carries_only_the_model_view_of_child_evidence():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("child-doc"), {"kind": "document"})
    carrier = runtime_format_invoke_skill_result("x", {
        "content": "done", "stop_reason": "completed", "tool_evidence": child.checkpoint_state(),
    })
    assert isinstance(carrier, RuntimeStructuredToolResult)

    # The raw string reaches on_tool_end, the compaction archive and, once the
    # parent observation is evicted, the model: no ledger bookkeeping in it.
    assert "receipt_records" not in str(carrier)
    assert "receipt_handles" not in str(carrier)
    assert [item["call_id"] for item in json.loads(str(carrier))["execution"]["evidence"]] == ["c-1"]
    # The host carrier still delivers the child's receipts to the parent ledger.
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", carrier, {"skill": "x"})
    assert [r["document_id"] for r in parent.checkpoint_state()["receipts"]] == ["child-doc"]


def test_checkpoint_stores_receipts_once_and_child_evidence_reattaches_them():
    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_from_state

    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("in-window"), {"kind": "document"})
    state = child.checkpoint_state()
    assert all("receipt_records" not in item for item in state["observations"])
    assert [r["document_id"] for r in state["receipts"]] == ["in-window"]
    assert state["observations"][0]["receipt_handles"] == ["r1"]

    evidence = runtime_child_evidence_from_state(state)
    assert [(item["call_id"], [r["document_id"] for r in item["receipt_records"]]) for item in evidence] == [
        ("c-1", ["in-window"]),
    ]

    # A checkpoint written before this change still restores without them.
    legacy = {**state, "observations": [{**state["observations"][0], "receipt_records": state["receipts"]}]}
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(legacy)
    assert "receipt_records" not in restored.observations["c-1"]
    assert [r["document_id"] for r in restored.checkpoint_state()["receipts"]] == ["in-window"]


# --- Delegation ledger hand-off never fails a delegation ---------------------


async def _delegate_with_fake_loop(client, db_session, monkeypatch, fake_loop, *, username):
    from types import SimpleNamespace

    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_scope
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Agent, AgentSubscription

    register = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": "pass123", "entity_name": f"{username} Corp",
    })
    assert register.status_code == 200
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
    response = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Receipts"})
    assert response.status_code == 201
    workspace = response.json()
    agent_id, subscription_id, conversation_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add_all([
        Agent(id=agent_id, entity_id=workspace["entity_id"], name="Writer", status="active"),
        AgentSubscription(
            id=subscription_id, entity_id=workspace["entity_id"], workspace_id=workspace["id"],
            agent_id=agent_id, service_key="writer", status="active",
        ),
        Conversation(
            id=conversation_id, entity_id=workspace["entity_id"],
            workspace_id=workspace["id"], scope="workspace_main",
        ),
    ])
    await db_session.commit()

    async def fake_context(_db, **_kwargs):
        return SimpleNamespace(
            runtime_envelope=None, system_prompt="Writer instructions",
            tools=[{"type": "function", "function": {"name": "generate_file"}}],
            allowed_tool_names={"generate_file"}, task_id=None,
            tool_profile="workspace_agent", model="gpt-test", llm_metadata=None,
            turn_execution_plan=None,
        )

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_context)
    monkeypatch.setattr("packages.core.ai.runtime.harness.runtime_execute_chat_agent_loop", fake_loop)
    with runtime_child_evidence_scope() as sink:
        result = json.loads(await _workspace_agent_handler(
            entity_id=workspace["entity_id"], workspace_id=workspace["id"],
            conversation_id=conversation_id, user_id="receipt-user", action="delegate_service",
            params={"service_key": "writer", "prompt": "Write the report."},
        ))
    return result, sink


def _child_loop_result(child_state, **overrides):
    from types import SimpleNamespace

    values = {
        "content": "Wrote it", "messages": [{"role": "assistant", "content": "Wrote it"}],
        "rounds": 2, "tool_calls_made": [], "usage": {"total_tokens": 20},
        "stop_reason": "completed", "error": None, "control": None, "tool_evidence": child_state,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_a_failing_ledger_publish_does_not_fail_a_completed_delegation(client, db_session, monkeypatch):
    def broken_publish(_observations):
        raise RuntimeError("ledger bug")

    async def fake_loop(**_kwargs):
        return _child_loop_result(_child_with_early_receipt_and_reads())

    monkeypatch.setattr("packages.core.ai.runtime.tool_evidence.runtime_publish_child_evidence", broken_publish)
    result, _sink = await _delegate_with_fake_loop(
        client, db_session, monkeypatch, fake_loop, username="ledger_publish_fails",
    )

    assert result.get("error") is None
    assert result["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["hitl_required", "request_input"])
async def test_a_delegation_stopping_for_a_human_still_hands_over_its_ledger(client, db_session, monkeypatch, stop):
    from unittest.mock import AsyncMock as _AsyncMock

    from packages.core.ai.runtime.control import HUMAN_INPUT_SUSPENSION_KIND

    hitl = {"__hitl__": True, "error": "approval_required", "approval_token": "tok-1",
            "hitl": {"prompt": "Publish?", "tool": "publish_site"}}

    async def fake_loop(**kwargs):
        if stop == "hitl_required":
            kwargs["on_tool_end"]("publish_site", json.dumps(hitl), 0, {"site": "s"})
            return _child_loop_result(_child_with_early_receipt_and_reads(), stop_reason="hitl_required")
        return _child_loop_result(
            _child_with_early_receipt_and_reads(), stop_reason="waiting_resource",
            control={"kind": HUMAN_INPUT_SUSPENSION_KIND, "payload": {"question": "Which tone?"}},
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.workspace_delegation_actions._persist_delegation_resume",
        _AsyncMock(return_value=True),
    )
    result, sink = await _delegate_with_fake_loop(
        client, db_session, monkeypatch, fake_loop, username=f"ledger_stop_{stop}",
    )

    assert (result.get("__hitl__") is True) if stop == "hitl_required" else (result["status"] == "blocked")
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", sink, observe=False)
    assert [r["document_id"] for r in parent.checkpoint_state()["receipts"]] == ["early"]


@pytest.mark.asyncio
async def test_a_delegation_failing_after_its_child_loop_still_hands_over_its_ledger(client, db_session, monkeypatch):
    async def fake_loop(**_kwargs):
        return _child_loop_result(_child_with_early_receipt_and_reads())

    def broken_receipts(*_args, **_kwargs):
        raise RuntimeError("post-processing failed")

    monkeypatch.setattr(
        "packages.core.ai.runtime.workspace_delegation_actions._delegation_receipts", broken_receipts,
    )
    result, sink = await _delegate_with_fake_loop(
        client, db_session, monkeypatch, fake_loop, username="ledger_after_failure",
    )

    assert result["error"] == "workspace_service_delegation_failed"
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", sink, observe=False)
    assert [r["document_id"] for r in parent.checkpoint_state()["receipts"]] == ["early"]


# ── Non-file effects: classifier facts and the run's effects ledger ────


_CREATE_TASK = {"action": "workspace", "params": {"action": "create_task", "title": "Stray"}}


def _created_task() -> str:
    return json.dumps({"created": True, "task": {"id": "t-new", "title": "Stray"}})


def _keys(ledger) -> dict:
    return {group["action_key"]: group["count"] for group in ledger.checkpoint_state()["effects"]}


def test_action_observation_records_classifier_facts_and_a_ledger_effect():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)

    observation = ledger.observations["m-1"]
    assert observation["operation"] == "workspace_agent.create_task"
    assert observation["effect"] == "action"
    assert observation["action_key"] == "workspace.task.create"
    assert observation["resource_kind"] == "workspace_task"
    # Compact: titles are derived at output time, never stored per call.
    assert "action_title" not in observation
    state = ledger.checkpoint_state()
    assert state["effects"] == [{
        "action_key": "workspace.task.create", "resource_kind": "workspace_task",
        "operation": "create", "count": 1,
    }]
    assert state["effects_omitted"] == 0


def test_control_orchestrator_read_only_and_file_backed_calls_are_not_effects():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("update_task_plan", "plan", json.dumps({"status": "ok"}), {"items": []})
    ledger.observe_tool_result("invoke_skill", "skill", json.dumps({"ok": True}), {"skill": "x"})
    ledger.observe_tool_result("read_file", "read", json.dumps({"content": "x"}), {"path": "a.md"})
    ledger.observe_tool_result("generate_file", "gen", _doc("d1"), {"kind": "document"})

    # Classifier facts are kept for ACTION calls only.
    for call_id in ("plan", "skill", "read"):
        assert not {"effect", "action_key"} & set(ledger.observations[call_id]), call_id
    assert ledger.checkpoint_state()["effects"] == []


def test_classifier_facts_are_host_only():
    from packages.core.ai.runtime.tool_evidence import runtime_model_view_of_observation

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)
    observation = ledger.observations["m-1"]
    assert observation["effect"] == "action" and observation["action_operation"] == "create"

    view = runtime_model_view_of_observation(observation)
    assert not {"effect", "action_key", "resource_kind", "action_operation"} & set(view)
    assert view["operation"] == "workspace_agent.create_task" and view["effect_state"] == "confirmed"


def test_unconfirmed_action_is_not_an_effect():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", json.dumps({"error": "boom"}), _CREATE_TASK)

    assert ledger.checkpoint_state()["effects"] == []
    assert [c["call_id"] for c in ledger.unconfirmed_calls] == ["m-1"]


def test_classifier_failure_never_breaks_observation(monkeypatch):
    from packages.core.ai.runtime import approval_classifier

    def broken(*_args, **_kwargs):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(approval_classifier, "classify_runtime_tool", broken)
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)

    assert "effect" not in ledger.observations["m-1"]
    assert ledger.observations["m-1"]["effect_state"] == "confirmed"
    assert ledger.checkpoint_state()["effects"] == []


def test_each_observed_call_is_classified_once(monkeypatch):
    from packages.core.ai.runtime import approval_classifier

    calls = []
    real = approval_classifier.classify_runtime_tool

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(approval_classifier, "classify_runtime_tool", counting)
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)
    ledger.observe_tool_result("read_file", "r-1", json.dumps({"content": "x"}), {"path": "a.md"})

    assert calls == ["manor", "read_file"]


def test_non_file_receipt_identity_travels_with_the_effect():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result(
        "publish_site", "pub", json.dumps({"published": True, "url": "https://launch.example.test"}),
        {"path": "Sites/launch"},
    )

    [effect] = ledger.checkpoint_state()["effects"]
    assert effect["action_key"] == "workspace.site.publish"
    assert effect["targets"] == ["https://launch.example.test"]


def test_effects_survive_eviction_and_a_re_observed_call_counts_once():
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)
    _evict(ledger)
    assert "m-1" not in ledger.observations

    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(ledger.checkpoint_state())
    restored.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)  # same call again: once
    restored.observe_tool_result("manor", "m-2", _created_task(), _CREATE_TASK)
    assert _keys(restored) == {"workspace.task.create": 2}


def test_noisy_effect_classes_never_hide_a_created_task():
    ledger = RuntimeToolEvidence()
    for i in range(300):
        ledger.observe_tool_result("bash", f"sh-{i}", json.dumps({"ok": True, "stdout": ""}), {"command": "ls"})
    ledger.observe_tool_result("manor", "m-1", _created_task(), _CREATE_TASK)

    keys = _keys(ledger)
    assert keys["workspace.task.create"] == 1
    assert keys["cli.exec"] == 300
    assert ledger.checkpoint_state()["effects_omitted"] == 0


def test_effect_keys_are_capped_with_an_omitted_count(monkeypatch):
    from packages.core.ai.runtime import tool_evidence

    monkeypatch.setattr(tool_evidence, "_MAX_EFFECT_KEYS", 2)
    ledger = RuntimeToolEvidence()
    for call_id, action in (("a", "create_task"), ("b", "add_rule"), ("c", "create_knowledge_folder"), ("d", "add_rule")):
        ledger.observe_tool_result(
            "workspace_agent", call_id, json.dumps({"ok": True}), {"action": action, "title": "x"},
        )

    state = ledger.checkpoint_state()
    assert _keys(ledger) == {"workspace.task.create": 1, "workspace.rule.update": 2}
    assert state["effects_omitted"] == 1


def _child_with_evicted_effect() -> RuntimeToolEvidence:
    child = RuntimeToolEvidence()
    child.observe_tool_result("manor", "c-early", _created_task(), _CREATE_TASK)
    _evict(child, 200)  # read-only calls fill the window
    assert "c-early" not in child.observations
    return child


def test_delegated_child_effects_aggregate_once_and_leave_the_parent_effect_state_alone():
    child = _child_with_evicted_effect()
    before = RuntimeToolEvidence()
    before.merge_child_observations("d-1", [
        item for item in _child_evidence(child) if item.get("operation") != "effects"
    ], observe=False)

    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", _child_evidence(child), observe=False)
    parent.merge_child_observations("d-1", _child_evidence(child), observe=False)  # retried merge
    # The child does more and publishes its whole (cumulative) ledger again.
    child.observe_tool_result("manor", "c-late", _created_task(), _CREATE_TASK)
    parent.merge_child_observations("d-1", _child_evidence(child), observe=False)

    assert _keys(parent) == {"workspace.task.create": 2}
    # Informational only: the evicted effect does not move the ledger state.
    assert before.effect_state == "none"
    state = parent.checkpoint_state()
    assert state["effect_floor"] == "confirmed"  # c-late is in the child's window
    fresh = RuntimeToolEvidence()
    fresh.merge_child_observations("d-1", _child_evidence(_child_with_evicted_effect()), observe=False)
    assert fresh.effect_state == before.effect_state
    assert _keys(fresh) == {"workspace.task.create": 1}


def test_child_omitted_effects_reach_the_parent(monkeypatch):
    from packages.core.ai.runtime import tool_evidence

    monkeypatch.setattr(tool_evidence, "_MAX_EFFECT_KEYS", 1)
    child = RuntimeToolEvidence()
    child.observe_tool_result("workspace_agent", "a", json.dumps({"ok": True}), {"action": "create_task", "title": "x"})
    child.observe_tool_result("workspace_agent", "b", json.dumps({"ok": True}), {"action": "add_rule"})
    assert child.checkpoint_state()["effects_omitted"] == 1

    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", _child_evidence(child), observe=False)
    state = parent.checkpoint_state()
    assert _keys(parent) == {"workspace.task.create": 1}
    assert state["effects_omitted"] == 1


def test_effect_call_ids_are_bounded_in_the_checkpoint(monkeypatch):
    from packages.core.ai.runtime import tool_evidence

    monkeypatch.setattr(tool_evidence, "_MAX_EFFECT_CALL_IDS", 3)
    ledger = RuntimeToolEvidence()
    for i in range(5):
        ledger.observe_tool_result("manor", f"m-{i}", _created_task(), _CREATE_TASK)

    state = ledger.checkpoint_state()
    assert list(state["effect_calls"]) == ["m-2", "m-3", "m-4"]
    assert _keys(ledger) == {"workspace.task.create": 5}


def _delegate_through_the_real_publisher(child: RuntimeToolEvidence, run_id: str = "run-1") -> list[dict]:
    """What a delegation hands its calling tool call (run-prefixed call IDs)."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_scope
    from packages.core.ai.runtime.workspace_delegation_actions import _publish_delegated_child_ledger

    with runtime_child_evidence_scope() as sink:
        assert _publish_delegated_child_ledger(run_id, SimpleNamespace(tool_evidence=child.checkpoint_state()))
    return sink


def test_delegated_confirmed_action_keeps_the_parent_confirmed_and_counts_once():
    """Run-prefixed ledger markers are still markers (regression: UNCONFIRMED)."""

    child = RuntimeToolEvidence()
    child.observe_tool_result("manor", "c1", _created_task(), _CREATE_TASK)
    sink = _delegate_through_the_real_publisher(child)
    assert [item["call_id"] for item in sink] == ["run-1/c1", "run-1/~effects"]

    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", sink, observe=False)
    parent.merge_child_observations("d-1", _delegate_through_the_real_publisher(child), observe=False)

    state = parent.checkpoint_state()
    assert parent.effect_state == "confirmed"
    assert state["unconfirmed_calls"] == []
    assert _keys(parent) == {"workspace.task.create": 1}
    assert list(state["effect_calls"]) == ["d-1/run-1/c1"]


def test_delegated_effect_floor_marker_is_recognised_with_its_run_prefix():
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "same", json.dumps({"error": "x"}), {"kind": "md"})
    child.observe_tool_result("generate_file", "same", json.dumps({"status": "ok"}), {"kind": "md"})
    state = child.checkpoint_state()
    assert state["effect_floor"] == "unconfirmed" and state["unconfirmed_calls"]
    child.unconfirmed_calls = []  # only the sticky floor says so now
    sink = _delegate_through_the_real_publisher(child)
    assert "run-1/~effect_floor" in [item["call_id"] for item in sink]

    for observe in (False, True):
        parent = RuntimeToolEvidence()
        parent.merge_child_observations("d-1", sink, observe=observe)
        assert parent.effect_state == "unconfirmed"
        assert parent.checkpoint_state()["effect_floor"] == "unconfirmed"
        # The marker raises the floor; it never becomes a citable observation.
        assert "d-1/run-1/~effect_floor" not in parent.observations


def test_grandchild_effects_reach_the_root_once():
    grandchild = RuntimeToolEvidence()
    grandchild.observe_tool_result("manor", "g1", _created_task(), _CREATE_TASK)
    child = RuntimeToolEvidence()
    child.merge_child_observations("cd", _delegate_through_the_real_publisher(grandchild, "run-g"), observe=False)
    parent = RuntimeToolEvidence()
    parent.merge_child_observations("d-1", _delegate_through_the_real_publisher(child), observe=False)

    assert _keys(parent) == {"workspace.task.create": 1}
    assert list(parent.checkpoint_state()["effect_calls"]) == ["d-1/run-1/cd/run-g/g1"]


def test_cumulative_republish_adds_only_new_omitted_calls(monkeypatch):
    from packages.core.ai.runtime import tool_evidence

    # Small window and seen-set (the seen-set stays above the window, as the
    # module asserts), so the child forgets calls between republishes.
    monkeypatch.setattr(tool_evidence, "_OBSERVATION_WINDOW", 1)
    monkeypatch.setattr(tool_evidence, "_MAX_EFFECT_CALL_IDS", 2)

    def accounted(ledger) -> int:
        state = ledger.checkpoint_state()
        return sum(group["count"] for group in state["effects"]) + state["effects_omitted"]

    child = RuntimeToolEvidence()
    parent = RuntimeToolEvidence()
    for calls in range(1, 7):
        child.observe_tool_result("manor", f"c{calls}", _created_task(), _CREATE_TASK)
        # Every republish is the child's whole cumulative ledger.
        parent.merge_child_observations("d-1", _delegate_through_the_real_publisher(child), observe=False)
        assert accounted(child) == calls
        assert accounted(parent) == calls, calls

    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(parent.checkpoint_state())
    restored.merge_child_observations("d-1", _delegate_through_the_real_publisher(child), observe=False)
    assert accounted(restored) == 6


def test_effect_seen_set_outlives_the_observation_window():
    """A call still in the window can be observed again; it must still be seen."""
    from packages.core.ai.runtime import tool_evidence

    assert tool_evidence._MAX_EFFECT_CALL_IDS > tool_evidence._OBSERVATION_WINDOW


# --- A Skill call's effect state is its child ledger's --------------------


def _skill_result(child: RuntimeToolEvidence, **reply) -> RuntimeStructuredToolResult:
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    result = runtime_format_invoke_skill_result("video", {
        "content": "done", "stop_reason": "completed", "tool_evidence": child.checkpoint_state(), **reply,
    })
    assert isinstance(result, RuntimeStructuredToolResult)
    return result


@pytest.mark.parametrize("reply", [
    {},
    # The Skill reports its own verdict as not fully done (disclosed exceptions).
    {"content": json.dumps({"status": "PARTIAL", "notes": "one stem 0.1 LU low"})},
    {"stop_reason": "max_rounds"},
    {"error": "provider error after the render", "stop_reason": "error"},
], ids=["completed", "partial-verdict", "max-rounds", "error"])
def test_a_skill_that_committed_a_file_is_confirmed_whatever_its_reply_says(reply):
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _doc("render"), {"kind": "video"})
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", _skill_result(child, **reply), {"skill": "video"})

    state = parent.checkpoint_state()
    assert parent.observations["p-1"]["effect_state"] == "confirmed"
    assert state["unconfirmed_calls"] == [] and state["effect_state"] == "confirmed"
    assert [r["document_id"] for r in state["receipts"]] == ["render"]


def test_a_skill_that_wrote_nothing_is_none_and_an_uncertain_child_call_stays_unconfirmed():
    reads = RuntimeToolEvidence()
    reads.observe_tool_result("read_file", "c-1", json.dumps({"content": "x"}), {"path": "a.md"})
    nothing = RuntimeToolEvidence()  # the child loop ran and made no call
    failed = RuntimeToolEvidence()
    failed.observe_tool_result("generate_file", "c-1", json.dumps({"error": "timeout after upload"}), {"kind": "md"})

    for child, expected in ((reads, "none"), (nothing, "none"), (failed, "unconfirmed")):
        parent = RuntimeToolEvidence()
        parent.observe_tool_result("invoke_skill", "p-1", _skill_result(child), {"skill": "video"})
        assert parent.observations["p-1"]["effect_state"] == expected
        assert parent.effect_state == expected
    # Listed once, as the child call that may have had the effect.
    assert [c["call_id"] for c in parent.unconfirmed_calls] == ["p-1/c-1"]


def test_a_nested_skill_that_reported_failure_over_its_render_leaves_the_run_confirmed():
    """The production case: Skill A invokes Skill B, which renders the MP4 and
    reports a partial verdict. Neither call may stay unconfirmed."""
    grandchild = RuntimeToolEvidence()
    grandchild.observe_tool_result("generate_file", "g-1", _doc("final-mp4"), {"kind": "video"})
    child = RuntimeToolEvidence()
    child.observe_tool_result("invoke_skill", "c-1", _skill_result(
        grandchild, content=json.dumps({"status": "PARTIAL"}),
    ), {"skill": "topic-explainer"})
    for i in range(140):  # more child calls than the observation window
        child.observe_tool_result("read_file", f"c-read-{i}", json.dumps({"content": "x"}), {"path": "a"})
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", _skill_result(child), {"skill": "video-production"})

    state = parent.checkpoint_state()
    assert state["effect_state"] == "confirmed" and state["unconfirmed_calls"] == []
    assert [(r["call_id"], r["document_id"]) for r in state["receipts"]] == [("p-1/c-1/g-1", "final-mp4")]
    # The caller outlives its own children in the bounded window (the journal
    # reads its effect state there).
    assert list(parent.observations)[-1] == "p-1"
    assert parent.observations["p-1"]["effect_state"] == "confirmed"


# --- Reconciliation: a host read of the reported file confirms the call ----


def _uncertain_doc(document_id: str, sha256: str) -> str:
    # The file was registered, then a later step failed: unconfirmed.
    return json.dumps({"error": "thumbnail step failed", "document": {
        "document_id": document_id, "fs_path": f"out/{document_id}.md", "source_sha256": sha256,
    }})


def _host_read(document_id: str, sha256: str) -> str:
    return json.dumps({
        "path": f"out/{document_id}.md", "resolved_path": f"out/{document_id}.md",
        "document_id": document_id, "source_sha256": sha256, "size": 10,
    })


def test_a_host_read_with_the_reported_hash_confirms_an_unconfirmed_call():
    from packages.core.ai.runtime.tool_evidence import runtime_model_view_of_observation

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "g-1", _uncertain_doc("d1", "aa11"), {"kind": "md"})
    assert ledger.effect_state == "unconfirmed"
    assert ledger.unconfirmed_calls[0]["targets"] == [{"document_id": "d1", "fs_path": "out/d1.md", "sha256": "aa11"}]
    assert "effect_targets" not in runtime_model_view_of_observation(ledger.observations["g-1"])

    # Different bytes, or a read that measured no hash, confirm nothing.
    ledger.observe_tool_result("read_file", "r-1", _host_read("d1", "bb22"), {"document_id": "d1"})
    ledger.observe_tool_result("read_file", "r-2", json.dumps({"path": "out/d1.md", "content": "x"}), {"path": "out/d1.md"})
    assert ledger.effect_state == "unconfirmed"

    # Survives a restage; then the matching read (here through the gateway) confirms it.
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(ledger.checkpoint_state())
    restored.observe_tool_result(
        "call_tool", "r-3", _host_read("d1", "AA11"), {"name": "read_file", "arguments": {"document_id": "d1"}},
    )
    state = restored.checkpoint_state()
    assert state["unconfirmed_calls"] == [] and state["effect_state"] == "confirmed"
    assert restored.observations["g-1"]["effect_state"] == "confirmed"
    assert state["reconciled_calls"] == [
        {"call_id": "g-1", "tool": "generate_file", "operation": "generate_file", "reconciled_by": "r-3"},
    ]


def test_a_call_without_a_reported_hash_or_with_an_unobserved_target_stays_unconfirmed():
    ledger = RuntimeToolEvidence()
    # Reported nothing the host can check.
    ledger.observe_tool_result("generate_file", "g-1", json.dumps({"error": "x"}), {"kind": "md"})
    # Reported two files; each must be read back.
    two = json.dumps({"status": "partial", "files": [
        {"document_id": "d1", "fs_path": "out/d1.md", "sha256": "aa11"},
        {"document_id": "d2", "fs_path": "out/d2.md", "sha256": "bb22"},
    ]})
    ledger.observe_tool_result("generate_file", "g-2", two, {"kind": "md"})
    ledger.observe_tool_result("read_file", "r-1", _host_read("d1", "aa11"), {"document_id": "d1"})
    assert [c["call_id"] for c in ledger.unconfirmed_calls] == ["g-1", "g-2"]

    ledger.observe_tool_result("read_file", "r-2", _host_read("d2", "bb22"), {"document_id": "d2"})
    assert [c["call_id"] for c in ledger.unconfirmed_calls] == ["g-1"]
    assert ledger.effect_state == "unconfirmed"


def test_a_skill_s_unconfirmed_child_call_is_reconciled_by_a_later_parent_read():
    child = RuntimeToolEvidence()
    child.observe_tool_result("generate_file", "c-1", _uncertain_doc("d1", "aa11"), {"kind": "md"})
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "p-1", _skill_result(child), {"skill": "x"})
    assert parent.effect_state == "unconfirmed"
    assert [c["call_id"] for c in parent.unconfirmed_calls] == ["p-1/c-1"]

    parent.observe_tool_result("read_file", "r-1", _host_read("d1", "aa11"), {"document_id": "d1"})
    assert parent.unconfirmed_calls == [] and parent.effect_state == "confirmed"
    assert parent.observations["p-1"]["effect_state"] == "confirmed"


def test_a_later_confirmed_write_of_the_same_file_supersedes_an_uncertain_one():
    """Strict rule: an earlier uncertain write is settled when a later call
    replaced every file it reported with a host-confirmed artifact. Other
    bytes seen by a read, or an in-place edit on top, settle nothing."""
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "g-1", _uncertain_doc("d1", "aa11"), {"kind": "md"})
    # A read showing other bytes may be g-1's own partial output.
    ledger.observe_tool_result("read_file", "r-1", _host_read("d1", "ffff"), {"document_id": "d1"})
    # An in-place patch builds on whatever g-1 left.
    ledger.observe_tool_result("patch_file", "p-1", json.dumps({
        "patched": True, "document_id": "d1", "path": "out/d1.md", "source_sha256": "eeee",
    }), {"path": "out/d1.md"})
    assert [c["call_id"] for c in ledger.unconfirmed_calls] == ["g-1"]

    # A later full write of the same file with a confirmed receipt replaces it.
    ledger.observe_tool_result("generate_file", "g-2", json.dumps({"created": True, "document": {
        "document_id": "d1", "fs_path": "out/d1.md", "source_sha256": "bb22",
    }}), {"kind": "md"})
    state = ledger.checkpoint_state()
    assert state["unconfirmed_calls"] == [] and state["effect_state"] == "confirmed"
    assert state["reconciled_calls"] == [{
        "call_id": "g-1", "tool": "generate_file", "operation": "generate_file",
        "reconciled_by": "g-2", "superseded": True,
    }]


def test_a_handler_settled_failure_is_none_or_confirmed_and_never_from_json():
    from packages.core.ai.runtime.tool_results import RuntimeSettledToolResult

    refused = json.dumps({"error": "Command 'python' not in allowed list", "command_executed": False})
    stopped = json.dumps({"contract": "tool_error", "status": "error", "error": "render_failed"})
    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("bash", "b-1", RuntimeSettledToolResult.not_applied(refused, "command_policy"),
                               {"command": "python -c 1"})
    assert ledger.effect_state == "none" and ledger.unconfirmed_calls == []
    ledger.observe_tool_result("video_edit", "v-1", RuntimeSettledToolResult.completed_steps_only(
        stopped, "render_failed"), {"operation": "render"})
    assert ledger.effect_state == "confirmed" and ledger.unconfirmed_calls == []
    # The same text as plain JSON (a provider, a checkpoint) is an uncertain failure.
    ledger.observe_tool_result("bash", "b-2", refused, {"command": "python -c 1"})
    assert [c["call_id"] for c in ledger.unconfirmed_calls] == ["b-2"]
