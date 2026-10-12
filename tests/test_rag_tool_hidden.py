"""Regression coverage for temporarily hiding the rag tool from LLM surfaces."""
from __future__ import annotations

from packages.core.ai.runtime.capability_bindings import (
    WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES,
    normalize_workspace_custom_agent_tool_bindings,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_surface import runtime_public_agent_tool_surface
from packages.core.ai.runtime.tool_visibility import (
    HIDDEN_RUNTIME_TOOLS,
    MASTER_ALWAYS_LOADED,
    WORKSPACE_AGENT_ALWAYS_LOADED,
    runtime_tool_visible_for_profile,
    runtime_workspace_capability_tool_groups,
)


def test_rag_remains_registered_but_is_not_discoverable():
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()

    assert pool.get("rag") is not None
    assert pool.get_schema("rag") is not None
    assert "rag" not in pool.registered_tool_names()
    assert "rag" in pool.registered_tool_names(include_undiscoverable=True)
    assert "rag" not in {match["name"] for match in pool.search("rag", max_results=10)}
    assert "rag" not in {match["name"] for match in pool.search("knowledge", max_results=10)}


def test_rag_is_hidden_from_eager_and_visible_runtime_surfaces():
    assert "rag" in HIDDEN_RUNTIME_TOOLS
    assert "rag" not in MASTER_ALWAYS_LOADED
    assert "rag" not in WORKSPACE_AGENT_ALWAYS_LOADED
    assert "rag" not in WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES
    assert not runtime_tool_visible_for_profile(
        "rag",
        bound_tool_names={"rag"},
        is_master=True,
        mcp_allowed_names=None,
    )
    assert not runtime_tool_visible_for_profile(
        "rag",
        bound_tool_names={"rag"},
        is_master=False,
        mcp_allowed_names=None,
    )

    always_names = {tool["name"] for tool in runtime_workspace_capability_tool_groups()["always"]}
    assert "rag" not in always_names


def test_rag_is_stripped_from_custom_agent_bindings_and_public_chat():
    tools = normalize_workspace_custom_agent_tool_bindings(
        ["rag", "web_search"],
        business_capability_ids=["workspace.search"],
    )
    assert "web_search" in tools
    assert "rag" not in tools

    public_tools, allowed = runtime_public_agent_tool_surface(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        bound_tool_names={"rag", "workspace_create_task"},
    )
    assert allowed == {"workspace_create_task"}
    assert [tool["function"]["name"] for tool in public_tools] == ["workspace_create_task"]


def test_rag_node_is_not_offered_for_new_workflows():
    from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES, HIDDEN_NODE_TYPES
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance
    from packages.core.services import workflow_generator
    from packages.core.services.workflow_service import SUPPORTED_WORKFLOW_STEP_TYPES

    assert "rag" in HIDDEN_NODE_TYPES
    # Existing workflows keep saving and running their rag steps.
    assert "rag" in CANONICAL_NODE_TYPES and "rag" in SUPPORTED_WORKFLOW_STEP_TYPES
    # The AI generator neither offers nor coerces into it.
    assert "rag" not in workflow_generator._ALLOWED_TYPES
    assert workflow_generator._coerce_type("knowledge") == "unsupported"
    assert workflow_generator._coerce_type("rag") == "unsupported"
    assert "rag " not in workflow_generator.WORKFLOW_GEN_SYSTEM_PROMPT
    guidance = runtime_tool_usage_guidance(tool_names=["rag", "manor"], has_tools=True) or ""
    assert "`rag`" not in guidance
