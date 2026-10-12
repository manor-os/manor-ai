"""An approval stop that a durable host can resume at the gated call.

``hitl_resume_checkpoint=True`` (the Task agent loop) attaches the exact
pre-call continuation to a ``hitl_required`` stop: the gated call is pending,
every call before it keeps its result, every call after it is still queued.
Without the flag the control is unchanged for every other host.
"""
from __future__ import annotations

import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.receipts import EffectState
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
    }


def _envelope(request_id: str = "hitl-1") -> str:
    return json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": request_id,
        "hitl": {"id": request_id, "type": "approval", "prompt": "Publish?", "tool": "publish_post"},
    })


def _script(monkeypatch, rounds: list[list[dict]]):
    seen: list[list[dict]] = []

    async def fake_round(messages, _tools, **_kwargs):
        seen.append([dict(message) for message in messages])
        calls = rounds[min(len(seen), len(rounds)) - 1]
        return ("" if calls else "Done."), calls, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    return seen


BATCH = [
    {"id": "call-note", "name": "write_note", "arguments": {"text": "before"}},
    {"id": "call-gate", "name": "publish_post", "arguments": {"text": "launch"}},
    {"id": "call-after", "name": "write_note", "arguments": {"text": "after"}},
]


async def _stop_at_gate(monkeypatch, **kwargs):
    runs: list[str] = []

    async def executor(name, arguments):
        runs.append(f"{name}:{arguments.get('text')}")
        if name == "publish_post":
            return _envelope()
        return json.dumps({"ok": True})

    _script(monkeypatch, [BATCH])
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="work",
        tools=[_tool("write_note"), _tool("publish_post")],
        tool_executor=executor, max_rounds=3, **kwargs,
    )
    return result, runs


@pytest.mark.unit
async def test_resumable_approval_stop_carries_the_pre_call_checkpoint(monkeypatch):
    result, runs = await _stop_at_gate(monkeypatch, hitl_resume_checkpoint=True)

    assert result.stop_reason == "hitl_required"
    assert runs == ["write_note:before", "publish_post:launch"]
    checkpoint = RuntimeAgentCheckpoint.from_dict(result.control["checkpoint"])
    assert checkpoint.pending_tool_call["id"] == "call-gate"
    assert [call["id"] for call in checkpoint.remaining_tool_calls] == ["call-after"]
    tool_results = {m["tool_call_id"]: m["content"] for m in checkpoint.messages if m.get("role") == "tool"}
    # The call before the gate kept its result; the gated call has none yet.
    assert set(tool_results) == {"call-note"}
    assert json.loads(tool_results["call-note"]) == {"ok": True}


@pytest.mark.unit
async def test_other_hosts_get_the_unchanged_approval_stop(monkeypatch):
    result, _runs = await _stop_at_gate(monkeypatch)

    assert result.stop_reason == "hitl_required"
    assert "checkpoint" not in result.control


