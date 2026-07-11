"""Repo-wide contract smoke coverage for Manor tools and MCP wrappers.

These tests intentionally avoid real third-party calls.  They verify the
cross-cutting contracts the agent runtime depends on before any OAuth/API-key
credential is present: schemas are valid, names are unique, handlers are
callable, and MCP wrappers return friendly error envelopes for unknown tools.

Provider-specific behavior belongs in the dedicated MCP/runtime tests.  Keep
this file limited to repo-wide invariants so it does not duplicate those suites.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


def _mcp_module_names() -> list[str]:
    modules: list[str] = []
    for path in Path("packages/core/ai/mcp").glob("*.py"):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "def list_tools" in source and "call_tool" in source:
            modules.append(path.stem)
    return sorted(modules)


def _mcp_schema(tool: dict[str, Any]) -> dict[str, Any] | None:
    schema = tool.get("inputSchema") or tool.get("parameters")
    return schema if isinstance(schema, dict) else None


def _assert_object_schema(schema: dict[str, Any], *, label: str) -> None:
    assert schema.get("type") == "object", f"{label}: schema must be object"
    assert isinstance(schema.get("properties", {}), dict), f"{label}: properties must be dict"
    assert isinstance(schema.get("required", []), list), f"{label}: required must be list"
    missing_required = [name for name in schema.get("required", []) if name not in schema.get("properties", {})]
    assert not missing_required, f"{label}: required fields missing from properties: {missing_required}"


def test_tool_pool_registers_valid_unique_function_schemas_and_handlers() -> None:
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()

    names = pool.registered_tool_names()
    assert len(names) == len(set(names))
    assert "manor" in names
    assert "search_tools" in names

    for name, schema in pool.registered_tool_schemas():
        assert schema.get("type") == "function", f"{name}: tool schema must be function"
        function = schema.get("function")
        assert isinstance(function, dict), f"{name}: missing function schema"
        assert function.get("name") == name, f"{name}: function.name must match registry key"
        assert isinstance(function.get("description"), str), f"{name}: description must be string"
        _assert_object_schema(function.get("parameters") or {}, label=name)

        entry = pool.get(name)
        assert entry is not None, f"{name}: missing pool entry"
        assert callable(entry.get("handler")), f"{name}: handler must be callable"


def test_all_mcp_wrappers_list_valid_unique_tool_schemas() -> None:
    module_names = _mcp_module_names()
    assert module_names, "No MCP wrapper modules discovered"

    for module_name in module_names:
        module = importlib.import_module(f"packages.core.ai.mcp.{module_name}")
        tools = module.list_tools()
        assert isinstance(tools, list), f"{module_name}: list_tools must return list"

        names: set[str] = set()
        for tool in tools:
            name = tool.get("name")
            assert isinstance(name, str) and name, f"{module_name}: tool name required"
            assert name not in names, f"{module_name}: duplicate tool name {name}"
            names.add(name)
            assert isinstance(tool.get("description"), str) and tool["description"].strip(), (
                f"{module_name}.{name}: description required"
            )
            schema = _mcp_schema(tool)
            assert schema is not None, f"{module_name}.{name}: inputSchema/parameters required"
            _assert_object_schema(schema, label=f"{module_name}.{name}")


@pytest.mark.asyncio
async def test_all_mcp_wrappers_unknown_tool_returns_error_envelope() -> None:
    for module_name in _mcp_module_names():
        module = importlib.import_module(f"packages.core.ai.mcp.{module_name}")
        result = await module.call_tool("__not_a_real_tool__", {}, "")
        assert isinstance(result, dict), f"{module_name}: call_tool must return dict"
        assert result.get("isError") is True or bool(result.get("error")) or result.get("status") == "error", (
            f"{module_name}: unknown tool should return error envelope"
        )
