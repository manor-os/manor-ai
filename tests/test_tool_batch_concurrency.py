"""Independent gate-free calls of one model round may overlap.

The plan builder previews each call's real classification and receipt-bound
file targets; the loop runs the plan with the real AgentLoop and deterministic
model/tool adapters. No database, provider or sandbox is required.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import packages.core.ai.agentic_loop as loop
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalPreview, RuntimeApprovalRequest
from packages.core.ai.runtime.media_outputs import prepare_runtime_media_output_plan
from packages.core.ai.runtime.tool_batch_concurrency import runtime_plan_tool_batch
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeWorkspaceFileScope,
    WorkspaceFileExistence,
)
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
from packages.core.database_runtime import database_admission_group


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
BASE = "Workspaces/_by_id/folder-1/tasks/task-1"


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _call(call_id: str, tool_name: str, **arguments) -> dict:
    return {"id": call_id, "name": tool_name, "arguments": arguments}


def _previewer(entity_root: str, *, gated: set[str] = frozenset()):
    """The live preview's shape: real file scope and classification."""

    async def preview(call: dict) -> RuntimeApprovalPreview:
        name = call["name"]
        arguments = {
            key: value for key, value in call["arguments"].items() if not key.startswith("_")
        }
        plan = prepare_runtime_media_output_plan(
            tool_name=name, arguments=arguments, artifact_base_dir=BASE, entity_root=entity_root,
        )
        scope = RuntimeWorkspaceFileScope(WorkspaceFileExistence.MISSING, BASE, plan)
        request = RuntimeApprovalRequest(
            tool_name=name, arguments=arguments, entity_id="entity", user_id="user",
            workspace_id="ws", task_id="task-1", workspace_file_scope=scope,
            classification=classify_runtime_tool(
                name, arguments, entity_id="entity", workspace_id="ws",
                task_id="task-1", workspace_file_scope=scope,
            ),
        )
        return RuntimeApprovalPreview(clear=call["id"] not in gated, request=request)

    return preview


@pytest.mark.parametrize("batch", [
    [  # media generation
        _call("img-1", "generate_image", prompt="a red fox in snow"),
        _call("img-2", "generate_image", prompt="a lighthouse at dusk"),
        _call("vo-1", "generate_file", kind="audio", name="intro-voiceover", prompt="hello"),
    ],
    [  # an unrelated scenario: two different Workspace documents
        _call("doc-1", "write_file", path="plan.md", content="plan"),
        _call("doc-2", "write_file", path="budget.md", content="budget"),
    ],
], ids=["media", "documents"])
async def test_plan_overlaps_gate_free_calls_on_disjoint_targets(tmp_path, batch):
    plan = await runtime_plan_tool_batch(batch, preview=_previewer(str(tmp_path)))

    assert plan is not None
    assert plan.dependencies() == tuple(frozenset() for _ in batch)


async def test_plan_orders_calls_that_share_a_target(tmp_path):
    batch = [
        _call("a", "write_file", path="plan.md", content="v1"),
        _call("b", "write_file", path="budget.md", content="b"),
        _call("c", "edit_file", path="plan.md", old_string="v1", new_string="v2"),
        _call("d", "web_search", query="unknown target"),
    ]

    plan = await runtime_plan_tool_batch(batch, preview=_previewer(str(tmp_path)))

    assert plan is not None
    # The edit waits for the write of the same file; a call without a
    # derivable target waits for every earlier call.
    assert plan.dependencies() == (
        frozenset(), frozenset(), frozenset({0}), frozenset({0, 1, 2}),
    )


async def _failing_preview(_call):
    raise RuntimeError("policy store unavailable")


@pytest.mark.parametrize(("batch", "preview_kind"), [
    ([_call("a", "write_file", path="a.md", content="x"),
      _call("gated", "write_file", path="b.md", content="y")], "gated"),
    ([_call("a", "write_file", path="a.md", content="x"),
      _call("b", "write_file", path="b.md", content="y")], "fails"),
    ([_call("a", "write_file", path="a.md", content="x"),
      _call("b", "future_vendor_mutation", target="external")], "real"),
    ([_call("a", "write_file", path="a.md", content="x"),
      _call("b", "request_input", question="Which one?")], "real"),
    ([_call("a", "write_file", path="a.md", content="x"),
      _call("b", "write_file", path="a.md", content="y")], "real"),
], ids=["human-gate", "preview-failure", "unclassified", "parks-turn", "same-target-only"])
async def test_plan_keeps_round_serial_on_any_doubt(tmp_path, batch, preview_kind):
    preview = {
        "gated": _previewer(str(tmp_path), gated={"gated"}),
        "fails": _failing_preview,
        "real": _previewer(str(tmp_path)),
    }[preview_kind]

    assert await runtime_plan_tool_batch(batch, preview=preview) is None


