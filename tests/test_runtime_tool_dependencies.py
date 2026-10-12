"""Composite authorization contracts independent of any browser or Blueprint."""

from dataclasses import replace
import json

import pytest

from packages.core.ai.runtime import tool_registry
from packages.core.ai.runtime.authorization_receipts import runtime_current_tool_authorization_receipt
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.tool_pool import ToolPool
from tests.test_automated_run_approvals import _running_workflow
from tests.test_runtime_guard_unified_store import _runtime_envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "chat", "cancelled", "blocked"])
async def test_composite_dependencies_authorize_independently_and_restore_parent(
    db_session, client, monkeypatch, mode,
):
    ids, run = await _running_workflow(db_session)
    envelope = replace(_runtime_envelope(ids, name="invoke_skill"), surface=ChatSurface.WORKFLOW_AGENT_STEP)
    if mode == "chat":
        envelope = replace(envelope, surface=ChatSurface.WORKSPACE_CHAT)
    elif mode == "blocked":
        envelope = replace(envelope, blocked_tool_names=("web_search",))
    pool = ToolPool()
    monkeypatch.setattr(tool_registry, "_tool_pool", lambda: pool)
    seen = []
    restored = []

    async def dependency(**kwargs):
        receipt = runtime_current_tool_authorization_receipt()
        seen.append(receipt)
        if mode == "cancelled":
            run.status = "cancelled"
            await db_session.commit()
        return json.dumps({"ok": True})

    async def composite(entity_id, user_id, **kwargs):
        context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id, user_id=user_id)
        parent = runtime_current_tool_authorization_receipt()
        results = []
        for name in ("workspace_search", "web_search"):
            results.append(await tool_registry.runtime_execute_tool_dependency(
                name,
                {"query": "test", "_workflow_run_id_from_context": context.workflow_run_id},
                entity_id=context.entity_id, user_id=context.user_id,
                workspace_id=context.workspace_id, conversation_id=context.conversation_id,
                runtime_envelope=context.runtime_envelope,
            ))
            restored.append(runtime_current_tool_authorization_receipt() is parent)
        return json.dumps(results)

    for name, handler in (("invoke_skill", composite), ("workspace_search", dependency), ("web_search", dependency)):
        pool.register(name, {"type": "function", "function": {
            "name": name, "parameters": {"type": "object"},
        }}, handler)
    results = json.loads(await tool_registry.runtime_execute_tool(
        "invoke_skill", {"skill_id": "test-composite", "_workflow_run_id_from_context": run.id},
        **ids, runtime_envelope=envelope,
    ))
    expected = ["workspace_search"] if mode in {"cancelled", "blocked"} else ["workspace_search", "web_search"]
    assert [receipt.tool_name for receipt in seen] == expected, results
    assert all(receipt.workspace_id == ids["workspace_id"] for receipt in seen)
    assert all(receipt.workflow_run_id == (None if mode == "chat" else run.id) for receipt in seen)
    assert restored == [True, True]
    assert runtime_current_tool_authorization_receipt() is None
    if mode == "cancelled":
        assert "workflow_origin_not_executable" in results[-1]
    elif mode == "blocked":
        assert "blocked_by_runtime_policy" in results[-1]
    assert envelope.allowed_tool_names == ("invoke_skill",)
    child_events = [event["tool_name"] for event in envelope.metadata["runtime_events"] if event["type"] == "tool_start"]
    assert child_events == ["invoke_skill", *expected]
