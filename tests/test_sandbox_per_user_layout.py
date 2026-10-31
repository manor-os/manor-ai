"""One general sandbox per user: ownership keys, work areas, Skill views.

* ownership: a human user's scope is the USER (every conversation and task
  of the user resolves the same records); user-less work and group room runs
  are isolated by their own key; per-agent views are per work + agent;
* the work-area layout helper (``runtime_sandbox_layout``) the bash tool uses
  to place a command and to attribute its file changes;
* the control plane names a Skill's directory exactly as the runner does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_sandbox_session_service import fake_cache  # noqa: F401 - fixture

SANDBOX_SERVICE = Path(__file__).resolve().parents[1] / "sandbox-service"


def _scope(user="user_1", key="01CONVERSATIONAAAAAAAAAAAA", entity="ent_1"):
    from packages.core.ai.runtime.sandbox import runtime_sandbox_scope

    return runtime_sandbox_scope(entity_id=entity, user_id=user, binding_key=key)


def test_a_users_conversations_and_tasks_share_one_owner_scope() -> None:
    from packages.core.ai.runtime.sandbox import runtime_room_binding_key, runtime_task_binding_key

    conversation = _scope()
    other_conversation = _scope(key="01OTHERCONVERSATIONAAAAAAA")
    task = _scope(key=runtime_task_binding_key("01TASKAAAAAAAAAAAAAAAAAAAA"))
    assert conversation.shared and other_conversation.shared and task.shared
    assert conversation.prefix == other_conversation.prefix == task.prefix == "sandbox:bind:*:ent_1:user_1"
    assert conversation.key("sbx", "s") == task.key("sbx", "s")
    # Per-agent views stay per work: the same agent in two conversations.
    assert conversation.agent_view_key("a") != other_conversation.agent_view_key("a")

    # Another user, another tenant: other scopes.
    assert _scope(user="user_2").prefix != conversation.prefix
    assert _scope(entity="ent_2").prefix != conversation.prefix
    # User-less work and group room runs: isolated by their own key.
    userless = _scope(user="", key=runtime_task_binding_key("01TASKAAAAAAAAAAAAAAAAAAAA"))
    room = _scope(key=runtime_room_binding_key("01HROOMRUN0000000000000000"))
    assert not userless.shared and not room.shared
    assert userless.prefix.startswith("sandbox:bind:U") and ":system" in userless.prefix
    assert room.prefix.startswith("sandbox:bind:L") and room.prefix != conversation.prefix
    # A binding key can never forge the per-user marker.
    forged = _scope(user="", key="*")
    assert forged.prefix == "sandbox:bind:%2A:ent_1:system"


def test_registry_scopes_follow_the_owner() -> None:
    from packages.core.ai.runtime.sandbox import runtime_room_binding_key, runtime_task_binding_key
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxOwner,
        sandbox_registry_scope,
        sandbox_reuse_scope,
    )

    assert sandbox_registry_scope(user_id="u", binding_key="c1") == "user"
    assert sandbox_registry_scope(user_id="u", binding_key="c2", role="video:session") == "user:video:session"
    task_key = runtime_task_binding_key("01TASKAAAAAAAAAAAAAAAAAAAA")
    assert sandbox_registry_scope(user_id="", binding_key=task_key) == f"conversation:{task_key}"
    room = runtime_room_binding_key("01HROOMRUN0000000000000000")
    assert sandbox_registry_scope(user_id="u", binding_key=room) == f"conversation:{room}"
    assert sandbox_registry_scope(user_id="u", binding_key="") is None

    owner = SandboxOwner(entity_id="e", user_id="u")
    plain = SandboxBundle(skill_name="xlsx", files={})
    remotion = SandboxBundle(skill_name="remotion", files={}, config={"memory": "2g"})
    assert sandbox_reuse_scope(plain, SandboxBinding(conversation_id="c1"), owner) == (True, "user")
    assert sandbox_reuse_scope(remotion, SandboxBinding(conversation_id="c9"), owner) == (True, "user:skill:remotion")


def test_work_area_layout_places_and_attributes_each_command() -> None:
    from packages.core.ai.runtime.sandbox import (
        RUNTIME_SANDBOX_LAYOUT_NOTE,
        runtime_sandbox_agent_workdir,
        runtime_sandbox_layout,
        runtime_sandbox_layout_for,
    )

    layout = runtime_sandbox_layout("01CONV", "agent:7")
    assert layout.work_area == "/workspace/c/01CONV"
    assert layout.agent_dir == "/workspace/c/01CONV/agents/agent-7"
    assert layout.shared_dir == "/workspace/shared"
    assert runtime_sandbox_agent_workdir("agent:7", "01CONV") == layout.agent_dir
    # Attribution: only the command's own agent directory.
    assert layout.owns_path("/workspace/c/01CONV/agents/agent-7/out/a.csv")
    assert not layout.owns_path("/workspace/c/01CONV/agents/agent-8/a.csv")
    assert not layout.owns_path("/workspace/c/01OTHER/agents/agent-7/a.csv")
    assert not layout.owns_path("/workspace/c/01CONV/agents/agent-7/../agent-8/a.csv")
    assert not layout.owns_path("/workspace/shared/a.csv")
    # Path-unsafe keys cannot escape the work root.
    assert runtime_sandbox_layout("../../etc", None).work_area == "/workspace/c/------etc"
    assert runtime_sandbox_layout(None, None).agent_dir == "/workspace/c/default/agents/main"

    class Context:
        sandbox_binding_key = "01CONV"
        conversation_id = "ignored"
        agent_id = None

    assert runtime_sandbox_layout_for(Context()).agent_dir == "/workspace/c/01CONV/agents/main"
    assert "/workspace/shared" in RUNTIME_SANDBOX_LAYOUT_NOTE and "venv" in RUNTIME_SANDBOX_LAYOUT_NOTE


def test_the_control_plane_names_skill_directories_like_the_runner() -> None:
    sys.path.insert(0, str(SANDBOX_SERVICE))
    sys.modules.pop("config", None)
    from sandbox.docker_backend import skill_slot

    from packages.core.ai.tools.sandbox_tools import _skill_view_path, runner_skill_slot

    for name in ("pptx", "PPTX Builder!", "video-edit", "a.b_c", "../..", "", "x" * 90):
        assert runner_skill_slot(name) == skill_slot(name), name
    assert _skill_view_path("/skills/pptx/projects/p/exports/a.pptx", "pptx") == "/skill/projects/p/exports/a.pptx"
    assert _skill_view_path("/skills/pptx", "pptx") == "/skill"
    assert _skill_view_path("/skills/docx/a.docx", "pptx") == "/skills/docx/a.docx"
    assert _skill_view_path("/skills/pptxx/a", "pptx") == "/skills/pptxx/a"
    assert _skill_view_path("/workspace/a", None) == "/workspace/a"


def test_the_runner_charges_disk_to_the_work_area_the_control_plane_places_commands_in() -> None:
    """The runner's disk quota names a command's work area from its fair_key
    (the work key); the control plane puts the command in that very directory."""

    sys.path.insert(0, str(SANDBOX_SERVICE))
    sys.modules.pop("config", None)
    from sandbox.disk_quota import WORK_AREAS_DIR, work_area_key

    from packages.core.ai.runtime.sandbox import (
        RUNTIME_SANDBOX_WORK_ROOT,
        runtime_sandbox_area_key,
        runtime_sandbox_layout,
    )

    assert WORK_AREAS_DIR == RUNTIME_SANDBOX_WORK_ROOT
    for key in ("01CONV", "U01TASK:with:colons", "L01ROOM", "a b/c", "x" * 90, "-"):
        assert work_area_key(key) == runtime_sandbox_area_key(key), key
        assert runtime_sandbox_layout(key, "main").work_area == f"{WORK_AREAS_DIR}/{work_area_key(key)}"
    assert work_area_key("") is None and work_area_key(None) is None


@pytest.mark.asyncio
async def test_skills_coexist_in_the_binding_record_and_views_stay_per_work(fake_cache) -> None:  # noqa: F811
    from packages.core.ai.runtime.sandbox import (
        runtime_agent_skill_in,
        runtime_binding_skills,
        runtime_init_sandbox_binding,
        runtime_load_sandbox_binding,
        runtime_work_skill_sandbox,
    )

    first = _scope(key="01CONVONEAAAAAAAAAAAAAAAAA")
    second = _scope(key="01CONVTWOAAAAAAAAAAAAAAAAA")
    await runtime_init_sandbox_binding(first, "sb", "skill_pptx", agent_id="a", skill_key="pptx")
    await runtime_init_sandbox_binding(second, "sb", "skill_docx", agent_id="a", skill_key="docx")
    record = await runtime_load_sandbox_binding(first, "sb")
    assert runtime_binding_skills(record) == {"skill_pptx": "pptx", "skill_docx": "docx"}
    assert sorted(record["works"]) == sorted([first.binding_key, second.binding_key])
    # The same agent id drives a different Skill in each conversation.
    assert await runtime_agent_skill_in(first, "a", "sb") == {"skill_id": "skill_pptx", "skill_key": "pptx"}
    assert await runtime_agent_skill_in(second, "a", "sb") == {"skill_id": "skill_docx", "skill_key": "docx"}
    assert await runtime_agent_skill_in(first, "b", "sb") is None
    # A conversation follows only the Skills it opened.
    assert (await runtime_work_skill_sandbox(first, "skill_pptx"))[0] == "sb"
    assert await runtime_work_skill_sandbox(first, "skill_docx") is None
    # An older runner replaces its single /skill: the record says so.
    await runtime_init_sandbox_binding(first, "sb", "skill_xlsx", agent_id="a", skill_key="xlsx", coexist=False)
    assert runtime_binding_skills(await runtime_load_sandbox_binding(first, "sb")) == {"skill_xlsx": "xlsx"}