@pytest.mark.unit
async def test_resume_re_executes_the_gated_call_then_the_queued_one(monkeypatch):
    result, _ = await _stop_at_gate(monkeypatch, hitl_resume_checkpoint=True)
    runs: list[str] = []

    async def executor(name, arguments):
        runs.append(f"{name}:{arguments.get('text')}")
        return json.dumps({"ok": True, "published": name == "publish_post"})

    seen = _script(monkeypatch, [[]])
    resumed = await loop_module.agentic_loop(
        system_prompt="system", user_message="work",
        tools=[_tool("write_note"), _tool("publish_post")],
        tool_executor=executor, max_rounds=3,
        resume_checkpoint=result.control["checkpoint"], hitl_resume_checkpoint=True,
    )

    assert resumed.stop_reason == "completed"
    # Approved: the gated call runs exactly once, then the queued call; the
    # call that already ran before the gate is not repeated.
    assert runs == ["publish_post:launch", "write_note:after"]
    results = [m for m in seen[0] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call-note", "call-gate", "call-after"]


@pytest.mark.unit
async def test_a_host_written_gate_result_is_replayed_as_no_effect(monkeypatch):
    result, _ = await _stop_at_gate(monkeypatch, hitl_resume_checkpoint=True)
    checkpoint = dict(result.control["checkpoint"])
    rejection = json.dumps({"status": "rejected", "error": "approval_rejected", "action_executed": False})
    checkpoint["pending_tool_result"] = {"id": "call-gate", "content": rejection, "gate": True}
    runs: list[str] = []

    async def executor(name, arguments):
        runs.append(name)
        return json.dumps({"ok": True})

    seen = _script(monkeypatch, [[]])
    evidence = RuntimeToolEvidence()
    resumed = await loop_module.agentic_loop(
        system_prompt="system", user_message="work",
        tools=[_tool("write_note"), _tool("publish_post")],
        tool_executor=executor, max_rounds=3, tool_evidence=evidence,
        resume_checkpoint=checkpoint, hitl_resume_checkpoint=True,
    )

    assert resumed.stop_reason == "completed"
    assert runs == ["write_note"]  # the rejected call never ran; the queued one did
    gate = next(m for m in seen[0] if m.get("tool_call_id") == "call-gate")
    assert json.loads(gate["content"])["status"] == "rejected"
    # Host proof that the gate stopped the call: never an unconfirmed effect.
    assert evidence.effect_state != EffectState.UNCONFIRMED.value
    assert "call-gate" not in {call.get("call_id") for call in evidence.unconfirmed_calls}


@pytest.mark.unit
async def test_a_plain_stored_result_keeps_its_existing_ledger_meaning(monkeypatch):
    result, _ = await _stop_at_gate(monkeypatch, hitl_resume_checkpoint=True)
    checkpoint = dict(result.control["checkpoint"])
    checkpoint["pending_tool_result"] = {
        "id": "call-gate", "content": json.dumps({"status": "rejected", "error": "approval_rejected"}),
    }

    async def executor(name, arguments):
        return json.dumps({"ok": True})

    _script(monkeypatch, [[]])
    evidence = RuntimeToolEvidence()
    await loop_module.agentic_loop(
        system_prompt="system", user_message="work",
        tools=[_tool("write_note"), _tool("publish_post")],
        tool_executor=executor, max_rounds=3, tool_evidence=evidence,
        resume_checkpoint=checkpoint,
    )

    assert "call-gate" in {call.get("call_id") for call in evidence.unconfirmed_calls}


@pytest.mark.unit
async def test_a_parallel_batch_keeps_its_siblings_results_and_queues_other_gates(monkeypatch):
    batch = [
        {"id": "read-a", "name": "web_search", "arguments": {"query": "a"}},
        {"id": "read-gate", "name": "web_search", "arguments": {"query": "gate"}},
        {"id": "read-gate-2", "name": "web_search", "arguments": {"query": "gate-2"}},
    ]

    async def executor(name, arguments):
        if arguments.get("query", "").startswith("gate"):
            return _envelope(f"hitl-{arguments['query']}")
        return json.dumps({"ok": True, "results": []})

    _script(monkeypatch, [batch])
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="work", tools=[_tool("web_search")],
        tool_executor=executor, max_rounds=3, hitl_resume_checkpoint=True,
    )

    assert result.stop_reason == "hitl_required"
    checkpoint = RuntimeAgentCheckpoint.from_dict(result.control["checkpoint"])
    assert checkpoint.pending_tool_call["id"] == "read-gate"
    # The sibling that ran keeps its result; the other gated one re-executes.
    assert [call["id"] for call in checkpoint.remaining_tool_calls] == ["read-gate-2"]
    answered = {m["tool_call_id"] for m in checkpoint.messages if m.get("role") == "tool"}
    assert answered == {"read-a"}


@pytest.mark.unit
async def test_a_gate_flag_is_ignored_by_a_loop_that_does_not_park_approvals(monkeypatch):
    result, _ = await _stop_at_gate(monkeypatch, hitl_resume_checkpoint=True)
    checkpoint = dict(result.control["checkpoint"])
    checkpoint["pending_tool_result"] = {
        "id": "call-gate", "content": json.dumps({"status": "rejected"}), "gate": True,
    }

    async def executor(name, arguments):
        return json.dumps({"ok": True})

    _script(monkeypatch, [[]])
    evidence = RuntimeToolEvidence()
    await loop_module.agentic_loop(
        system_prompt="system", user_message="work",
        tools=[_tool("write_note"), _tool("publish_post")],
        tool_executor=executor, max_rounds=3, tool_evidence=evidence,
        resume_checkpoint=checkpoint,
    )

    # Only a host that parks approval stops may write that proof.
    assert "call-gate" in {call.get("call_id") for call in evidence.unconfirmed_calls}
