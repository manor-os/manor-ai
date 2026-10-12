"""Child completion must not turn failed/unfinished work into parent learning."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as delegation
from packages.core.ai.runtime.streams import runtime_tool_path_memory_outcome
from packages.core.services.runtime_learning import chat_learning_outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("receipts,missing,stop,error,expected", [
    ([{"error": "tool_input_validation_failed"}] * 5, 0, "completed", None, "failed"),
    ([{"status": "completed"}, {"error": "write_failed"}], 0, "completed", None, "partial"),
    ([{"status": "unknown"}], 0, "completed", None, "partial"),
    ([{"status": "queued"}], 0, "completed", None, "partial"),
    ([], 1, "completed", None, "partial"),
    ([], 0, "max_rounds", None, "partial"),
    ([], 0, "cancelled", "chat_turn_cancelled", "cancelled"),
    ([], 0, "credit_exhausted", None, "blocked"),
    ([{"status": "completed"}], 0, "completed", None, "completed"),
    ([], 0, "completed", None, "completed"),
])
async def test_delegation_classifies_current_receipts_before_returning_to_parent(
    monkeypatch, receipts, missing, stop, error, expected,
):
    from packages.core.ai import context
    from packages.core.ai.runtime import harness
    from packages.core import database
    from packages.core.services import agent_delegation_session as sessions

    @asynccontextmanager
    async def session():
        yield AsyncMock()

    monkeypatch.setattr(database, "async_session", session)
    sub = SimpleNamespace(id="subscription", agent_id="reviewer", service_key="review", custom_prompt=None)
    monkeypatch.setattr(delegation, "_resolve_workspace_service_agent", AsyncMock(
        return_value=(sub, SimpleNamespace(name="Reviewer"), [], None),
    ))
    prior = [{"role": "tool", "tool_call_id": "old", "content": '{"error":"old failure"}'}]
    monkeypatch.setattr(sessions, "claim_delegation_session", AsyncMock(
        return_value=(SimpleNamespace(id="same-session"), "new-run", prior),
    ))
    finish = AsyncMock()
    monkeypatch.setattr(sessions, "finish_delegation_session", finish)
    monkeypatch.setattr(context, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="Review", tools=[], task_id=None,
        tool_profile="workspace_agent", allowed_tool_names=set(), model="test", llm_metadata=None,
    )))

    async def loop(**kwargs):
        assert kwargs["initial_messages"] == prior
        for receipt in receipts:
            kwargs["on_tool_end"]("check", json.dumps(receipt), 1.0, {})
        return SimpleNamespace(
            content="Here is a draft, but check the receipts.",
            messages=[*prior, {"role": "assistant", "content": "Draft"}],
            rounds=2, tool_calls_made=["check"] * (len(receipts) + missing), usage={},
            stop_reason=stop, error=error,
        )

    monkeypatch.setattr(harness, "runtime_execute_chat_agent_loop", loop)
    raw = await delegation.runtime_workspace_delegate_service_action(
        entity_id="entity", workspace_id="workspace", user_id="user", conversation_id="conversation",
        params={"service_key": "review", "prompt": "Check the draft"},
    )
    result = json.loads(raw)
    assert result["status"] == expected
    assert result["session_id"] == "same-session"
    assert sum(result["tool_outcomes"].values()) == len(receipts) + missing
    finish.assert_awaited_once()
    status, _ = chat_learning_outcome(
        stop_reason="completed", has_content=True, error=None, tools=["manor"],
        tool_results=[{"name": "manor", "result": raw}],
    )
    assert (status == "succeeded") is (expected == "completed")
    assert (runtime_tool_path_memory_outcome(raw) == "success") is (expected == "completed")


@pytest.mark.parametrize("status", ["unknown", "pending", "queued", "running", "submitted", "partial", "interrupted"])
def test_unfinished_receipts_never_train_successful_tool_paths(status):
    raw = json.dumps({"status": status})
    assert runtime_tool_path_memory_outcome(raw) is None
    outcome, counts = chat_learning_outcome(
        stop_reason="completed", has_content=True, error=None,
        tools=["manor"], tool_results=[{"name": "manor", "result": raw}],
    )
    assert outcome == "partial"
    assert counts["unknown"] == 1


@pytest.mark.parametrize("key", ["success", "ok"])
def test_explicit_false_receipt_is_not_a_successful_path(key):
    assert runtime_tool_path_memory_outcome(json.dumps({key: False})) == "failure"
