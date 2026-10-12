"""Bound first-party tools with small schemas stay first-round eager.

These are Workspace Host Chat visibility contracts, not JR-specific grants.
A wide custom agent still discovers extra tools through search_tools; the
already-authorized invoke_skill / file.read quartet must not wait on that
round. Hidden rag stays undiscoverable. Unbound agents do not receive
file.read for free.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.capability_bindings import (
    WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES,
    expand_authorized_runtime_file_read_bindings,
    normalize_workspace_custom_agent_tool_bindings,
)
from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
from packages.core.ai.runtime.tool_visibility import (
    HIDDEN_RUNTIME_TOOLS,
    RuntimeAgentBoundToolLoadingMode,
    resolve_runtime_tool_surface,
    runtime_tool_visible_for_profile,
)
from packages.core.constants.agent_capabilities import AGENT_EAGER_BOUND_TOOL_LIMIT


FILE_READ = ("read_file", "list_files", "glob_files", "grep_files")


def _wide_bound(*extra: str) -> set[str]:
    padding = {f"bound_tool_{index}" for index in range(AGENT_EAGER_BOUND_TOOL_LIMIT + 1)}
    return padding | {name for name in extra}


def _surface(registered, bound, *, is_master: bool = False, tool_profile: str | None = WORKSPACE_AGENT_TOOL_PROFILE):
    return resolve_runtime_tool_surface(
        registered,
        bound_tool_names=bound,
        is_master=is_master,
        mcp_allowed_names=None if is_master else set(),
        tool_profile=tool_profile,
    )


def test_wide_skill_bound_agent_eagers_invoke_skill() -> None:
    bound = _wide_bound("invoke_skill")
    surface = _surface(("search_tools", "invoke_skill", *sorted(bound)), bound)

    assert surface.bound_tool_loading_mode is RuntimeAgentBoundToolLoadingMode.PROGRESSIVE
    assert "invoke_skill" in surface.eager_tool_names
    assert "invoke_skill" not in surface.deferred_tool_names
    assert "bound_tool_0" in surface.deferred_tool_names


def test_wide_unbound_skill_agent_does_not_eager_invoke_skill() -> None:
    bound = _wide_bound()
    surface = _surface(("search_tools", "invoke_skill", *sorted(bound)), bound)

    assert "invoke_skill" not in surface.eager_tool_names
    assert "invoke_skill" not in surface.visible_tool_names


def test_wide_authorized_file_read_is_eager() -> None:
    bound = _wide_bound(*FILE_READ, "generate_file")
    surface = _surface(("search_tools", *FILE_READ, "generate_file", *sorted(bound)), bound)

    assert surface.bound_tool_loading_mode is RuntimeAgentBoundToolLoadingMode.PROGRESSIVE
    for name in FILE_READ:
        assert name in surface.eager_tool_names
        assert name not in surface.deferred_tool_names


def test_unbound_file_agent_does_not_receive_read_file() -> None:
    bound = {"workspace_agent"}
    surface = _surface(("search_tools", "workspace_agent", *FILE_READ), bound)

    assert "read_file" not in surface.visible_tool_names
    assert "read_file" not in surface.eager_tool_names


def test_master_workspace_surface_still_eagers_file_read_and_invoke_skill() -> None:
    surface = _surface(
        ("search_tools", *FILE_READ, "invoke_skill"),
        None,
        is_master=True,
    )

    for name in FILE_READ:
        assert name in surface.eager_tool_names
    assert "invoke_skill" in surface.eager_tool_names


def test_non_workspace_agent_profile_does_not_change_unscoped_master() -> None:
    surface = _surface(("search_tools", "read_file"), None, is_master=True, tool_profile=None)

    assert "search_tools" in surface.eager_tool_names
    assert "read_file" in surface.visible_tool_names


def test_custom_agent_base_includes_file_read_not_rag() -> None:
    for name in FILE_READ:
        assert name in WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES
    assert "rag" not in WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES

    tools = normalize_workspace_custom_agent_tool_bindings(["generate_file"])
    for name in FILE_READ:
        assert name in tools
    assert "rag" not in tools
    assert "generate_file" in tools


def test_expander_none_stays_none() -> None:
    assert expand_authorized_runtime_file_read_bindings(None) is None


def test_expander_replaces_rag_and_write_with_file_read() -> None:
    expanded = expand_authorized_runtime_file_read_bindings({"rag", "generate_file"})
    assert expanded is not None
    assert "rag" not in expanded
    assert set(FILE_READ) <= expanded
    assert "generate_file" in expanded


def test_expander_does_not_grant_read_to_workspace_only_agent() -> None:
    expanded = expand_authorized_runtime_file_read_bindings({"workspace_agent"})
    assert expanded == {"workspace_agent"}
    assert "read_file" not in expanded


def test_hidden_rag_stays_undiscoverable_after_file_read_eager() -> None:
    assert "rag" in HIDDEN_RUNTIME_TOOLS
    expanded = expand_authorized_runtime_file_read_bindings({"rag", "patch_file"})
    bound = _wide_bound(*(expanded or ()))
    surface = _surface(("search_tools", "rag", *FILE_READ, "patch_file", *sorted(bound)), bound)

    assert not runtime_tool_visible_for_profile(
        "rag",
        bound_tool_names=bound,
        is_master=False,
        mcp_allowed_names=set(),
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    assert "rag" not in surface.visible_tool_names
    assert "rag" not in surface.eager_tool_names
    for name in FILE_READ:
        assert name in surface.eager_tool_names


@pytest.mark.asyncio
async def test_runtime_agent_tool_scope_expands_live_rag_write_bindings(monkeypatch) -> None:
    from packages.core.ai.runtime.tool_bindings import runtime_agent_tool_scope

    async def fake_get_agent_tools(db, agent_id):
        return [SimpleNamespace(name="rag"), SimpleNamespace(name="generate_file")]

    monkeypatch.setattr(
        "packages.core.services.agent_service.get_agent_tools",
        fake_get_agent_tools,
    )

    scoped = await runtime_agent_tool_scope(
        SimpleNamespace(),
        agent_id="agent_custom",
        is_master=False,
    )
    assert scoped.bound_tool_names is not None
    assert "rag" not in scoped.bound_tool_names
    assert set(FILE_READ) <= set(scoped.bound_tool_names)
    assert "generate_file" in scoped.bound_tool_names

    master = await runtime_agent_tool_scope(
        SimpleNamespace(),
        agent_id="agent_custom",
        is_master=True,
    )
    assert master.bound_tool_names is None
