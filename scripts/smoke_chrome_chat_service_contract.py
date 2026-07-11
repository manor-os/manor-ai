#!/usr/bin/env python3
"""No-dependency smoke test for ``run_chat_message`` -> Chrome Runtime path.

The local dev environment used for Chrome smoke tests may not have the full
API dependency set installed. This script still loads the real
``packages/core/services/chat_service.py`` source and executes its
``run_chat_message`` function by stubbing only DB/model/persistence imports.

It proves the non-streaming Manor AI runner hands Chrome MCP tools, the
active user message, context IDs, allowed tool names, and forced tool calls to
``runtime_execute_chat_agent_loop`` instead of bypassing the Runtime Harness.
"""
from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import json
import pathlib
import sys
import time
import types
from types import SimpleNamespace
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAT_SERVICE_PATH = ROOT / "packages" / "core" / "services" / "chat_service.py"
MODULE_NAME = "packages.core.services.chat_service"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


STATE: dict[str, Any] = {
    "context_calls": [],
    "runtime_loop_calls": [],
    "usage_calls": [],
    "tool_events": [],
    "learning_calls": [],
}


class ChatTrace:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started_at = time.time()
        self.trace_id = "trace-chat-service-smoke"
        self.total_usage: dict[str, Any] = {}

    def log_request(self, message: Any) -> None:
        STATE["trace_request"] = message

    def log_llm_call(self, **kwargs: Any) -> None:
        STATE.setdefault("llm_calls", []).append(kwargs)

    def log_tool_exec(self, **kwargs: Any) -> None:
        STATE.setdefault("trace_tool_execs", []).append(kwargs)

    def log_complete(self, **kwargs: Any) -> None:
        STATE["trace_complete"] = kwargs

    def log_error(self, error: str, **kwargs: Any) -> None:
        STATE.setdefault("trace_errors", []).append({"error": error, **kwargs})


class RuntimeToolStreamSink:
    def __init__(
        self,
        *,
        record_tool_event: Any,
        format_tool_arguments: Any,
        format_tool_result: Any,
        resolve_tool_status: Any,
    ) -> None:
        self.record_tool_event = record_tool_event
        self.format_tool_arguments = format_tool_arguments
        self.format_tool_result = format_tool_result
        self.resolve_tool_status = resolve_tool_status


runtime_tool_stream_sink_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "runtime_tool_stream_sink_var",
    default=None,
)


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


async def resolve_runtime_chat_context(
    db: Any,
    message: Any,
    **kwargs: Any,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], Any]:
    STATE["context_calls"].append({"db": db, "message": message, "kwargs": kwargs})
    ctx = SimpleNamespace(
        runtime_envelope=SimpleNamespace(profile=SimpleNamespace(value="owner_copilot")),
        workspace_id=kwargs.get("workspace_id") or "ws-chat-service-smoke",
        task_id="task-chat-service-smoke",
        legacy_runtime_profile="owner_copilot",
        allowed_tool_names={
            "search_tools",
            "mcp__chrome__read_page",
            "mcp__chrome__click_element",
            "mcp__chrome__fill_or_select",
        },
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "mcp__chrome__read_page",
            "description": "Return a Chrome read_page payload.",
            "parameters": {
                "type": "object",
                "properties": {"tabId": {"type": "integer"}},
            },
        },
    }]
    initial_messages = [{"role": "system", "content": "previous context"}]
    return "You operate Chrome through Manor tools.", tools, initial_messages, ctx


async def runtime_execute_chat_agent_loop(**kwargs: Any) -> Any:
    STATE["runtime_loop_calls"].append(kwargs)
    on_tool_start = kwargs.get("on_tool_start")
    on_tool_end = kwargs.get("on_tool_end")
    if on_tool_start:
        on_tool_start("mcp__chrome__read_page", {"tabId": 321})
    if on_tool_end:
        on_tool_end(
            "mcp__chrome__read_page",
            json.dumps({"status": "complete", "read_page_refs_count": 1}),
            7.5,
            args={"tabId": 321},
        )
    return SimpleNamespace(
        content="Chrome read_page complete.",
        usage={"input_tokens": 3, "output_tokens": 5},
        tool_calls_made=["mcp__chrome__read_page"],
        rounds=1,
        stop_reason="chrome_chat_service_smoke",
        error=None,
        error_detail=None,
    )


async def resolve_model_for_user(*args: Any, **kwargs: Any) -> str:
    STATE["model_call"] = {"args": args, "kwargs": kwargs}
    return "smoke-primary-model"


async def record_chat_llm_usage(*args: Any, **kwargs: Any) -> None:
    STATE["usage_calls"].append({"args": args, "kwargs": kwargs})


