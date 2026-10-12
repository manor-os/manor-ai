"""Discovered tools are called through call_tool, so the tool list never changes.

Before this, every search_tools discovery appended a schema to the request's
tool list. Tool definitions sit at the front of the cached prompt prefix, so
each discovery (and each turn that restored a different set) invalidated the
cache for the whole conversation.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_turn_context_message
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalDecision,
    RuntimeApprovalMiddleware,
)
from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
from packages.core.ai.runtime.tool_gateway import (
    CALL_TOOL_NAME,
    call_tool_schema,
    runtime_gateway_tools_section,
    runtime_tool_call_target,
    runtime_unwrap_gateway_call,
)
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

SLACK = {"type": "function", "function": {
    "name": "slack_send_message", "description": "Send a Slack message.",
    "parameters": {"type": "object", "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                   "required": ["channel", "text"]},
}}
SEARCH = {"type": "function", "function": {"name": "search_tools", "parameters": {"type": "object"}}}
READ = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}


def test_unwrap_gives_the_real_call_under_the_same_id():
    call = {"id": "c1", "name": CALL_TOOL_NAME,
            "arguments": {"name": "slack_send_message", "arguments": {"channel": "#ops", "text": "hi"}}}
    unwrapped, reason = runtime_unwrap_gateway_call(call, reachable={"slack_send_message"})
    assert reason is None
    assert unwrapped == {"id": "c1", "name": "slack_send_message", "arguments": {"channel": "#ops", "text": "hi"}}
    # String-encoded inner arguments are accepted too.
    string_args = {**call, "arguments": {"name": "slack_send_message", "arguments": '{"channel": "#ops", "text": "hi"}'}}
    assert runtime_unwrap_gateway_call(string_args, reachable={"slack_send_message"})[0]["name"] == "slack_send_message"


@pytest.mark.parametrize("arguments,reachable", [
    ({"name": "slack_send_message", "arguments": {}}, set()),          # not loaded / narrowed out
    ({"name": CALL_TOOL_NAME, "arguments": {}}, {CALL_TOOL_NAME}),       # recursion
    ({"arguments": {}}, {"slack_send_message"}),                         # no name
    ({"name": "slack_send_message", "arguments": "not json"}, {"slack_send_message"}),
])
def test_calls_that_cannot_be_unwrapped_stay_call_tool_with_a_reason(arguments, reachable):
    call = {"id": "c1", "name": CALL_TOOL_NAME, "arguments": arguments}
    kept, reason = runtime_unwrap_gateway_call(call, reachable=reachable)
    assert reason and kept["name"] == CALL_TOOL_NAME and kept["arguments"]["rejected_reason"] == reason


def test_history_readers_see_the_real_tool():
    declared = json.dumps({"name": "slack_send_message", "arguments": {"channel": "#ops"}})
    assert runtime_tool_call_target(CALL_TOOL_NAME, declared) == ("slack_send_message", {"channel": "#ops"})
    assert runtime_tool_call_target("read_file", {"path": "a"}) == ("read_file", {"path": "a"})

    from packages.core.ai.runtime.workspace_delegation_actions import _tool_call_arguments, _tool_call_name

    call = {"id": "c1", "type": "function", "function": {"name": CALL_TOOL_NAME, "arguments": declared}}
    assert _tool_call_name(call) == "slack_send_message"
    assert _tool_call_arguments(call) == {"channel": "#ops"}


def test_a_gateway_call_is_governed_as_the_tool_it_calls():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    assert RuntimeCompositeToolCallFactory.create(
        CALL_TOOL_NAME, {"name": "bash", "arguments": {"command": "rm -rf /tmp/x"}},
    ).tool_name == "bash"
    gateway = classify_runtime_tool(CALL_TOOL_NAME, {"name": "bash", "arguments": {"command": "ls"}})
    direct = classify_runtime_tool("bash", {"command": "ls"})
    assert gateway.effect == direct.effect
    # A rejected call executes nothing and is not governed as its target.
    rejected = {"name": "bash", "arguments": {"command": "ls"}, "rejected_reason": "not loaded"}
    assert RuntimeCompositeToolCallFactory.create(CALL_TOOL_NAME, rejected).tool_name == CALL_TOOL_NAME


def test_read_only_fallback_never_offers_call_tool():
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    names = [t["function"]["name"] for t in loop._read_only_tools_for_alternate_path([call_tool_schema(), READ])]
    assert CALL_TOOL_NAME not in names and "read_file" in names


def test_registered_handler_reports_rejections_and_delegates_valid_calls(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    handler = pool._tools[CALL_TOOL_NAME]["handler"]
    execute = AsyncMock(return_value="sent")
    monkeypatch.setattr(pool, "execute", execute)

    rejected = asyncio.run(handler(entity_id="e", user_id="u", name="slack_send_message", arguments={},
                                   rejected_reason="'slack_send_message' is not loaded for this request."))
    payload = json.loads(rejected)
    assert payload["error"] == "gateway_call_rejected" and payload["details"]["action_executed"] is False
    execute.assert_not_awaited()

    assert asyncio.run(handler(entity_id="e", user_id="u", name="slack_send_message",
                               arguments={"channel": "#ops", "text": "hi"})) == "sent"
    assert execute.await_args.args == ("slack_send_message", {"channel": "#ops", "text": "hi"})


@pytest.mark.asyncio
async def test_rejected_gateway_read_stops_before_approval_and_pool_handler(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    execute = AsyncMock(return_value="unexpected")
    monkeypatch.setattr(pool, "execute", execute)

    async def approval_must_not_run(*_args, **_kwargs):
        raise AssertionError("A rejected gateway call must not request approval")

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", approval_must_not_run)
    result = await runtime_execute_registered_tool(
        tool_name=CALL_TOOL_NAME,
        arguments={
            "name": "mcp__chrome__read_page",
            "arguments": {"tabId": 123},
            "rejected_reason": "'mcp__chrome__read_page' is not loaded for this request.",
        },
        handler_resolver=lambda name: pool._tools[name]["handler"],
        entity_id="entity",
        user_id="user",
    )

    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload == {
        "contract": "tool_error",
        "status": "error",
        "tool": CALL_TOOL_NAME,
        "error": "gateway_call_rejected",
        "message": "'mcp__chrome__read_page' is not loaded for this request.",
        "details": {"action_executed": False, "discovery_tool": "search_tools"},
    }
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ("e9", "f80:e21"))
async def test_unsupported_chrome_computer_action_stops_before_approval_and_handler(monkeypatch, ref):
    handler = AsyncMock(return_value="unexpected")

    async def approval_must_not_run(*_args, **_kwargs):
        raise AssertionError("An unsupported Chrome computer action must not request approval")

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", approval_must_not_run)
    result = await runtime_execute_registered_tool(
        tool_name="mcp__chrome__computer",
        arguments={"tabId": 123, "action": "scroll_into_view", "ref": ref},
        handler_resolver=lambda _name: handler,
        entity_id="entity",
        user_id="user",
        active_user_message="滚动到表单字段",
    )

    payload = json.loads(result)
    assert payload["reason"] == "chrome_unsupported_computer_action"
    assert payload["action"] == "scroll_into_view"
    assert "scroll_to" in payload["supported_actions"]
    handler.assert_not_awaited()

    from packages.core.ai.runtime.chrome_routing import runtime_blocked_chrome_action_shortcut

    assert runtime_blocked_chrome_action_shortcut(
        tool_name="mcp__chrome__computer",
        arguments={"tabId": 123, "action": "scroll_to", "ref": ref},
        active_user_message="滚动到表单字段",
    ) is None
    for tool_name, arguments in (
        ("mcp__chrome__click_element", {"tabId": 123, "ref": ref}),
        ("mcp__chrome__fill_or_select", {"tabId": 123, "ref": ref, "value": "test"}),
    ):
        assert runtime_blocked_chrome_action_shortcut(
            tool_name=tool_name,
            arguments=arguments,
            active_user_message="填写表单字段",
        ) is None
    malformed_ref_block = runtime_blocked_chrome_action_shortcut(
        tool_name="mcp__chrome__computer",
        arguments={"tabId": 123, "action": "scroll_to", "ref": "f80:e21:bad"},
        active_user_message="滚动到表单字段",
    )
    assert json.loads(malformed_ref_block)["reason"] == "chrome_fallback_reason_required"
    malformed_node_ref_block = runtime_blocked_chrome_action_shortcut(
        tool_name="mcp__chrome__click_element",
        arguments={"tabId": 123, "ref": "f80:e21:bad"},
        active_user_message="点击表单字段",
    )
    assert json.loads(malformed_node_ref_block)["reason"] == "chrome_invalid_read_page_ref"


@pytest.mark.asyncio
async def test_valid_gateway_mutation_reaches_canonical_approval_before_pool_execution(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    execute = AsyncMock(return_value="unexpected")
    monkeypatch.setattr(pool, "execute", execute)
    approvals = []
    arguments = {"name": "bash", "arguments": {"command": "rm -rf /tmp/gateway-qa"}}

    async def require_approval(self, request):
        classification = self.classify_request(request)
        approvals.append(classification)
        return RuntimeApprovalDecision.block(
            request.tool_name,
            json.dumps({"error": "approval_required"}),
            classification,
            request,
        )

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", require_approval)
    result = await runtime_execute_registered_tool(
        tool_name=CALL_TOOL_NAME,
        arguments=arguments,
        handler_resolver=lambda name: pool._tools[name]["handler"],
        entity_id="entity",
        user_id="user",
    )

    assert json.loads(result)["error"] == "approval_required"
    assert len(approvals) == 1
    direct = classify_runtime_tool("bash", arguments["arguments"])
    assert approvals[0].effect == direct.effect
    assert approvals[0].action == direct.action
    execute.assert_not_awaited()


def test_gateway_section_renders_definitions_and_lists_overflow_by_name():
    section = runtime_gateway_tools_section({"slack_send_message": SLACK})
    assert "Tools callable through call_tool" in section and '"name":"slack_send_message"' in section
    huge = {f"tool_{i}": {"type": "function", "function": {
        "name": f"tool_{i}", "description": "x" * 20_000, "parameters": {"type": "object"}}} for i in range(5)}
    section = runtime_gateway_tools_section(huge)
    assert '"name":"tool_4"' in section  # newest keeps its definition
    assert "Also loaded" in section and "tool_0" in section.split("Also loaded")[1]


def _run_loop(monkeypatch, *, script, gateway_tool_schemas=None, tools=None):
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    sent: list[tuple[list[dict], list[dict]]] = []
    executed: list[tuple[str, dict]] = []
    started: list[str] = []

    async def complete(messages, tools, **kwargs):
        sent.append(([dict(m) for m in messages], [t["function"]["name"] for t in tools]))
        # Extra rounds (e.g. tool-error repair) get the final answer again.
        return script[min(len(sent), len(script)) - 1]

    async def execute(name, args):
        executed.append((name, {k: v for k, v in args.items() if not k.startswith("_")}))
        if name == "search_tools":
            return json.dumps({"loaded_tools": ["slack_send_message"],
                               "matches": [{"name": "slack_send_message", "schema": SLACK}]})
        return json.dumps({"ok": True, "tool": name})

    def on_tool_start(name, *_args, **_kwargs):
        started.append(name)

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    result = asyncio.run(loop.agentic_loop(
        system_prompt="stable", user_message="tell #ops we shipped",
        tools=tools or [SEARCH, call_tool_schema(), READ], tool_executor=execute, max_rounds=5,
        on_tool_start=on_tool_start, gateway_tool_schemas=gateway_tool_schemas,
    ))
    return result, sent, executed, started


GATEWAY_CALL = {"id": "c2", "name": CALL_TOOL_NAME,
                "arguments": {"name": "slack_send_message", "arguments": {"channel": "#ops", "text": "shipped"}}}


def test_discovered_tools_are_called_through_call_tool_and_the_tool_list_never_changes(monkeypatch):
    result, sent, executed, started = _run_loop(monkeypatch, script=[
        ("", [{"id": "c1", "name": "search_tools", "arguments": {"query": "slack"}}], {"total": 1}),
        ("", [GATEWAY_CALL], {"total": 1}),
        ("Posted.", [], {"total": 1}),
    ])
    assert result.content == "Posted."
    # The same tool list in every round: discovery did not touch the prefix.
    assert len({tuple(tool_names) for _, tool_names in sent}) == 1
    assert "slack_send_message" not in sent[0][1]
    # The definition reached the model through the turn context.
    context = [m for m in sent[1][0] if runtime_is_turn_context_message(m)]
    assert context and '"name":"slack_send_message"' in context[0]["content"]
    # Execution, callbacks and the tool record see the real tool.
    assert executed[-1] == ("slack_send_message", {"channel": "#ops", "text": "shipped"})
    assert started[-1] == "slack_send_message"
    assert result.tool_calls_made[-1] == "slack_send_message"
    # History keeps what the model emitted, answered under the same id.
    declaration = next(m for m in result.messages if m.get("role") == "assistant" and m.get("tool_calls")
                       and m["tool_calls"][0]["id"] == "c2")
    assert declaration["tool_calls"][0]["function"]["name"] == CALL_TOOL_NAME
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c2" for m in result.messages)


def test_tools_restored_from_earlier_turns_are_callable_without_searching_again(monkeypatch):
    _result, sent, executed, _ = _run_loop(monkeypatch, gateway_tool_schemas=[SLACK], script=[
        ("", [GATEWAY_CALL], {"total": 1}),
        ("Posted.", [], {"total": 1}),
    ])
    assert executed == [("slack_send_message", {"channel": "#ops", "text": "shipped"})]
    assert "slack_send_message" not in sent[0][1]
    assert any('"name":"slack_send_message"' in m["content"] for m in sent[0][0] if runtime_is_turn_context_message(m))


def test_a_tool_that_was_never_loaded_is_rejected_without_running(monkeypatch):
    _result, _sent, executed, _ = _run_loop(monkeypatch, script=[
        ("", [GATEWAY_CALL], {"total": 1}),
        ("Could not post.", [], {"total": 1}),
    ])
    assert executed[0][0] == CALL_TOOL_NAME
    assert "not loaded" in executed[0][1]["rejected_reason"]
    assert all(name != "slack_send_message" for name, _ in executed)


def test_without_call_tool_discovery_still_extends_the_tool_list(monkeypatch):
    _result, sent, executed, _ = _run_loop(monkeypatch, tools=[SEARCH, READ], script=[
        ("", [{"id": "c1", "name": "search_tools", "arguments": {"query": "slack"}}], {"total": 1}),
        ("", [{"id": "c2", "name": "slack_send_message", "arguments": {"channel": "#ops", "text": "shipped"}}], {"total": 1}),
        ("Posted.", [], {"total": 1}),
    ])
    assert "slack_send_message" in sent[1][1]
    assert executed[-1][0] == "slack_send_message"


def test_master_surface_loads_call_tool_with_search_tools():
    from packages.core.ai.runtime.tool_visibility import ALWAYS_LOADED, MASTER_ALWAYS_LOADED

    assert {"search_tools", CALL_TOOL_NAME} <= MASTER_ALWAYS_LOADED
    assert {"search_tools", CALL_TOOL_NAME} <= ALWAYS_LOADED


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway", [True, False])
async def test_restored_tools_reach_the_gateway_instead_of_the_tool_list(client, monkeypatch, gateway):
    import packages.core.database as dbmod
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services import runtime_chat_context as module
    from tests.test_conversation_transcript import _register_owner

    user_id, entity_id = await _register_owner(client, f"gateway_restore_{gateway}")
    async with dbmod.async_session() as db:
        conv = Conversation(id=generate_ulid(), entity_id=entity_id, user_id=user_id, channel="web")
        db.add(conv)
        await db.commit()

    base_tools = [SEARCH, READ, *([call_tool_schema()] if gateway else [])]
    calls = []

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=None, tool_profile=None, is_master=True, task_id=None, thread_ref_kind=None,
            thread_ref_id=None, bound_tool_names=None, mcp_allowed_names=set(), extra_context=None,
        )

    async def fake_assemble(_db, *, request, **kwargs):
        calls.append(kwargs)
        ctx = ChatContext(db=_db, entity_id=request.entity_id, user_id=request.user_id,
                          conversation_id=request.conversation_id)
        tools = [*base_tools, *(kwargs.get("extra_tool_schemas") or [])]
        return SimpleNamespace(context=ctx, tool_schemas=tools, prompt="stable",
                               envelope=SimpleNamespace(metadata={}, principal=SimpleNamespace(actor_user_id=user_id)))

    monkeypatch.setattr("packages.core.services.workspace_runtime.resolve_workspace_runtime", fake_resolve_workspace_runtime)
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble)
    monkeypatch.setattr(module, "runtime_auto_skill_forced_tool_calls", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "_recent_chat_capability_hints", AsyncMock(return_value=[{"name": "slack_send_message", "source": "used"}]))
    monkeypatch.setattr(module, "_restore_chat_capability_schemas", AsyncMock(return_value=([SLACK], ("slack_send_message",))))
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_model_for_user", AsyncMock(return_value="openai/gpt-5.6-terra"))
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_llm_metadata_for_user", AsyncMock(return_value=None))

    async with dbmod.async_session() as db:
        _prompt, tools, _history, ctx = await module.resolve_runtime_chat_context(
            db, "post the update to #ops", entity_id=entity_id, user_id=user_id, conversation_id=conv.id,
            runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT, split_turn_context=True,
        )
    names = [t["function"]["name"] for t in tools]
    assert "slack_send_message" in calls[-1]["extra_allowed_tool_names"]
    if gateway:
        assert "slack_send_message" not in names
        assert ctx.gateway_tool_schemas == [SLACK]
    else:
        assert "slack_send_message" in names
        assert ctx.gateway_tool_schemas == []
