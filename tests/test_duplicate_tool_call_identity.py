"""New invocations cannot borrow the identity or journal proof of old calls."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.tool_effect_intents import (
    RuntimeToolEffectPersistenceError, runtime_tool_effect_result_scope,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.workers import internal
from test_nested_skill_durability import checkpoint
from test_nested_skill_output_recovery import _classify
from test_runtime_file_effect_recovery import ARGUMENTS, SCOPE, effect_ref, journal_row, read, session


def _call(call_id):
    return {"id": call_id, "name": "patch_file", "arguments": ARGUMENTS}


def _tool():
    return {"type": "function", "function": {"name": "patch_file", "parameters": {"type": "object"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["history", "batch"])
async def test_new_duplicate_batch_is_correctable_and_executes_no_tools(monkeypatch, collision):
    executed, model_calls = [], 0

    async def model(messages, _tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if collision == "history" and model_calls == 1:
            return "", [_call("same")], {}
        if model_calls == (2 if collision == "history" else 1):
            return "", [_call("same"), _call("same" if collision == "batch" else "must-not-run")], {}
        if model_calls == (3 if collision == "history" else 2):
            diagnostic = json.loads(messages[-1]["content"])
            assert diagnostic["contract"] == "tool_error"
            assert diagnostic["error"] == "duplicate_tool_call_id"
            assert diagnostic["details"]["tool_call_ids"] == ["same"]
            assert executed == (["same"] if collision == "history" else [])
            return "", [_call("corrected")], {}
        return "done", [], {}

    async def execute(_name, arguments):
        executed.append(arguments["_runtime_tool_call_id_from_context"])
        return '{"status":"completed","patched":true}'

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    with runtime_tool_effect_result_scope(AsyncMock()):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="write", tools=[_tool()], tool_executor=execute, max_rounds=6,
        )
    assert result.content == "done" and result.error is None
    assert executed == (["same", "corrected"] if collision == "history" else ["corrected"])
    ids = [call["id"] for message in result.messages for call in message.get("tool_calls") or []]
    assert len(ids) == len(set(ids))
    assert "must-not-run" not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_result", [False, True])
async def test_same_pending_call_resume_keeps_its_identity(monkeypatch, stored_result):
    pending = checkpoint("patch_file", "same", ARGUMENTS)
    if stored_result:
        pending["pending_tool_result"] = {"id": "same", "content": '{"status":"completed","patched":true}'}
    execute = AsyncMock(return_value='{"status":"completed","patched":true}')
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", AsyncMock(return_value=("done", [], {})))
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="write", tools=[_tool()], tool_executor=execute,
        max_rounds=4, resume_checkpoint=pending,
    )
    assert result.content == "done"
    assert execute.await_count == (0 if stored_result else 1)
    assert "duplicate_tool_call_id" not in json.dumps(result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_kind", ["returned", "settlement", "different_pending", "same_pending"])
async def test_worker_identity_guard_precedes_checkpoint_and_intent_mutation(monkeypatch, prior_kind):
    pending = checkpoint("patch_file", "call-1", ARGUMENTS)
    prior = effect_ref(intent_state="returned" if prior_kind == "returned" else "started")
    prior.update(kind="tool_effect", effect="action" if prior_kind == "returned" else "pending")
    if prior_kind == "settlement":
        prior.update(kind="tool_intent_settlement", effect="runtime_confirmed_not_applied")
    current = copy.deepcopy(pending)
    if prior_kind == "different_pending":
        current["messages"].insert(0, {"role": "user", "content": "previous invocation"})
    step = SimpleNamespace(id="step", plan_id="plan", evidence_refs=[prior], runtime_checkpoint=current)
    lease = SimpleNamespace(evidence_refs=[copy.deepcopy(prior)])
    before = copy.deepcopy((step.evidence_refs, step.runtime_checkpoint, lease.evidence_refs))
    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def database():
        yield db

    monkeypatch.setattr(internal, "async_session", database)
    monkeypatch.setattr(internal, "_lock_live_worker_execution", AsyncMock(return_value=(lease, step)))
    snapshot = {**SCOPE, "action_effect_generation": 3}
    if prior_kind == "same_pending":
        await internal._persist_worker_tool_effect_intent(snapshot, "patch_file", ARGUMENTS, "call-1", _classify("patch_file"), pending)
        db.commit.assert_awaited_once()
    else:
        with pytest.raises(RuntimeToolEffectPersistenceError, match="already recorded"):
            await internal._persist_worker_tool_effect_intent(snapshot, "patch_file", ARGUMENTS, "call-1", _classify("patch_file"), pending)
        db.commit.assert_not_awaited()
        assert (step.evidence_refs, step.runtime_checkpoint, lease.evidence_refs) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["duplicate_declaration", "prior_result", "unique_pending"])
async def test_old_journal_cannot_prove_reused_pending_transcript_identity(history):
    pending = checkpoint("patch_file", "call-1", ARGUMENTS)
    if history == "duplicate_declaration":
        pending["messages"].insert(1, copy.deepcopy(pending["messages"][-1]))
    elif history == "prior_result":
        pending["messages"].insert(1, {"role": "tool", "tool_call_id": "call-1", "content": '{"status":"completed"}'})
    db = session([journal_row()])
    result = await read(db, runtime_checkpoint=pending)
    if history == "unique_pending":
        assert result.operation_status == "committed"
    else:
        assert result is None
        db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_old_committed_row_cannot_authorize_second_identical_invocation(db_session, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.runtime_file_commit import RuntimeFileCommit
    from packages.core.models.worker import WorkLease
    from test_robust_worker2_partial_transcript import _seed_lease
    from test_runtime_file_effect_loop_recovery import _pending

    snapshot = await _seed_lease(db_session, execution_claim_id=generate_ulid())
    snapshot.update(workspace_id=generate_ulid(), task_id=generate_ulid(), action_effect_generation=3)
    scope = {key: snapshot[key] for key in SCOPE if key != "effect_generation"}
    scope["effect_generation"] = "3"
    pending, prior, row = _pending(scope, False)
    row.id, row.transaction_id = generate_ulid(), generate_ulid()
    db_session.add(RuntimeFileCommit(**vars(row)))
    prior = ToolEffectEvidenceFactory.bind_effect_id({**prior, "effect": "action", "fields": {
        **prior["fields"], "intent_state": "returned", "result_sha256": "a" * 64,
    }})
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    step.evidence_refs = lease.evidence_refs = [prior]
    # Simulate compacted model history: only the durable exact receipt still
    # knows the first invocation. A second handler must not even prepare a row.
    step.runtime_checkpoint = {"saved": "first invocation"}
    await db_session.commit()
    monkeypatch.setattr(internal, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    reached_handler = False
    with pytest.raises(RuntimeToolEffectPersistenceError, match="already recorded"):
        await internal._persist_worker_tool_effect_intent(
            snapshot, "patch_file", ARGUMENTS, "call-1", _classify("patch_file"), pending,
        )
        reached_handler = True
    assert not reached_handler
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    assert step.runtime_checkpoint == {"saved": "first invocation"}
    assert step.evidence_refs == [prior]
    pending["messages"].insert(1, copy.deepcopy(pending["messages"][-1]))
    assert await read(
        db_session, **{key: value for key, value in scope.items() if key != "effect_generation"},
        effect_ref=prior, runtime_checkpoint=pending,
    ) is None
