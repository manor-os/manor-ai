"""Discovery/inventory observations are not proof that an operation is unavailable."""
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def reply(status, operation, call_id):
    return json.dumps({"kind": "answer", "text": "Observed outcome", "outcome": {
        "status": status, "evidence_refs": [call_id], "limitations": ["Only this operation was checked"],
        "claims": [{"operation": operation, "status": status, "evidence_refs": [call_id]}],
    }})


@pytest.mark.asyncio
async def test_manor_search_is_scoped_by_trusted_context_not_model_params(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.ai.tools.manor_tool import _manor_handler

    seen = {}

    @asynccontextmanager
    async def no_db():
        yield None

    async def resolve(db, *, user_id, entity_id, provider_keys):
        seen["user_id"] = user_id
        return frozenset(provider_keys)

    monkeypatch.setattr("packages.core.database.async_session", no_db)
    monkeypatch.setattr("packages.core.services.agent_permission_service.resolve_usable_mcp_providers", resolve)
    forged = {"user_id": "forged", "mode": "provider", "provider_key": "apify"}
    result = json.loads(await _manor_handler(
        entity_id="entity", action="search", query="create scheduled automation and a booking link",
        _user_id_from_context="actor", _allowed_tool_names_from_context={"create_scheduled_job"}, params=forged,
    ))
    assert seen["user_id"] == "actor"
    assert result["mode"] == "manor"  # params cannot switch it to provider routing
    # The run's tool scope holds: Manor MCP tools outside it are neither listed nor loaded.
    assert result["matches"] and not any(match["id"].startswith("manor_mcp:") for match in result["matches"])
    assert result["loaded_tools"] == []
    other = json.loads(await _manor_handler(
        entity_id="entity", action="search", query="查找创建工作区定时任务或自动化",
        _user_id_from_context="actor", params=forged,
    ))
    assert other["error"] == "query_not_english"


@pytest.mark.asyncio
async def test_unsupported_workspace_category_is_not_empty_inventory():
    from packages.core.workspace_chat.context import workspace_search

    db = AsyncMock()
    result = json.loads(await workspace_search(db, "workspace", "entity", category="automations"))
    assert result["contract"] == "tool_error"
    assert result["error"] == "unsupported_search_category"
    assert result["details"]["field"] == "category"
    assert "tasks" in result["details"]["supported_categories"]
    assert result["details"]["capability_availability"] == "not_established"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_cannot_substantiate_creation_blocker():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("manor", "inventory", "No results found.", {
        "action": "workspace", "params": {"action": "search", "params": {"category": "automations"}},
    })
    result = await contract.validate(reply("blocked", "create_scheduled_job", "inventory"))
    assert result.action == FinalResponseAction.REVISE_REPLY
    assert contract.observations["inventory"]["operation"] == "workspace_agent.search"
    assert contract.observations["inventory"]["stage"] == "inventory"
    assert (await contract.validate(reply("partial", "workspace_agent.search", "inventory"))).action == FinalResponseAction.ACCEPT
    assert (await contract.validate(reply("completed", "workspace_agent.search", "inventory"))).action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_failed_creation_proves_only_that_operation_not_other_routes():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("manor", "create", '{"error":"permission_denied"}', {
        "action": "create_scheduled_job", "params": {"name": "Daily"},
    })
    assert (await contract.validate(reply("blocked", "create_scheduled_job", "create"))).action == FinalResponseAction.ACCEPT
    assert (await contract.validate(reply("blocked", "web_search", "create"))).action == FinalResponseAction.REVISE_REPLY


@pytest.mark.asyncio
async def test_successful_read_cannot_be_used_as_blocker():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("web_search", "read", '{"results":[]}')
    assert (await contract.validate(reply("blocked", "web_search", "read"))).action == FinalResponseAction.CONTINUE_EXECUTION
    assert (await contract.validate(reply("partial", "web_search", "read"))).action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_claim_operation_survives_resume_and_cannot_borrow_old_turn():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("create_scheduled_job", "create", '{"ok":true,"created":true,"automation":{"id":"job-1"}}')
    resumed = ChatEvidenceContract()
    resumed.restore_checkpoint_state(contract.checkpoint_state())
    assert (await resumed.validate(reply("completed", "create_scheduled_job", "create"))).action == FinalResponseAction.ACCEPT
    assert (await ChatEvidenceContract().validate(reply("completed", "create_scheduled_job", "create"))).action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
async def test_alias_discovery_loads_schema_and_continues_same_loop(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    rounds, calls = 0, []

    async def execute(name, args, **context):
        calls.append(name)
        if name == "manor":
            return json.dumps({"matches": [{"name": "create_scheduled_job", "available": True}],
                               "loaded_tools": ["create_scheduled_job"],
                               "discovery": {"action_executed": False}})
        # A simulated creation receipt: no scheduling, database or provider I/O.
        assert name == "create_scheduled_job"
        return '{"ok":true,"created":true,"automation":{"id":"job-1"}}'

    async def complete(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        names = {schema["function"]["name"] for schema in tools}
        if rounds == 1:
            assert "create_scheduled_job" not in names
            return "", [{"id": "discover", "name": "manor", "arguments": {"action": "search", "query": "每天执行"}}], {"total": 1}
        if rounds == 2:
            assert "create_scheduled_job" in names
            observation = next(item for item in messages if item.get("tool_call_id") == "discover" and item["role"] == "tool")
            assert json.loads(observation["content"])["discovery"]["action_executed"] is False
            return "", [{"id": "create", "name": "create_scheduled_job", "arguments": {}}], {"total": 1}
        return reply("completed", "create_scheduled_job", "create"), [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("manor", "create_scheduled_job"), None)
    kwargs["tools"] = [schema for schema in kwargs["tools"] if schema["function"]["name"] == "manor"]
    kwargs["dynamic_tool_handlers"] = {}
    kwargs["capability_refresh_tool_names"] = set()
    kwargs["final_response_contract"] = ChatEvidenceContract()
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert calls == ["manor", "create_scheduled_job"]
    assert rounds == 3
