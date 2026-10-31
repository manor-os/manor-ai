"""New invocations cannot borrow the identity or journal proof of old calls."""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_result_scope
from test_nested_skill_durability import checkpoint
from test_runtime_file_effect_recovery import ARGUMENTS, journal_row, read, session


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
