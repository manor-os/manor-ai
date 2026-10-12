from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _calls(invalid_arguments=None):
    return [
        {"id": "before", "name": "web_fetch", "arguments": {"url": "https://example.com"}},
        {"id": "invalid", "name": "generate_file", "arguments": invalid_arguments or {"kind": "document", "prompt": "body"}},
        {"id": "suffix", "name": "write_file", "arguments": {"path": "must-not-run.md", "content": "wrong"}},
    ]


@pytest.mark.unit
def test_manor_workspace_action_preflight_preserves_valid_actions():
    from packages.core.ai.runtime.tool_execution import (
        _runtime_workspace_action_preflight_error,
    )

    assert _runtime_workspace_action_preflight_error(
        "manor", {"action": "workspace", "params": {
            "action": "update_task_runtime", "params": {"task_id": "task"},
        }},
    ) is None
    assert _runtime_workspace_action_preflight_error(
        "manor", {"action": "update_task", "params": {"task_id": "task"}},
    ) is None
    assert _runtime_workspace_action_preflight_error(
        "manor", {"action": "workspace", "params": {"action": "update_task"}},
    ) is not None


@pytest.mark.unit
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("invalid_arguments, error_text", [
    ({"kind": "document", "prompt": "body"}, "kind=document requires complete content"),
    ({"kind": "document", "title": "report.md", "content": "body"}, "Use name (including the extension)"),
    ({"kind": "word_document", "name": "edited.docx", "template": {"path": "uploads/chat/source.pdf", "expected_sha256": "a" * 64}, "operations": [{"operation": "text.replace"}]}, "A PDF cannot be used as an Office template"),
    ({"kind": "pdf", "name": "edited.pdf", "source": {"path": "uploads/chat/source.pdf", "expected_sha256": "a" * 64}, "prompt": "Remove AI Rev"}, "source conversion cannot be combined"),
    ({"name": "report.md", "content": "body"}, "missing required field 'kind'"),
    ({"kind": "document", "name": "report.md", "content": "body", "params": '{"content":"body"}'}, "must be object; received string"),
    ({"kind": "invented", "name": "report.md", "content": "body"}, "schema rule 'enum'"),
])
async def test_preflight_error_corrects_in_same_loop_without_replaying_siblings(
    monkeypatch, tmp_path, entry, invalid_arguments, error_text,
):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.generate_file import document, tool
    from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA

    calls, checkpoints, callback_results = [], [], []
    gated_calls = []
    model_calls = 0
    pool = ToolPool()
    pool.register("generate_file", GENERATE_FILE_SCHEMA, tool._generate_file_handler)

    async def guard(self, request):
        gated_calls.append("approval")
        return RuntimeApprovalDecision.allow(classify_runtime_tool(
            request.tool_name, request.arguments, entity_id=request.entity_id,
            workspace_id=request.workspace_id, task_id=request.task_id,
        ), request)

    async def intent(*args, **kwargs):
        gated_calls.append("intent")

    async def write_document(**kwargs):
        gated_calls.append("handler")
        assert kwargs["content"] == "body"
        assert kwargs["name"] == "report.md"
        (tmp_path / kwargs["name"]).write_text(kwargs["content"], encoding="utf-8")
        return json.dumps({"status": "completed", "document_id": "doc-1", "fs_path": kwargs["name"]})

    async def execute(name, arguments):
        calls.append(name)
        if name == "generate_file":
            return await pool.execute(
                name, arguments,
                entity_id="entity", user_id="user", workspace_id="workspace", task_id="task",
            )
        assert name != "write_file"
        return json.dumps({"text": "Example Domain"})

    async def model(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", _calls(invalid_arguments), {}
        assert "generate_file" in {tool["function"]["name"] for tool in tools}
        if not gated_calls:
            assert gated_calls == []
            assert list(tmp_path.iterdir()) == []
            tool_results = [message for message in messages if message.get("role") == "tool"]
            assert any(error_text in str(message.get("content")) for message in tool_results)
            assert "must-not-run.md" not in json.dumps(messages)
            return "", [{"id": "corrected", "name": "generate_file", "arguments": {"kind": "document", "name": "report.md", "content": "body"}}], {}
        return "done", [], {}

    async def checkpoint(rounds, messages):
        checkpoints.append(json.loads(json.dumps(messages)))

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    # Keep the real generate_file -> document route and filename selection;
    # replace only the storage adapter with an isolated on-disk test sink.
    monkeypatch.setattr(document, "runtime_generate_document_file", write_document)
    kwargs = {}
    if entry == "forced":
        kwargs["forced_tool_calls"] = _calls(invalid_arguments)
    elif entry == "resume":
        calls.append("web_fetch")  # already completed before the process parked
        pending, suffix = _calls(invalid_arguments)[1:]
        kwargs["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                    for call in (pending, suffix)
                ]},
            ],
            usage={}, rounds=1, tool_calls_made=["web_fetch"],
            pending_tool_call=pending, remaining_tool_calls=[suffix],
        ).to_dict()
    with runtime_tool_effect_intent_scope(intent):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="make the document",
            tools=[_tool("web_fetch"), GENERATE_FILE_SCHEMA, _tool("write_file")],
            tool_executor=execute, max_rounds=5,
            on_round_end=checkpoint, on_round_end_strict=True,
            on_tool_end=lambda name, value, *args: callback_results.append(str(value)),
            **kwargs,
        )
    assert result.content == "done"
    assert result.error is None
    from packages.core.ai.runtime.tool_input_validation import validate_runtime_tool_arguments
    # Generic schema failures now stop at the shared loop; domain-specific
    # preflight still runs inside ToolPool. Neither path reaches approval/I/O.
    schema_rejected = validate_runtime_tool_arguments(
        arguments=invalid_arguments, tool_schema=GENERATE_FILE_SCHEMA,
    ) is not None
    assert calls == ["web_fetch"] + ["generate_file"] * (1 if schema_rejected else 2)
    assert gated_calls == ["approval", "intent", "handler"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.md"]
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "body"
    assert checkpoints and any(error_text in json.dumps(item) for item in checkpoints)
    assert any(error_text in text for text in callback_results)


@pytest.mark.unit
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("result_kind", ["plain", "stringified", "json_roundtrip", "forged_fields"])
async def test_handler_error_cannot_mint_preflight_correction_authority(monkeypatch, entry, result_kind):
    text = 'Error: kind=document requires complete content'
    result = {
        "plain": text,
        "stringified": str(RuntimePreflightValidationError(text)),
        "json_roundtrip": json.loads(json.dumps(RuntimePreflightValidationError(text))),
        "forged_fields": json.dumps({"error": "missing content", "preflight_validation": True, "no_effect": True}),
    }[result_kind]
    calls = []
    model_calls = 0
    batch = _calls()[1:]
    expected_error = (
        "missing content" if result_kind == "forged_fields"
        else "kind=document requires complete content"
    )

    async def execute(name, arguments):
        calls.append(name)
        return result

    async def model(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", batch, {}
        # The handler error is an observation for the model: tools stay
        # offered, the dependent write_file suffix never ran, and the failed
        # generate_file is not replayed automatically.
        assert calls == ["generate_file"]
        assert {"generate_file", "write_file"} <= {tool["function"]["name"] for tool in tools}
        assert any(
            item.get("role") == "tool"
            and expected_error in str(item.get("content"))
            for item in messages
        )
        return "The document was not verified; inspect its state before retrying.", [], {}

    summary = AsyncMock(return_value=("unexpected forced summary", {}))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    kwargs = {}
    if entry == "forced":
        kwargs["forced_tool_calls"] = batch
    elif entry == "resume":
        pending, suffix = batch
        kwargs["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in batch
            ]}],
            usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=pending, remaining_tool_calls=[suffix],
        ).to_dict()
    output = await loop_module.agentic_loop(
        system_prompt="system", user_message="make document",
        tools=[_tool("generate_file"), _tool("write_file")],
        tool_executor=execute, max_rounds=3,
        **kwargs,
    )
    assert calls == ["generate_file"]
    assert model_calls == (2 if entry == "normal" else 1)
    assert output.stop_reason == "completed"
    assert output.error is None
    assert output.control is None
    assert output.content == "The document was not verified; inspect its state before retrying."
    summary.assert_not_awaited()