async def record_chat_runtime_learning(**kwargs: Any) -> list[str]:
    STATE["learning_calls"].append(kwargs)
    return []


async def schedule_learning_candidate_applies(**kwargs: Any) -> None:
    STATE["learning_schedule"] = kwargs


def runtime_record_tool_start_for_chat(events: list[dict[str, Any]], name: str, args: Any) -> None:
    events.append({"name": name, "status": "running", "arguments": args})
    STATE["tool_events"].append({"event": "start", "name": name, "args": args})


def runtime_record_tool_end_for_chat(
    events: list[dict[str, Any]],
    name: str,
    *,
    args: Any = None,
    result: Any = None,
    status: str | None = None,
    duration_ms: float | None = None,
) -> None:
    for event in reversed(events):
        if event.get("name") == name and event.get("status") == "running":
            event.update({
                "status": status or "done",
                "arguments": args,
                "result": result,
                "duration_ms": duration_ms,
            })
            break
    else:
        events.append({
            "name": name,
            "status": status or "done",
            "arguments": args,
            "result": result,
            "duration_ms": duration_ms,
        })
    STATE["tool_events"].append({
        "event": "end",
        "name": name,
        "args": args,
        "result": result,
        "status": status,
    })


def _install_stubs() -> None:
    _module("sqlalchemy.ext.asyncio", AsyncSession=object)

    _module("packages.core.ai.chat_logger", ChatTrace=ChatTrace)
    _module(
        "packages.core.ai.runtime",
        ChannelRuntimeContext=object,
        ChatSurface=str,
        runtime_context_meta=lambda ctx: {"workspace_id": getattr(ctx, "workspace_id", None)},
        runtime_execute_chat_agent_loop=runtime_execute_chat_agent_loop,
        runtime_manual_skill_ids_from_refs=lambda refs: [
            str(ref.get("id") or ref.get("slug"))
            for ref in (refs or [])
            if isinstance(ref, dict)
        ],
        runtime_persist_chat_runtime_events=lambda *args, **kwargs: _async_noop(*args, **kwargs),
        runtime_persist_chat_stream_runtime_events=lambda *args, **kwargs: _async_noop(*args, **kwargs),
        runtime_release_billing_context=lambda handle: STATE.setdefault("released_billing", handle),
        runtime_set_suppressed_billing_context=lambda **kwargs: {"billing": kwargs},
    )
    _module(
        "packages.core.ai.runtime.skill_forcing",
        runtime_forced_tool_calls_for_turn=lambda ctx, manual_skill_refs, message: [{
            "name": "mcp__chrome__read_page",
            "arguments": {"tabId": 321},
        }],
        runtime_message_text_for_intent=lambda message: (
            message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        ),
    )
    _module(
        "packages.core.ai.runtime.output_policy",
        runtime_assistant_result_meta=lambda result: {"stop_reason": getattr(result, "stop_reason", None)},
        runtime_coerce_visible_text_language=lambda text, message: text,
        runtime_fallback_stream_final_summary=lambda *args, **kwargs: "",
        runtime_prefers_chinese=lambda text: False,
        runtime_sanitize_assistant_content_after_loop=lambda content, tool_calls: content,
    )
    _module(
        "packages.core.ai.runtime.streams",
        RuntimeToolStreamSink=RuntimeToolStreamSink,
        runtime_record_tool_end_for_chat=runtime_record_tool_end_for_chat,
        runtime_record_tool_start_for_chat=runtime_record_tool_start_for_chat,
        runtime_should_flush_stream_text=lambda *args, **kwargs: True,
        runtime_tool_arguments_for_chat=lambda name, args: args,
        runtime_tool_result_for_chat=lambda name, result: result,
        runtime_tool_status_for_chat=lambda result: "complete",
        runtime_tool_stream_sink_var=runtime_tool_stream_sink_var,
    )

    _module(
        "packages.core.services.conversation_messages",
        add_message=lambda *args, **kwargs: _async_value(SimpleNamespace(id="msg-stub")),
        resolve_author_subscription_id=lambda *args, **kwargs: _async_value(None),
        save_assistant_stream_error_message=lambda *args, **kwargs: _async_noop(*args, **kwargs),
        save_assistant_stream_interrupted_message=lambda *args, **kwargs: _async_noop(*args, **kwargs),
        save_or_update_assistant_stream_message=lambda *args, **kwargs: _async_noop(*args, **kwargs),
    )
    _module(
        "packages.core.services.hitl_requests",
        hitl_requests_from_data=lambda data: None,
        workspace_operation_pending_action_from_data=lambda data: None,
    )
    _module(
        "packages.core.services.model_resolver",
        resolve_llm_metadata_from_context=lambda ctx: {"model_context": "smoke"},
        resolve_model_for_user=resolve_model_for_user,
        resolve_model_from_context=lambda *args, **kwargs: _async_value("smoke-primary-model"),
    )
    _module(
        "packages.core.services.runtime_chat_context",
        resolve_runtime_chat_context=resolve_runtime_chat_context,
    )
    _module(
        "packages.core.services.runtime_learning",
        record_chat_runtime_learning=record_chat_runtime_learning,
        schedule_learning_candidate_applies=schedule_learning_candidate_applies,
    )
    _module("packages.core.services.sse_events", format_sse=lambda event, data: json.dumps({"event": event, "data": data}))
    _module("packages.core.services.usage_service", record_chat_llm_usage=record_chat_llm_usage)


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _async_value(value: Any) -> Any:
    return value


