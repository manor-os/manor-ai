"""Journal observations resume the real loop without replay or reclassification."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import workspace_delegation_actions as actions
from packages.core.ai.runtime.nested_agent import (
    runtime_checkpoint_leaf_call_id, runtime_nested_checkpoint_scope,
    runtime_nested_skill_scope, runtime_pending_checkpoint_leaf,
    runtime_scoped_tool_call_id,
)
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_checkpoint_scope
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.service import _recover_internal_file_effects
from packages.core.workers.submit_result import SUBMIT_RESULT_TERMINAL_POLICY
from test_nested_skill_durability import checkpoint
from test_runtime_file_effect_recovery import ARGUMENTS, SCOPE, effect_ref, journal_row, session


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _pending(scope, nested):
    tree = checkpoint("patch_file", "call-1", ARGUMENTS)
    if nested:
        with (
            runtime_nested_checkpoint_scope(AsyncMock()),
            runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}),
            runtime_nested_skill_scope("skill-1") as frame,
        ):
            tree = frame.wrap({"kind": "pending", "checkpoint": tree})
    call_id = runtime_checkpoint_leaf_call_id(tree)
    ref = effect_ref(
        tool_call_id=call_id, owner_plan_id=scope["plan_id"], owner_step_id=scope["step_id"],
        effect_generation=str(scope["effect_generation"]), intent_state="started",
    )
    ref.update(kind="tool_effect", effect="pending")
    row = journal_row(entity_id=scope["entity_id"])
    row.owner.update({**scope, "file_effect": copy.deepcopy(ref), "tool_call_id": call_id})
    row.effect_id = ToolEffectEvidenceFactory.effect_id(ref)
    return tree, ref, row


def _rescan(messages, evidence, scope):
    return actions._delegation_receipts(
        messages, entity_id=scope["entity_id"], workspace_id=scope["workspace_id"],
        task_id=scope["task_id"], authoritative_intents=evidence,
        owner_plan_id=scope["plan_id"], owner_step_id=scope["step_id"],
        effect_generation=int(scope["effect_generation"]),
    )


async def _resume(monkeypatch, tree, evidence, scope, *, persist_round=None, persist_nested=None):
    """Only model completion is mocked; resume, nested frames and tool stops are real."""
    executed, rounds, observations = [], [], []
    terminal = [ref for ref in evidence if ref.get("kind") == "tool_effect"]
    assert len(terminal) == 1 and terminal[0]["effect"] == "action"
    original = copy.deepcopy(terminal[0])
    stored = runtime_pending_checkpoint_leaf(tree)["pending_tool_result"]["content"]
    payload = json.loads(stored)
    assert payload["kind"] == "runtime_file_reconciliation"
    assert payload["status"] == "completed" and payload["operation_status"] == "committed"
    assert payload["original_tool_result_available"] is False
    assert terminal[0]["fields"]["result_sha256"] == hashlib.sha256(stored.encode()).hexdigest()
    assert terminal[0]["fields"]["result_canonical_sha256"] == actions._tool_result_canonical_sha256(stored)

    def classify(name, *_args, **_kwargs):
        assert name != "patch_file", "journal receipt must bypass native file reclassification"
        # The only new executions below are host control calls.
        assert name in {"invoke_skill", "submit_result"}
        return None

    monkeypatch.setattr(actions, "_delegation_effect_intent", classify)
    # Lossless JSON formatting changes still bind to the host-recorded receipt;
    # native patch details and model claims are never used to settle it again.
    call_id = terminal[0]["fields"]["tool_call_id"]
    normalized = [
        {"role": "assistant", "tool_calls": [{"id": call_id, "function": {
            "name": "patch_file", "arguments": json.dumps(ARGUMENTS),
        }}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, separators=(",", ":"))},
    ]
    assert _rescan(normalized, evidence, scope) == ([], [], [])

    async def model(messages, tools, **_kwargs):
        results = [item for item in messages if item.get("role") == "tool"]
        assert results
        if tools[0]["function"]["name"] == "patch_file":
            assert json.loads(results[-1]["content"]) == payload
            observations.append(copy.deepcopy(messages))
            return "Committed historical report observed.", [], {}
        if results[-1]["tool_call_id"] == "call-1":
            assert json.loads(results[-1]["content"]) == payload
            observations.append(copy.deepcopy(messages))
        return "", [{"id": "submit", "name": "submit_result", "arguments": {
            "result": {"document_id": payload["files"][0]["document_id"]},
        }}], {}

    async def on_round(round_number, messages, usage, tool_calls_made, _state=None):
        rounds.append(round_number)
        assert _rescan(messages, evidence, scope) == ([], [], [])
        if persist_round:
            await persist_round(round_number, messages, usage, tool_calls_made)

    async def on_nested(root, messages):
        # Nested effect IDs are qualified by their host frame, not model-local
        # IDs. Verify exactly the same stored observation against its receipt.
        scoped = copy.deepcopy(messages)
        for message in scoped:
            if message.get("role") == "tool":
                message["tool_call_id"] = runtime_scoped_tool_call_id(message["tool_call_id"])
            for call in message.get("tool_calls") or []:
                call["id"] = runtime_scoped_tool_call_id(call["id"])
        assert _rescan(scoped, evidence, scope) == ([], [], [])
        if persist_nested:
            await persist_nested(root, messages)

    async def execute(name, arguments):
        executed.append(name)
        assert name != "patch_file", "the recovered mutation must never execute again"
        if name == "invoke_skill":
            with runtime_nested_skill_scope("skill-1") as frame:
                child = await loop_module.agentic_loop(
                    system_prompt="child", user_message="update the report", tools=[_tool("patch_file")],
                    tool_executor=execute, max_rounds=4, on_round_end=frame.on_round_end,
                    on_round_end_strict=True, **frame.loop_kwargs(),
                )
                response = {"content": child.content, "stop_reason": child.stop_reason}
                await frame.complete(response, child.messages)
            return json.dumps(response)
        assert name == "submit_result"
        return '{"ok":true,"status":"completed"}'

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    with runtime_nested_checkpoint_scope(on_nested):
        result = await loop_module.agentic_loop(
            system_prompt="parent", user_message="finish the report", tools=[_tool("invoke_skill"), _tool("submit_result")],
            tool_executor=execute, max_rounds=4, resume_checkpoint=tree,
            terminal_tool_result_policy=SUBMIT_RESULT_TERMINAL_POLICY,
            on_round_end=on_round, on_round_end_strict=True,
        )
    assert result.stop_reason == "submit_result"
    assert executed in (["submit_result"], ["invoke_skill", "submit_result"])
    assert observations and len(rounds) >= 2
    assert terminal[0] == original
    return result, original


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
async def test_journal_observation_resumes_shared_loop_without_native_reclassification(monkeypatch, nested):
    tree, pending, row = _pending(SCOPE, nested)
    db = session([row])
    db.scalar = AsyncMock(return_value=SCOPE["task_id"])
    step = SimpleNamespace(
        id=SCOPE["step_id"], plan_id=SCOPE["plan_id"], entity_id=SCOPE["entity_id"],
        workspace_id=SCOPE["workspace_id"], evidence_refs=[pending], runtime_checkpoint=tree,
    )
    lease = SimpleNamespace(evidence_refs=[pending])
    assert await _recover_internal_file_effects(db, lease, step) == (True, [])
    await _resume(monkeypatch, step.runtime_checkpoint, step.evidence_refs, SCOPE)
    assert ToolEffectEvidenceFactory.identity(step.evidence_refs[0]) == ToolEffectEvidenceFactory.identity(pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
async def test_db_journal_loop_next_worker_round_preserves_settled_identity(db_session, monkeypatch, nested):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.runtime_file_commit import RuntimeFileCommit
    from packages.core.models.worker import WorkLease
    from packages.core.workers import internal
    from test_robust_worker2_partial_transcript import _seed_lease

    snapshot = await _seed_lease(db_session, execution_claim_id=generate_ulid())
    snapshot.update(workspace_id=generate_ulid(), task_id=generate_ulid(), action_effect_generation=3)
    scope = {key: snapshot[key] for key in SCOPE if key != "effect_generation"}
    scope["effect_generation"] = "3"
    tree, pending, row = _pending(scope, nested)
    row.id, row.transaction_id = generate_ulid(), generate_ulid()
    db_session.add(RuntimeFileCommit(**vars(row)))
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    plan = await db_session.get(ExecutionPlan, snapshot["plan_id"])
    plan.task_id = scope["task_id"]
    step.workspace_id = scope["workspace_id"]
    step.runtime_checkpoint, step.evidence_refs, lease.evidence_refs = tree, [pending], [pending]
    await db_session.commit()
    assert await _recover_internal_file_effects(db_session, lease, step) == (True, [])
    tree, evidence = copy.deepcopy(step.runtime_checkpoint), copy.deepcopy(step.evidence_refs)
    await db_session.commit()
    monkeypatch.setattr(internal, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def persist_round(rounds, messages, usage, tools):
        await internal._persist_worker_round_checkpoint(
            snapshot, rounds=rounds, messages=messages, usage=usage, tool_calls_made=tools,
        )

    async def persist_nested(root, messages):
        await internal._persist_worker_nested_checkpoint(snapshot, root, messages)

    result, original = await _resume(
        monkeypatch, tree, evidence, scope, persist_round=persist_round, persist_nested=persist_nested,
    )
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    for owner in (step, lease):
        effects = [ref for ref in owner.evidence_refs if ref.get("kind") == "tool_effect"]
        assert effects == [original]
    assert step.runtime_checkpoint["messages"] == [message for message in result.messages if message.get("role") != "system"]
    assert internal.is_worker_transcript_checkpoint(step.runtime_checkpoint)
