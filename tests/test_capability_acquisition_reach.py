"""acquire_capabilities' readiness and the Agent's runtime tool scope are one rule.

2026-10-15 Video Studio eval: a Workspace Agent bound to generate_file and
generate_image invoked a picture-book Skill declaring both. The runtime shadowed
the explicitly bound generate_image behind generate_file, so the Skill ran
without it, while acquire_capabilities prepared "ready" and its apply was a
no-op (capability_not_applied). Readiness is now the runtime's own resolution
(``runtime_tool_binding_reach``) over the Agent's live envelope, so for every
tool a shipped Skill declares: "acquire reports ready" <=> "binding puts the
tool in the Agent's runtime scope", and each such tool can be acquired.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.capability_refresh import (
    RuntimeToolBindingReach,
    runtime_tool_binding_reach,
)
from packages.core.ai.runtime.entrypoints import runtime_request_for_chat_turn
from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
from packages.core.ai.runtime.skills import (
    runtime_prepare_prompt_skill_tool_surface,
    runtime_skill_tools_withheld_from_surface,
    runtime_skill_work_tools,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
from packages.core.ai.runtime.tool_surface import runtime_prepare_agent_tool_surface_for_turn
from packages.core.ai.runtime.tool_visibility import runtime_tool_is_catalog_bindable
from packages.core.services.builtin_skill_loader import (
    _packaged_builtin_skill_dirs_by_slug,
    packaged_builtin_skill_declaration,
)

CONFIGS = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"
READY = {RuntimeToolBindingReach.IN_SCOPE, RuntimeToolBindingReach.BINDABLE}


def _agent_envelope(bound: set[str]):
    """A subscribed Workspace Agent's Task-loop envelope, resolved as a wake resolves it."""
    request = runtime_request_for_chat_turn(
        surface=ChatSurface.SCHEDULED_AGENT_RUN, entity_id="entity", user_id="user",
        agent_id="agent", workspace_id="workspace", task_id="task", message="brief",
    )
    return runtime_prepare_agent_tool_surface_for_turn(
        request, agent_id="agent", bound_tool_names=set(bound), is_master=False,
        mcp_allowed_names=set(), tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    ).envelope


def _shipped_skills():
    for slug in sorted(_packaged_builtin_skill_dirs_by_slug()):
        yield f"builtin:{slug}", packaged_builtin_skill_declaration(slug)
    for path in sorted(CONFIGS.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for payload in [data] if "manifest" in data else data.values():
            for skill in (payload.get("embedded") or {}).get("skills") or ():
                yield f"{path.name}:{skill['slug']}", SimpleNamespace(
                    id=None, slug=skill["slug"], name=skill["slug"], entity_id="embedded",
                    tools=list(skill.get("tools") or ()), config=dict(skill.get("config") or {}),
                )


def test_every_shipped_skill_tool_is_ready_exactly_when_binding_puts_it_in_scope():
    registered = runtime_registered_tool_schemas()
    names = {name for name, _ in registered}
    checked = 0
    for label, skill in _shipped_skills():
        declared = [
            name for name in runtime_skill_work_tools(skill)
            if name in names and runtime_tool_is_catalog_bindable(name)
        ]
        for tool in declared:
            # An Agent set up for this Skill except for one tool it declares.
            without = _agent_envelope(set(declared) - {tool})
            reach = runtime_tool_binding_reach(tool, envelope=without, registered_tool_schemas=registered)
            bound = _agent_envelope(set(declared))
            in_scope = tool in bound.effective_allowed_tool_names()
            assert (reach in READY) == in_scope, (label, tool, reach)
            assert (reach is RuntimeToolBindingReach.IN_SCOPE) == (
                tool in without.effective_allowed_tool_names()
            ), (label, tool, reach)
            # A shipped Skill's tool must be acquirable, and once bound the
            # Skill invoked by that Agent receives it.
            assert reach in READY, (label, tool, reach)
            surface = runtime_prepare_prompt_skill_tool_surface(
                skill, allowed_tool_names=bound.effective_allowed_tool_names(), runtime_envelope=bound,
                get_schemas_for_names=lambda _names: [], get_registered_tool_names=lambda: names,
            )
            assert tool not in runtime_skill_tools_withheld_from_surface(
                skill, surface, registered_tool_names=names,
            ), (label, tool)
            checked += 1
    assert checked > 50, "the guard checked almost nothing"


@pytest.mark.parametrize(("tool", "reach"), [
    # Initiatives are never exposed to a Workspace Agent's Task loop.
    ("create_agent_initiative", RuntimeToolBindingReach.NOT_EXPOSED),
    ("no_such_tool", RuntimeToolBindingReach.NOT_REGISTERED),
])
def test_a_tool_binding_cannot_expose_is_refused_with_its_reason(tool, reach):
    assert runtime_tool_binding_reach(tool, envelope=_agent_envelope({"generate_file"})) is reach