def _planner(entity_root: str, **kwargs):
    preview = _previewer(entity_root, **kwargs)
    calls: list[list[str]] = []

    async def plan(batch):
        calls.append([call["id"] for call in batch])
        return await runtime_plan_tool_batch(batch, preview=preview)

    plan.calls = calls
    return plan


def _model(batch):
    rounds = 0

    async def model(_messages, _tools, **_kwargs):
        nonlocal rounds
        rounds += 1
        return ("", batch, {}) if rounds == 1 else ("done", [], {})

    return model


async def test_loop_runs_independent_mutations_concurrently(monkeypatch, tmp_path):
    batch = [
        _call("doc-1", "write_file", path="plan.md", content="plan"),
        _call("doc-2", "write_file", path="budget.md", content="budget"),
    ]
    started = {call["arguments"]["path"]: asyncio.Event() for call in batch}
    groups = []
    overlapped: list[str] = []

    async def execute(name, arguments):
        path = arguments["path"]
        started[path].set()
        groups.append(database_admission_group.get())
        other = next(event for key, event in started.items() if key != path)
        # Serial execution would never start the other call: time out here.
        await asyncio.wait_for(other.wait(), timeout=5)
        overlapped.append(path)
        return json.dumps({"ok": True, "path": path})

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", _model(batch))
    result = await loop.agentic_loop(
        system_prompt="system", user_message="write both", tools=[_tool("write_file")],
        tool_executor=execute, tool_batch_planner=_planner(str(tmp_path)), max_rounds=3,
    )

    assert result.content == "done"
    assert sorted(overlapped) == ["budget.md", "plan.md"]
    assert [m["tool_call_id"] for m in result.messages if m.get("role") == "tool"] == ["doc-1", "doc-2"]
    # The calls' database work queues as one caller of the process gate.
    assert groups[0] is not None and groups[0] is groups[1]
    assert database_admission_group.get() is None


async def test_loop_orders_same_target_and_stops_its_dependents_after_an_error(monkeypatch, tmp_path):
    batch = [
        _call("first", "write_file", path="plan.md", content="v1"),
        _call("other", "write_file", path="budget.md", content="b"),
        _call("same", "edit_file", path="plan.md", old_string="v1", new_string="v2"),
    ]
    other_started = asyncio.Event()
    executed: list[str] = []

    async def execute(name, arguments):
        path = arguments["path"]
        executed.append(f"{name}:{path}")
        if path == "budget.md":
            other_started.set()
            return json.dumps({"ok": True})
        # The independent call overlaps the failing one.
        await asyncio.wait_for(other_started.wait(), timeout=5)
        return json.dumps({"status": "failed", "error": "write_failed", "message": "disk full"})

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", _model(batch))
    result = await loop.agentic_loop(
        system_prompt="system", user_message="edit", tools=[_tool("write_file"), _tool("edit_file")],
        tool_executor=execute, tool_batch_planner=_planner(str(tmp_path)), max_rounds=3,
    )

    # The edit of the same file never starts after the write of it failed.
    assert sorted(executed) == ["write_file:budget.md", "write_file:plan.md"]
    assistant = next(m for m in result.messages if m.get("role") == "assistant" and m.get("tool_calls"))
    assert [call["id"] for call in assistant["tool_calls"]] == ["first", "other"]
    assert [m["tool_call_id"] for m in result.messages if m.get("role") == "tool"] == ["first", "other"]


@pytest.mark.parametrize("reason", ["human-gate", "intent-barrier"])
async def test_loop_keeps_round_serial_when_a_call_is_not_clear(monkeypatch, tmp_path, reason):
    batch = [
        _call("doc-1", "write_file", path="plan.md", content="plan"),
        _call("doc-2", "write_file", path="budget.md", content="budget"),
        _call("doc-3", "write_file", path="notes.md", content="notes"),
    ]
    active = 0
    peak = 0
    order: list[str] = []

    async def execute(_name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(arguments["path"])
        for _ in range(3):
            await asyncio.sleep(0)
        active -= 1
        return json.dumps({"ok": True})

    planner = _planner(str(tmp_path), gated={"doc-3"} if reason == "human-gate" else set())

    async def run():
        return await loop.agentic_loop(
            system_prompt="system", user_message="write", tools=[_tool("write_file")],
            tool_executor=execute, tool_batch_planner=planner, max_rounds=3,
        )

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", _model(batch))
    if reason == "intent-barrier":
        with runtime_tool_effect_intent_scope(lambda *_args, **_kwargs: None):
            await run()
        assert planner.calls == []
    else:
        await run()
        assert planner.calls == [["doc-1", "doc-2", "doc-3"]]
    assert peak == 1
    assert order == ["plan.md", "budget.md", "notes.md"]