def _load_chat_service() -> types.ModuleType:
    sys.modules.pop(MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, CHAT_SERVICE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {CHAT_SERVICE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    _install_stubs()
    chat_service = _load_chat_service()
    message = "打开当前 Chrome 页面并识别内容"
    result = await chat_service.run_chat_message(
        message,
        "conv-chat-service-smoke",
        entity_id="ent-chat-service-smoke",
        user_id="user-chat-service-smoke",
        agent_id="agent-chat-service-smoke",
        workspace_id="ws-chat-service-smoke",
        db=None,
        blocked_tools=["web_search"],
        runtime_metadata={"chrome_contract": "chat-service"},
        runtime_surface="global_owner_chat",
    )

    if result.get("tool_calls_made") != ["mcp__chrome__read_page"]:
        raise AssertionError(f"run_chat_message did not record Chrome tool activity: {result}")
    if result.get("stop_reason") != "chrome_chat_service_smoke":
        raise AssertionError(f"wrong stop reason: {result}")

    if len(STATE["context_calls"]) != 1:
        raise AssertionError(f"expected one context resolution, got {STATE['context_calls']}")
    context_kwargs = STATE["context_calls"][0]["kwargs"]
    for key, expected in {
        "entity_id": "ent-chat-service-smoke",
        "user_id": "user-chat-service-smoke",
        "agent_id": "agent-chat-service-smoke",
        "conversation_id": "conv-chat-service-smoke",
        "workspace_id": "ws-chat-service-smoke",
        "blocked_tools": ["web_search"],
        "runtime_metadata": {"chrome_contract": "chat-service"},
        "runtime_surface": "global_owner_chat",
    }.items():
        if context_kwargs.get(key) != expected:
            raise AssertionError(f"context kwarg {key} mismatch: {context_kwargs}")

    if len(STATE["runtime_loop_calls"]) != 1:
        raise AssertionError(f"expected one runtime loop call, got {STATE['runtime_loop_calls']}")
    loop_call = STATE["runtime_loop_calls"][0]
    expected_loop_values = {
        "system_prompt": "You operate Chrome through Manor tools.",
        "user_message": message,
        "entity_id": "ent-chat-service-smoke",
        "user_id": "user-chat-service-smoke",
        "agent_id": "agent-chat-service-smoke",
        "workspace_id": "ws-chat-service-smoke",
        "conversation_id": "conv-chat-service-smoke",
        "task_id": "task-chat-service-smoke",
        "active_user_message": message,
        "legacy_tool_profile": "owner_copilot",
        "model": "smoke-primary-model",
        "metadata": {"model_context": "smoke"},
        "forced_tool_calls": [{"name": "mcp__chrome__read_page", "arguments": {"tabId": 321}}],
    }
    for key, expected in expected_loop_values.items():
        if loop_call.get(key) != expected:
            raise AssertionError(f"runtime loop {key} mismatch: expected={expected!r} actual={loop_call.get(key)!r}")
    if "mcp__chrome__read_page" not in loop_call.get("allowed_tool_names", set()):
        raise AssertionError(f"Chrome read_page missing from allowed tools: {loop_call.get('allowed_tool_names')}")
    if loop_call["tools"][0]["function"]["name"] != "mcp__chrome__read_page":
        raise AssertionError(f"Chrome read_page schema not forwarded: {loop_call['tools']}")
    if len(STATE["usage_calls"]) != 1:
        raise AssertionError(f"usage persistence was not called once: {STATE['usage_calls']}")

    print(json.dumps({
        "status": "ok",
        "conversation_id": result["conversation_id"],
        "tool_calls_made": result["tool_calls_made"],
        "runtime_loop": {
            "tool": loop_call["tools"][0]["function"]["name"],
            "allowed_tool_names": sorted(loop_call["allowed_tool_names"]),
            "active_user_message": loop_call["active_user_message"],
            "forced_tool_calls": loop_call["forced_tool_calls"],
        },
        "usage_calls": len(STATE["usage_calls"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
