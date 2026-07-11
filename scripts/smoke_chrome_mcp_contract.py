#!/usr/bin/env python3
"""No-dependency smoke test for the Chrome MCP wrapper contract.

This intentionally avoids importing ``packages.core.ai.mcp`` through its
package ``__init__`` so it can run in lightweight local environments that do
not have the full API test dependency set installed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "packages" / "core" / "ai" / "mcp"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_mcp_module(name: str):
    fullname = f"packages.core.ai.mcp.{name}"
    path = MCP_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(fullname, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


def load_chrome_module():
    # Stub only the mcp package to avoid running packages.core.ai.mcp.__init__.
    package = types.ModuleType("packages.core.ai.mcp")
    package.__path__ = [str(MCP_DIR)]  # type: ignore[attr-defined]
    sys.modules["packages.core.ai.mcp"] = package
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class AsyncClient:
            def __init__(self, *args: Any, **kwargs: Any):
                pass

            async def __aenter__(self) -> "AsyncClient":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        httpx.AsyncClient = AsyncClient  # type: ignore[attr-defined]
        sys.modules["httpx"] = httpx
    _load_mcp_module("_local_worker_runner")
    return _load_mcp_module("chrome")


async def main() -> int:
    chrome = load_chrome_module()
    calls: list[dict[str, Any]] = []

    async def fake_dispatch_local_action(**kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "result": {
                "provider": "browser_mcp",
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"echo": kwargs}, ensure_ascii=False),
                    }],
                    "isError": False,
                },
            },
        }

    def expected_dispatch(
        tool_name: str,
        args: dict[str, Any],
        timeout: int = 600,
        mcp_tool_name: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": "browser_mcp",
            "action_key": "tools/call",
            "params": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": mcp_tool_name or f"mcp__chrome__{tool_name}",
                    "arguments": args,
                },
            },
            "timeout": timeout,
            "risk_level": "medium",
            "step_key": f"browser_mcp_{tool_name}",
            "target_worker_id": "",
        }

    chrome._local_worker_runner.dispatch_local_action = fake_dispatch_local_action

    scenarios = [
        (
            "read_page",
            {"tabId": 321},
            expected_dispatch("read_page", {"tabId": 321}),
        ),
        (
            "claim_tab",
            {"tabId": 321},
            expected_dispatch("claim_tab", {"tabId": 321}),
        ),
        (
            "click_element",
            {"ref": "e1", "timeout_seconds": 7},
            expected_dispatch("click_element", {"ref": "e1"}, timeout=7),
        ),
        (
            "fill_or_select",
            {"ref": "e2", "value": "hello"},
            expected_dispatch("fill_or_select", {"ref": "e2", "value": "hello"}),
        ),
        (
            "wait",
            {"textContains": "ready", "timeout_seconds": 7},
            expected_dispatch("wait", {"textContains": "ready"}, timeout=7),
        ),
        (
            "computer",
            {"action": "click", "ref": "e1"},
            expected_dispatch("computer", {"action": "click", "ref": "e1"}),
        ),
        (
            "type_text",
            {"text": "hello"},
            expected_dispatch("type_text", {"text": "hello"}),
        ),
        (
            "scroll",
            {"direction": "down"},
            expected_dispatch("scroll", {"direction": "down"}),
        ),
        (
            "navigate",
            {"url": "https://example.com/", "tabId": 321},
            expected_dispatch("navigate", {"url": "https://example.com/", "tabId": 321}),
        ),
        (
            "get_interactive_elements",
            {"tabId": 321},
            expected_dispatch(
                "get_interactive_elements",
                {"tabId": 321},
                mcp_tool_name="mcp__chrome__read_page",
            ),
        ),
        (
            "click",
            {"ref": "e1"},
            expected_dispatch("click", {"ref": "e1"}, mcp_tool_name="mcp__chrome__click_element"),
        ),
        (
            "fill",
            {"ref": "e2", "value": "hello"},
            expected_dispatch(
                "fill",
                {"ref": "e2", "value": "hello"},
                mcp_tool_name="mcp__chrome__fill_or_select",
            ),
        ),
        (
            "keyboard",
            {"key": "Enter"},
            expected_dispatch("keyboard", {"key": "Enter"}),
        ),
        (
            "upload",
            {"ref": "e3", "files": ["/tmp/photo.png"]},
            expected_dispatch("upload", {"ref": "e3", "files": ["/tmp/photo.png"]}),
        ),
    ]

    results: list[dict[str, Any]] = []
    for tool_name, args, expected in scenarios:
        before = len(calls)
        result = await chrome.call_tool(tool_name, args, "")
        if result.get("isError") is not False:
            raise AssertionError(f"{tool_name} returned error: {result}")
        if len(calls) != before + 1:
            raise AssertionError(f"{tool_name} did not dispatch exactly once")
        actual = calls[-1]
        actual_compare = json.loads(json.dumps(actual))
        request_id = actual_compare.get("params", {}).pop("id", "")
        if not request_id:
            raise AssertionError(f"{tool_name} dispatch missing JSON-RPC id: {actual}")
        if actual_compare != expected:
            raise AssertionError(
                f"{tool_name} dispatch mismatch\n"
                f"expected={json.dumps(expected, sort_keys=True)}\n"
                f"actual={json.dumps(actual_compare, sort_keys=True)}"
            )
        payload = json.loads(result["content"][0]["text"])
        echo = payload.get("echo")
        if isinstance(echo, dict) and isinstance(echo.get("params"), dict):
            echo["params"].pop("id", None)
        if echo != expected:
            raise AssertionError(f"{tool_name} payload echo mismatch: {payload}")
        results.append({"tool": tool_name, "dispatch": actual})

    print(json.dumps({"status": "ok", "checked": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
