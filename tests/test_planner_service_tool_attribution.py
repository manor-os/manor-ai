"""An Agent's tool surface comes from its own bindings, never another service's."""
import pytest

from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_surface_from_schemas


@pytest.mark.unit
@pytest.mark.parametrize("tool_profile", [None, WORKSPACE_AGENT_TOOL_PROFILE])
def test_empty_bindings_keep_runtime_helpers_without_borrowing_file_tools(tool_profile):
    registry = [
        (name, {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}})
        for name in ["search_tools", "generate_file"]
    ]
    atlas = runtime_registered_tool_surface_from_schemas(
        registry, bound_tool_names=set(), is_master=False, tool_profile=tool_profile,
    )
    delivery = runtime_registered_tool_surface_from_schemas(
        registry, bound_tool_names={"generate_file"}, is_master=False, tool_profile=tool_profile,
    )

    assert "search_tools" in atlas.visible_tool_names
    assert "generate_file" not in atlas.visible_tool_names
    assert "generate_file" in delivery.visible_tool_names