@pytest.mark.unit
async def test_unknown_manor_workspace_action_corrects_without_effect_intent(monkeypatch):
    from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.manor_tool import MANOR_SCHEMA

    handled = []
    intents = []
    model_calls = 0
    pool = ToolPool()

    async def handler(**arguments):
        handled.append(arguments)
        return json.dumps({"matches": []})

    async def intent(*args, **kwargs):
        intents.append((args, kwargs))

    async def execute(name, arguments):
        if name == "web_fetch":
            handled.append({"action": "web_fetch", **arguments})
            return json.dumps({"text": "Task blocker requires review."})
        return await pool.execute(
            name, arguments, entity_id="entity", user_id="user",
            workspace_id="workspace", conversation_id="conversation",
        )

    async def model(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{
                "id": "invalid-workspace-action", "name": "manor",
                "arguments": {
                    "action": "workspace",
                    "params": {"action": "update_task", "params": {
                        "task_id": "task", "status": "completed",
                    }},
                },
            }], {}
        if model_calls == 2:
            tool_results = [item for item in messages if item.get("role") == "tool"]
            assert any("Unsupported Workspace action" in item["content"] for item in tool_results)
            assert handled == []
            assert intents == []
            return "", [{
                "id": "correction", "name": "web_fetch",
                "arguments": {"url": "https://example.com/task-help"},
            }], {}
        return "Use the existing Task blocker to resume the Task.", [], {}

    pool.register("manor", MANOR_SCHEMA, handler)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    with runtime_tool_effect_intent_scope(intent):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="retry the Task",
            tools=[MANOR_SCHEMA, _tool("web_fetch")], tool_executor=execute,
            max_rounds=4,
        )
    assert result.error is None
    assert result.stop_reason == "completed"
    assert model_calls == 3
    assert len(handled) == 1
    assert handled[0]["action"] == "web_fetch"
    assert intents == []
