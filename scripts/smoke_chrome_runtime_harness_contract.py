#!/usr/bin/env python3
"""No-dependency smoke test for Runtime Harness -> Chrome MCP execution.

This is the closest lightweight proxy for a Manor chat turn without booting
the full API/UI stack or calling a real model. It runs
``runtime_execute_agentic_loop`` with a forced Chrome tool call, proving the
Runtime Harness-owned executor sends ``mcp__chrome__read_page`` through the
global ToolPool, MCP registry, Chrome MCP module, and local-worker dispatch
wrapper with the active user message and chat/workspace/task context intact.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "smoke_chrome_mcp_builtin_execute_contract.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "smoke_chrome_mcp_builtin_execute_contract",
        HELPER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    helper = _load_helper()
    chrome = helper._install_fake_mcp_package()

    from packages.core.ai import agentic_loop as agentic_loop_module
    from packages.core.ai.runtime import billing as runtime_billing
    from packages.core.ai.runtime import tool_execution
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision
    from packages.core.ai.runtime import approvals as runtime_approvals
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.tool_pool import tool_pool

    async def _no_credit_preflight() -> None:
        return None

    def _no_billing_context(*args: Any, **kwargs: Any) -> None:
        return None

    class AllowAllApprovalMiddleware:
        async def guard_request(self, request: Any) -> RuntimeApprovalDecision:
            return RuntimeApprovalDecision.allow()

        async def guard_tool_action(self, **kwargs: Any) -> None:
            return None

        def tool_block_event(self, *args: Any, **kwargs: Any) -> None:
            return None

    agentic_loop_module._preflight_credit_check = _no_credit_preflight
    runtime_billing.runtime_ensure_billing_context = _no_billing_context
    tool_execution.RuntimeApprovalMiddleware = AllowAllApprovalMiddleware
    runtime_approvals.RuntimeApprovalMiddleware = AllowAllApprovalMiddleware
    helper._install_fake_runtime_dependencies()
    helper._install_fake_sqlalchemy_select()

    mcp_builtin = helper._load_module(
        "packages.core.ai.tools.mcp_builtin",
        helper.MCP_BUILTIN_PATH,
    )

    tool_pool._tools.clear()
    for schema, handler in mcp_builtin.get_tools():
        name = schema["function"]["name"]
        if name.startswith("mcp__chrome__"):
            tool_pool.register(name, schema, handler, deferred=False)
    tool_pool._initialized = True

    calls: list[dict[str, Any]] = []

    async def fake_dispatch_local_action(**kwargs: Any) -> dict[str, Any]:
        ctx = chrome._local_worker_runner.get_call_context()
        calls.append({"ctx": ctx, "dispatch": kwargs})
        return {
            "status": "complete",
            "provider": kwargs["provider"],
            "action_key": kwargs["action_key"],
            "params": kwargs["params"],
            "ctx": ctx,
        }

    chrome._local_worker_runner.dispatch_local_action = fake_dispatch_local_action

    user_message = "打开当前 Chrome 页面并识别内容"
    result = await runtime_execute_agentic_loop(
        runtime_envelope=None,
        system_prompt="You operate Chrome through Manor tools.",
        user_message=user_message,
        tools=[tool_pool.get_schema("mcp__chrome__read_page")],
        entity_id="ent-runtime-smoke",
        agent_id="agent-runtime-smoke",
        user_id="user-runtime-smoke",
        workspace_id="ws-runtime-smoke",
        conversation_id="conv-runtime-smoke",
        task_id="task-runtime-smoke",
        active_user_message=None,
        forced_tool_calls=[{
            "name": "mcp__chrome__read_page",
            "arguments": {"tabId": 321},
        }],
        terminal_tool_result_policy={
            "terminal_tool_results": [{
                "tool_names": ["mcp__chrome__read_page"],
                "statuses": ["complete"],
                "notice": "Chrome read_page complete.",
                "stop_reason": "chrome_runtime_smoke",
            }],
        },
        max_rounds=3,
    )

    if getattr(result, "stop_reason", "") != "chrome_runtime_smoke":
        raise AssertionError(f"runtime loop did not stop on Chrome tool result: {result}")
    if "mcp__chrome__read_page" not in getattr(result, "tool_calls_made", []):
        raise AssertionError(f"Chrome tool call not recorded: {result.tool_calls_made}")
    if len(calls) != 1:
        raise AssertionError(f"expected one Chrome dispatch, got {len(calls)}")

    dispatch = calls[0]["dispatch"]
    ctx = calls[0]["ctx"]
    expected_ctx = {
        "user_id": "user-runtime-smoke",
        "entity_id": "ent-runtime-smoke",
        "conversation_id": "conv-runtime-smoke",
        "workspace_id": "ws-runtime-smoke",
        "task_id": "task-runtime-smoke",
        "active_user_message": user_message,
    }
    if ctx != expected_ctx:
        raise AssertionError(f"context mismatch: expected={expected_ctx}, actual={ctx}")

    expected_dispatch = {
        "provider": "browser_mcp",
        "action_key": "tools/call",
        "params": {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "mcp__chrome__read_page",
                "arguments": {"tabId": 321},
            },
        },
        "timeout": 600,
        "risk_level": "medium",
        "step_key": "browser_mcp_read_page",
        "target_worker_id": "",
    }
    dispatch_compare = json.loads(json.dumps(dispatch))
    request_id = dispatch_compare.get("params", {}).pop("id", "")
    if not request_id:
        raise AssertionError(f"dispatch missing JSON-RPC id: {dispatch}")
    if dispatch_compare != expected_dispatch:
        raise AssertionError(
            "dispatch mismatch\n"
            f"expected={json.dumps(expected_dispatch, sort_keys=True)}\n"
            f"actual={json.dumps(dispatch_compare, sort_keys=True)}"
        )

    print(json.dumps({
        "status": "ok",
        "stop_reason": result.stop_reason,
        "rounds": result.rounds,
        "tool_calls_made": result.tool_calls_made,
        "dispatch": dispatch,
        "ctx": ctx,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
