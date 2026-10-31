"""The chat transport preserves the Runtime's chosen output language."""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.response_language import response_language_guidance
from packages.core.ai.runtime import ChatSurface
from packages.core.ai.runtime.output_policy import runtime_public_tool_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [None, "workspace_1"])
@pytest.mark.parametrize("final_delivery", ["stream", "return", "replace", "recover", "nested", "nested_replace", "skill_terminal"])
@pytest.mark.parametrize(
    ("locale", "user_message"),
    [
        ("en", "帮我生成周报"),
        ("zh", "请用英语写周报和进度说明"),
    ],
)
async def test_stream_preserves_language_choice_in_deltas_and_persisted_blocks(
    monkeypatch, workspace_id, final_delivery, locale, user_message,
):
    from packages.core import database
    from packages.core.services import chat_service

    opening = "Now let me prepare the weekly report."
    process_note = "Good progress: the source documents are ready."
    final_text = "I have completed the report and saved it to Knowledge."
    saved = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            pass

    async def context(*_args, **_kwargs):
        ctx = SimpleNamespace(
            user=SimpleNamespace(locale=locale), entity=None, agent_id="agent_1",
            workspace_id=workspace_id, task_id=None, runtime_envelope=None,
            tool_profile=None, allowed_tool_names=None,
        )
        return response_language_guidance(locale), [], [], ctx

    async def loop(**kwargs):
        assert f"({locale})" in kwargs["system_prompt"]
        stream_handler = kwargs["stream_handler"]
        if final_delivery == "skill_terminal":
            from packages.core.ai.agentic_loop import agentic_loop
            from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
            from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result, runtime_manual_skill_result_stops_parent
            from packages.core.ai.runtime.streams import runtime_skill_nested_tool_callbacks, runtime_skill_progress_callbacks

            async def execute_skill(_name, args):
                start, end = runtime_skill_nested_tool_callbacks(skill_name="research", invoke_skill_args=args)
                start, handler = runtime_skill_progress_callbacks(start)
                await handler("text_delta", {"content": process_note})
                start("workspace_search", {"query": "report sources"})
                end("workspace_search", '{"count":1}', 5, {"query": "report sources"})
                result = {"content": final_text, "stop_reason": "completed", "tools_used": ["workspace_search"]}
                ctx = SimpleNamespace(manual_skill_selected=True, runtime_envelope=SimpleNamespace(
                    metadata={"turn_execution_plan": {"tool_catalog_mode": "web_research"}},
                ))
                assert runtime_manual_skill_result_stops_parent(ctx, result)
                return runtime_format_invoke_skill_result("research", {
                    **result, "stop_parent": True, "replace_visible_text": True,
                })

            monkeypatch.setattr("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", AsyncMock(
                return_value=(json.dumps({"kind": "progress", "text": opening}), [
                    {"id": "call_research", "name": "invoke_skill", "arguments": {"skill": "research"}},
                ], {}),
            ))
            # The model is a local stub; this transport test makes no billable calls.
            monkeypatch.setattr("packages.core.ai.agentic_loop._preflight_credit_check", AsyncMock())
            return await agentic_loop(
                system_prompt=kwargs["system_prompt"], user_message=user_message,
                tools=[{"type": "function", "function": {"name": "invoke_skill", "parameters": {
                    "type": "object", "properties": {"skill": {"type": "string"}},
                }}}], tool_executor=execute_skill, stream_handler=stream_handler,
                on_tool_start=kwargs["on_tool_start"], on_tool_end=kwargs["on_tool_end"],
                final_response_contract=ChatEvidenceContract(),
            )
        await stream_handler("text_delta", {"content": opening})
        kwargs["on_tool_start"]("manor", {"action": "list_documents"})
        if final_delivery == "recover":
            kwargs["on_tool_end"]("manor", "Tool error (manor): private executor failed", 5, {"action": "list_documents"})
        if final_delivery in {"nested", "nested_replace"}:
            from packages.core.ai.runtime.streams import runtime_tool_stream_sink_var

            runtime_tool_stream_sink_var.get().emit_progress(process_note)
        else:
            await stream_handler("process_note", {"content": process_note})
        if final_delivery != "recover":
            kwargs["on_tool_end"]("manor", '{"count":1}', 5, {"action": "list_documents"})
        if final_delivery != "return":
            await stream_handler("final_response_start", {})
            await stream_handler("text_delta", {
                "content": "The report is almost ready." if final_delivery in {"replace", "nested_replace"} else final_text,
            })
        return AgenticResult(
            content=final_text, messages=[], usage={}, rounds=2,
            tool_calls_made=["manor"],
            control={"replace_visible_text": True} if final_delivery in {"replace", "nested_replace"} else None,
        )

    async def save(**kwargs):
        saved.append(deepcopy(kwargs))
        return "assistant_1"

    monkeypatch.setattr(database, "async_session", Session)
    # The fake session holds no delivery rows; the Workspace owner pays.
    monkeypatch.setattr(
        chat_service, "resolve_run_payer", AsyncMock(side_effect=lambda _db, *, entity_id, workspace_id: entity_id),
    )
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", context)
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", loop)
    monkeypatch.setattr(chat_service, "save_or_update_assistant_stream_message", save)
    monkeypatch.setattr(chat_service, "get_chat_turn_cancel_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(chat_service, "chat_turn_cancellation_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_service, "runtime_persist_chat_stream_runtime_events", AsyncMock())
    monkeypatch.setattr(chat_service, "record_chat_runtime_learning", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_service, "schedule_learning_candidate_applies", AsyncMock())
    monkeypatch.setattr(chat_service, "_publish_chat_stream_snapshot", AsyncMock())
    monkeypatch.setattr(chat_service, "_schedule_tool_path_recording", lambda **_kwargs: None)

    events = []
    async for raw in chat_service.stream_chat_response(
        user_message, "conversation_1", entity_id="entity_1", user_id="user_1",
        agent_id="agent_1", workspace_id=workspace_id, assistant_message_id="assistant_1",
        persist_messages=True,
        runtime_surface=ChatSurface.WORKSPACE_CHAT if workspace_id else ChatSurface.GLOBAL_OWNER_CHAT,
    ):
        if "\ndata: " in raw:
            event = raw.split("\n", 1)[0].removeprefix("event: ")
            events.append((event, json.loads(raw.split("\ndata: ", 1)[1])))

    assert not [payload for event, payload in events if event == "error"]
    deltas = "".join(payload["content"] for event, payload in events if event == "text_delta")
    assert opening in deltas
    assert final_text in deltas
    assert any(event == "process_note" and payload["content"] == process_note for event, payload in events)
    assert saved[-1]["content"] == final_text
    persisted_blocks = saved[-1]["meta"]["assistant_blocks"]
    serialized_blocks = json.dumps(persisted_blocks)
    if final_delivery in {"replace", "nested_replace", "skill_terminal"}:
        assert any(event == "text_reset" for event, _payload in events)
        assert "The report is almost ready." not in serialized_blocks
    assert opening in serialized_blocks
    assert process_note in serialized_blocks
    if final_delivery in {"nested", "nested_replace", "skill_terminal"}:
        assert any(block.get("phase") == "progress" and process_note in block["text"] for block in persisted_blocks)
    assert final_text in serialized_blocks
    stream_end = next(payload for event, payload in events if event == "stream_end")
    assert stream_end["assistant_blocks"] == runtime_public_tool_payload(persisted_blocks)
