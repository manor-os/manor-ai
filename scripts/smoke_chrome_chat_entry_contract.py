#!/usr/bin/env python3
"""No-dependency smoke test for Manor chat entry -> Chrome Runtime path.

This sits one layer above ``smoke_chrome_runtime_harness_contract.py``. It
does not start FastAPI, touch a database, or call a model. Instead it proves
the production chat entrypoints still hand a Chrome-intent turn into the
Runtime Harness with the entity/user/conversation/workspace context, and that
the non-streaming chat runner still delegates tool execution to the Runtime
Harness path that owns the Chrome MCP tool surface.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import types
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _require_fragment(source: str, fragment: str, label: str) -> None:
    if fragment not in source:
        raise AssertionError(f"missing {label}: {fragment!r}")


def _require_regex(source: str, pattern: str, label: str) -> None:
    if re.search(pattern, source, re.S) is None:
        raise AssertionError(f"missing {label}: /{pattern}/")


async def check_runtime_run_chat_turn_delegates() -> dict[str, Any]:
    from packages.core.ai.runtime.chat_turns import runtime_run_chat_turn
    from packages.core.ai.runtime.surfaces import ChatSurface

    fake_service = types.ModuleType("packages.core.services.chat_service")
    calls: list[dict[str, Any]] = []

    async def run_chat_message(message: str, conversation_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({
            "message": message,
            "conversation_id": conversation_id,
            "kwargs": kwargs,
        })
        return {
            "conversation_id": conversation_id,
            "message_id": "msg-chat-entry-smoke",
            "content": "Chrome read_page complete.",
            "tool_calls_made": ["mcp__chrome__read_page"],
            "usage": {},
            "rounds": 1,
            "stop_reason": "chrome_chat_entry_smoke",
        }

    fake_service.run_chat_message = run_chat_message  # type: ignore[attr-defined]

    module_name = "packages.core.services.chat_service"
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = fake_service
    try:
        result = await runtime_run_chat_turn(
            "打开当前 Chrome 页面并识别内容",
            "conv-chat-entry-smoke",
            surface="global_owner_chat",
            entity_id="ent-chat-entry-smoke",
            user_id="user-chat-entry-smoke",
            agent_id="agent-chat-entry-smoke",
            workspace_id="ws-chat-entry-smoke",
            db="db-chat-entry-smoke",  # type: ignore[arg-type]
            manual_skill_refs=[{"id": "skill-smoke"}],
            blocked_tools=["web_search"],
            editor_context={"kind": "none"},
            runtime_metadata={"chrome_contract": "chat-entry"},
        )
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

    if result.get("tool_calls_made") != ["mcp__chrome__read_page"]:
        raise AssertionError(f"runtime_run_chat_turn did not return Chrome result: {result}")
    if len(calls) != 1:
        raise AssertionError(f"expected exactly one run_chat_message call, got {len(calls)}")

    call = calls[0]
    expected_kwargs = {
        "entity_id": "ent-chat-entry-smoke",
        "user_id": "user-chat-entry-smoke",
        "agent_id": "agent-chat-entry-smoke",
        "workspace_id": "ws-chat-entry-smoke",
        "db": "db-chat-entry-smoke",
        "manual_skill_refs": [{"id": "skill-smoke"}],
        "blocked_tools": ["web_search"],
        "editor_context": {"kind": "none"},
        "runtime_metadata": {"chrome_contract": "chat-entry"},
        "runtime_surface": ChatSurface.GLOBAL_OWNER_CHAT,
    }
    if call["message"] != "打开当前 Chrome 页面并识别内容":
        raise AssertionError(f"wrong delegated message: {call}")
    if call["conversation_id"] != "conv-chat-entry-smoke":
        raise AssertionError(f"wrong delegated conversation id: {call}")
    if call["kwargs"] != expected_kwargs:
        raise AssertionError(
            "runtime_run_chat_turn delegated wrong kwargs\n"
            f"expected={expected_kwargs!r}\nactual={call['kwargs']!r}"
        )

    return {
        "status": "ok",
        "surface": str(call["kwargs"]["runtime_surface"].value),
        "tool_calls_made": result.get("tool_calls_made"),
    }


def check_api_chat_route_source_contract() -> dict[str, Any]:
    source = _read("apps/api/routers/chat.py")
    _require_fragment(source, "@router.post(\"/message\", response_model=ChatMessageResponse)", "chat message route")
    _require_fragment(source, "async def chat_message(", "chat_message endpoint")
    _require_fragment(source, "result = await runtime_run_chat_turn(", "chat route Runtime Harness call")
    _require_fragment(source, "llm_message,", "chat route passes LLM message")
    _require_fragment(source, "conv.id,", "chat route passes conversation id")
    _require_fragment(source, "surface=_surface_for_chat_request(", "chat route passes explicit surface")
    _require_fragment(source, "entity_id=user.entity_id,", "chat route passes entity")
    _require_fragment(source, "user_id=user.id,", "chat route passes user")
    _require_fragment(source, "agent_id=agent_id,", "chat route passes agent")
    _require_fragment(source, "workspace_id=workspace_id,", "chat route passes workspace")
    _require_fragment(source, "blocked_tools=_parse_csv_names(blocked_tools),", "chat route passes blocked tools")
    _require_fragment(source, "runtime_metadata=_runtime_metadata_for_chat_mode(", "chat route passes runtime metadata")
    _require_fragment(source, "tool_calls_made=result.get(\"tool_calls_made\", []),", "chat route returns tool call names")
    _require_regex(
        source,
        r"def _surface_for_chat_request\(.*?return infer_chat_surface\(",
        "chat route uses Runtime surface inference",
    )
    return {"status": "ok", "route": "/api/v1/chat/message"}


def check_chat_turn_source_contract() -> dict[str, Any]:
    source = _read("packages/core/ai/runtime/chat_turns.py")
    _require_fragment(source, "resolved_surface = normalize_surface(surface)", "chat turn normalizes surface")
    _require_fragment(source, "raise ValueError(\"Runtime chat turn requires an explicit surface\")", "chat turn requires surface")
    _require_fragment(source, "from packages.core.services.chat_service import run_chat_message", "chat turn imports service at call time")
    _require_fragment(source, "runtime_surface=resolved_surface,", "chat turn passes resolved surface")
    _require_fragment(source, "blocked_tools=blocked_tools,", "chat turn passes blocked tools")
    _require_fragment(source, "runtime_metadata=runtime_metadata,", "chat turn passes runtime metadata")
    return {"status": "ok", "entrypoint": "runtime_run_chat_turn"}


def check_chat_service_source_contract() -> dict[str, Any]:
    source = _read("packages/core/services/chat_service.py")
    _require_regex(
        source,
        r"await resolve_runtime_chat_context\(.*?blocked_tools=blocked_tools,.*?runtime_metadata=runtime_metadata,.*?runtime_surface=runtime_surface,",
        "chat service resolves Runtime context with request controls",
    )
    _require_regex(
        source,
        r"result = await runtime_execute_chat_agent_loop\(.*?conversation_id=conversation_id,.*?workspace_id=ctx\.workspace_id,.*?active_user_message=runtime_message_text_for_intent\(message\),.*?allowed_tool_names=ctx\.allowed_tool_names,.*?forced_tool_calls=runtime_forced_tool_calls_for_turn\(ctx, manual_skill_refs, message\),",
        "chat service executes Runtime Harness loop with context and allowed tools",
    )
    _require_fragment(source, "runtime_record_tool_start_for_chat(", "chat service records tool start events")
    _require_fragment(source, "runtime_record_tool_end_for_chat(", "chat service records tool end events")
    return {"status": "ok", "runner": "run_chat_message"}


async def main() -> int:
    result = {
        "status": "ok",
        "runtime_run_chat_turn": await check_runtime_run_chat_turn_delegates(),
        "api_chat_route": check_api_chat_route_source_contract(),
        "chat_turn_source": check_chat_turn_source_contract(),
        "chat_service_source": check_chat_service_source_contract(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
