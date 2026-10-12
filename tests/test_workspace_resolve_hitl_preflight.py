"""Workspace ordinary replies have one explicit, pre-I/O tool contract."""

from __future__ import annotations

import copy
import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.prompt_guidance import runtime_workspace_agent_mode_guidance
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
from packages.core.ai.runtime.tool_execution import _runtime_workspace_action_preflight_error
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
from packages.core.ai.tools.workspace_agent_tools import (
    WORKSPACE_AGENT_SCHEMA,
    WORKSPACE_RESOLVE_HITL_SCHEMA,
)


SCHEMAS = {
    "manor": MANOR_SCHEMA,
    "workspace_agent": WORKSPACE_AGENT_SCHEMA,
    "workspace_resolve_hitl": WORKSPACE_RESOLVE_HITL_SCHEMA,
}


def _arguments(tool_name, params):
    if tool_name == "manor":
        return {"action": "workspace", "params": {"action": "resolve_hitl", "params": params}}
    if tool_name == "workspace_agent":
        return {"action": "resolve_hitl", "params": params}
    return params


@pytest.mark.unit
def test_schema_and_workspace_prompt_name_response_as_ordinary_reply_field():
    properties = WORKSPACE_RESOLVE_HITL_SCHEMA["function"]["parameters"]["properties"]
    assert properties["action"]["enum"] == ["respond", "approve", "reject"]
    assert properties["response"]["type"] == "string"
    assert "Ordinary reply text belongs in response" in properties["note"]["description"]
    for schema in (MANOR_SCHEMA, WORKSPACE_AGENT_SCHEMA):
        description = schema["function"]["parameters"]["properties"]["params"]["description"]
        assert "resolve_hitl" in description and "response" in description
    envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
        surface=ChatSurface.WORKSPACE_CHAT,
        entity_id="entity", user_id="user", workspace_id="workspace",
    ))
    guidance = runtime_workspace_agent_mode_guidance(envelope=envelope, workspace_id="workspace")
    assert "action='respond'" in guidance
    assert "put the user's reply in `response`" in guidance
    assert "An ordinary reply never grants approval" in guidance


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", SCHEMAS)
@pytest.mark.parametrize("params", [
    {"action": "respond", "response": "Use the original audio."},
    {"action": "respond", "payload": {"fields": {"include_music": False}}},
    {"action": "respond", "payload": {"answers": {"quantity": 0}}},
    {"action": "respond", "payload": {"attachments": [{"id": "document-1"}]}},
    {"action": "respond", "payload": {"attachments": [{"document_id": "document-1"}]}},
    {"action": "approve", "note": "Proceed with the reviewed change."},
    {"action": "reject", "note": "Please revise the draft."},
])
def test_valid_reply_payloads_pass_without_mutation(tool_name, params):
    arguments = _arguments(tool_name, {"message_id": "message-1", **params})
    original = copy.deepcopy(arguments)
    assert _runtime_workspace_action_preflight_error(tool_name, arguments) is None
    assert arguments == original


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", SCHEMAS)
@pytest.mark.parametrize("params", [
    {"response": "Missing action"},
    {"action": "maybe", "response": "Unsupported action"},
    {"action": "respond"},
    {"action": "respond", "response": " \n "},
    {"action": "respond", "note": "Reply in the wrong field"},
    {"action": "respond", "response": ["wrong type"]},
    {"action": "respond", "payload": {"fields": {"answer": " "}}},
    {"action": "respond", "payload": {"answers": {"answer": None}}},
    {"action": "respond", "payload": {"attachments": []}},
    {"action": "respond", "payload": {"attachments": [{"id": " "}]}},
    {"action": "respond", "payload": {"attachments": [{"name": "No document ID"}]}},
    {"action": "respond", "payload": {"attachments": ["document-1"]}},
    {"action": "respond", "payload": {"attachments": "document-1"}},
    {"action": "respond", "payload": {"answers": "wrong type"}},
    {"action": "respond", "payload": "wrong type"},
])
async def test_invalid_reply_never_reaches_approval_intent_or_handler(monkeypatch, tool_name, params):
    events = []

    async def handler(**kwargs):
        events.append("handler")
        return "must not run"

    async def guard(*args, **kwargs):
        events.append("approval")
        raise AssertionError("Invalid input reached approval")

    async def intent(*args, **kwargs):
        events.append("intent")

    pool = ToolPool()
    pool.register(tool_name, SCHEMAS[tool_name], handler)
    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    with runtime_tool_effect_intent_scope(intent):
        result = await pool.execute(
            tool_name, _arguments(tool_name, {"message_id": "message-1", **params}),
            entity_id="entity", user_id="user", workspace_id="workspace",
            conversation_id="conversation",
        )
    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["error"] == "tool_input_validation_failed"
    assert events == []


@pytest.mark.unit
@pytest.mark.parametrize("tool_name", SCHEMAS)
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("invalid_params", [
    {"response": "Use the original audio."},
    {"action": "submit", "response": "Use the original audio."},
    {"action": "respond", "response": " "},
])
async def test_reply_preflight_corrects_in_same_loop(monkeypatch, tool_name, entry, invalid_params):
    invalid = {
        "id": "invalid", "name": tool_name,
        "arguments": _arguments(tool_name, {"message_id": "message-1", **invalid_params}),
    }
    corrected = {
        "id": "corrected", "name": tool_name,
        "arguments": _arguments(tool_name, {
            "message_id": "message-1", "action": "respond", "response": "Use the original audio.",
        }),
    }
    events, handled = [], []
    model_calls = 0

    async def handler(**arguments):
        events.append("handler")
        handled.append(arguments)
        return json.dumps({"resolved": True, "message_id": "message-1", "action": "respond"})

    async def guard(self, request):
        events.append("approval")
        return RuntimeApprovalDecision.allow(classify_runtime_tool(
            request.tool_name, request.arguments, entity_id=request.entity_id,
            workspace_id=request.workspace_id, task_id=request.task_id,
        ), request)

    async def intent(*args, **kwargs):
        events.append("intent")

    pool = ToolPool()
    pool.register(tool_name, SCHEMAS[tool_name], handler)

    async def execute(name, arguments):
        return await pool.execute(
            name, arguments, entity_id="entity", user_id="user",
            workspace_id="workspace", conversation_id="conversation",
        )

    async def model(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", [invalid], {}
        if not handled:
            assert events == []
            assert any(
                message.get("role") == "tool"
                and "tool_input_validation_failed" in str(message.get("content"))
                for message in messages
            )
            return "", [corrected], {}
        return "Your reply was submitted.", [], {}

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    extra = {}
    if entry == "forced":
        extra["forced_tool_calls"] = [invalid]
    elif entry == "resume":
        extra["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [{
                "id": invalid["id"], "type": "function", "function": {
                    "name": tool_name, "arguments": json.dumps(invalid["arguments"]),
                },
            }]}],
            usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=invalid, remaining_tool_calls=[],
        ).to_dict()
    with runtime_tool_effect_intent_scope(intent):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="Use the original audio.",
            tools=[SCHEMAS[tool_name]], tool_executor=execute, max_rounds=4, **extra,
        )
    assert result.error is None
    assert result.content == "Your reply was submitted."
    assert len(handled) == 1
    assert events.count("approval") == 1
    assert events.count("handler") == 1
    assert events.count("intent") == 1
