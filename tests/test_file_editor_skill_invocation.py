"""The file editor invokes Skills exactly as ordinary chat does.

Product decision 2026-10-06: the file editor surface may invoke any Skill the
caller could invoke in ordinary chat, including sandbox Skills; Skill-level
authorization (user access, Agent bindings, plugin enablement) is unchanged.
External principals still never reach a sandbox Skill. The workspace
architect is deliberately out of scope.

Every case is derived from the production pipeline: the registry tool
surface feeds the RuntimeResolver, and Skill visibility comes from the same
descriptor resolver and ``invoke_skill`` service the chat runtime uses.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.capabilities import (
    CORE_CAPABILITIES,
    allowed_tools_for_profile,
    tool_names_for_capability_ids,
)
from packages.core.ai.runtime.profiles import (
    EXTERNAL_CHAT_SURFACES,
    RuntimeProfile,
)
from packages.core.ai.runtime.surfaces import ChatSurface


EDITOR = ChatSurface.FILE_EDITOR_CHAT
# The ordinary chat a caller of the editor would otherwise use: the owner's
# own chat without an Agent, or that Agent's DM.
_CHAT_FOR_IDENTITY = {
    "owner": ChatSurface.GLOBAL_OWNER_CHAT,
    "bound_agent": ChatSurface.AGENT_DM,
    "unbound_agent": ChatSurface.AGENT_DM,
}
_EXTERNAL_SURFACES = sorted(EXTERNAL_CHAT_SURFACES, key=lambda s: s.value)


def _schema_name(schema: dict) -> str:
    return str((schema.get("function") or schema).get("name") or "")


def _resolve(
    surface: ChatSurface,
    *,
    identity: str,
    entity_id: str = "entity",
    user_id: str | None = "user",
    agent_id: str = "agent",
    editor_context: dict | None = None,
):
    """Run one turn through the production tool-surface pipeline."""
    from packages.core.ai.runtime.entrypoints import runtime_request_for_surface_turn
    from packages.core.ai.runtime.profiles import tool_profile_for_surface
    from packages.core.ai.runtime.resolver import RuntimeResolver
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
    from packages.core.ai.runtime.tool_visibility import (
        runtime_tool_registry_surface_from_schemas,
    )

    is_master = identity == "owner"
    bound = {"invoke_skill"} if identity == "bound_agent" else set()
    tool_profile = tool_profile_for_surface(surface)
    registry = runtime_tool_registry_surface_from_schemas(
        runtime_registered_tool_schemas(),
        bound_tool_names=None if is_master else bound,
        is_master=is_master,
        mcp_allowed_names=None if is_master else set(),
        tool_profile=tool_profile,
    )
    request = runtime_request_for_surface_turn(
        surface=surface,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=None if is_master else agent_id,
        editor_context=editor_context,
    )
    return RuntimeResolver().resolve_tool_surface(
        request,
        tool_profile=tool_profile,
        tool_schemas=registry.prompt_schemas,
        allowed_tool_names=registry.visible_tool_names,
    )


# ── Capability and tool surface ────────────────────────────────────────


def test_skill_invoke_reaches_the_file_editor_profile_only_by_decision() -> None:
    profiles = CORE_CAPABILITIES["skill.invoke"].profiles
    assert RuntimeProfile.FILE_EDITOR_PATCH in profiles
    # Out of scope by product decision; widening it is a separate change.
    assert RuntimeProfile.WORKSPACE_ARCHITECT not in profiles
    editor_allowlist = allowed_tools_for_profile(RuntimeProfile.FILE_EDITOR_PATCH)
    assert {"invoke_skill", "sandbox"} <= editor_allowlist
    assert "invoke_skill" in tool_names_for_capability_ids(
        {"skill.invoke"}, profile=RuntimeProfile.FILE_EDITOR_PATCH,
    )


@pytest.mark.parametrize("identity", list(_CHAT_FOR_IDENTITY))
def test_editor_reaches_invoke_skill_exactly_when_ordinary_chat_does(identity) -> None:
    from packages.core.ai.runtime.policies import check_runtime_tool_policy

    editor = _resolve(EDITOR, identity=identity)
    chat = _resolve(_CHAT_FOR_IDENTITY[identity], identity=identity)
    expected = identity != "unbound_agent"

    for resolved in (editor, chat):
        envelope = resolved.envelope
        assert ("invoke_skill" in envelope.effective_allowed_tool_names()) is expected
        decision = check_runtime_tool_policy(
            envelope=envelope, tool_name="invoke_skill", arguments={"skill": "pdf"},
        )
        assert decision.allowed is expected, decision
        # Same eager placement as the chat surface: master and bound Agents
        # carry the schema; an unbound Agent carries neither.
        assert (
            "invoke_skill" in {_schema_name(schema) for schema in resolved.tool_schemas}
        ) is expected
    assert editor.envelope.profile is RuntimeProfile.FILE_EDITOR_PATCH
    # The editor keeps its patch-only boundary for everything else.
    assert "write_file" not in editor.envelope.effective_allowed_tool_names()


def test_skill_outputs_keep_the_editor_patch_only_write_boundary() -> None:
    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.prompt_guidance import runtime_file_editor_live_edit_guidance

    mounted = "docs/brief.md"
    editor_context = {"path": mounted, "supportsNativeFilePatch": True}
    envelope = _resolve(EDITOR, identity="owner", editor_context=editor_context).envelope

    def decision(name, arguments):
        return check_runtime_tool_policy(envelope=envelope, tool_name=name, arguments=arguments)

    # A sandbox Skill delivers through save_result: a managed output that the
    # handler writes under a fresh collision-safe name (plain filename only,
    # require_missing), behind its own SAVE_FILE approval gate.
    save = {"action": "save_result", "params": {
        "sandbox_id": "sbx-1", "file_path": "/skill/out/brief.pdf", "filename": "brief.pdf",
    }}
    assert decision("sandbox", save).allowed
    # The mounted file itself still changes only through approved patch_file.
    assert decision("patch_file", {"path": mounted, "operations": []}).allowed
    for name in ("write_file", "edit_file", "replace_file", "delete_file"):
        denied = decision(name, {"path": mounted, "target_path": mounted, "content": "x"})
        assert not denied.allowed, name
    # A Skill can never write elsewhere through a direct file tool either.
    assert not decision("write_file", {"path": "docs/other.md", "content": "x"}).allowed
    guidance = runtime_file_editor_live_edit_guidance(editor_context)
    assert "never overwrite the active file" in guidance
    assert "Do not call unrestricted write, edit, delete, shell, or sandbox tools" not in guidance


@pytest.mark.parametrize("surface", _EXTERNAL_SURFACES, ids=lambda s: s.value)
def test_external_surfaces_never_reach_the_sandbox_a_sandbox_skill_hands_off_to(surface) -> None:
    from packages.core.ai.runtime.policies import check_runtime_tool_policy

    for identity in ("owner", "bound_agent"):
        envelope = _resolve(surface, identity=identity).envelope
        # Even a run-local handoff grant cannot reopen the sandbox.
        envelope.discovered_tool_grants.grant(["sandbox"])
        for action in ("create", "exec"):
            decision = check_runtime_tool_policy(
                envelope=envelope, tool_name="sandbox", arguments={"action": action},
            )
            assert not decision.allowed, (identity, action)


# ── Database-backed: authorization and Skill visibility ───────────────


async def _seed_scope(db, *, role: str = "member"):
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Agent

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@editor.example",
        password_hash="x", role=role,
    )
    other = User(
        id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@editor.example",
        password_hash="x", role="member",
    )
    db.add_all([user, other])
    db.add_all([
        UserMembership(user_id=user.id, entity_id=entity_id, role=role, status="active"),
        UserMembership(user_id=other.id, entity_id=entity_id, role="member", status="active"),
    ])
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Editor helper", status="active")
    db.add(agent)

    def sandbox_skill(slug: str, **overrides) -> Skill:
        config = {"type": "sandbox", **overrides.pop("config", {})}
        return Skill(
            id=generate_ulid(), entity_id=entity_id, name=slug, slug=slug,
            description=f"{slug} sandbox skill", system_prompt=f"# {slug}",
            tools=["invoke_skill", "search_tools", "generate_file", "sandbox"],
            config=config, status="active", **overrides,
        )

    skills = {
        # Public entity Skill: every caller in the entity may use it.
        "public": sandbox_skill(f"sbx-public-{entity_id[-6:].lower()}", is_public=True),
        # Private Skill another user owns: not readable by this user, not
        # usable by an Agent without a binding.
        "private": sandbox_skill(
            f"sbx-private-{entity_id[-6:].lower()}", is_public=False,
            owner_user_id=other.id, visibility="private",
        ),
        # Entity-visible private Skill: readable by every member, usable by
        # an Agent only through its binding.
        "bound": sandbox_skill(f"sbx-bound-{entity_id[-6:].lower()}", is_public=False),
        # A plugin Skill (disabled by default) the user must enable first.
        "plugin": sandbox_skill(
            f"sbx-plugin-{entity_id[-6:].lower()}", is_public=True,
            config={"plugin_key": "remotion"},
        ),
    }
    db.add_all(skills.values())
    await db.flush()
    db.add(AgentSkillBinding(agent_id=agent.id, skill_id=skills["bound"].id, status="active"))
    await db.flush()
    return entity_id, user, agent, skills


def _envelope_for(surface, identity, entity_id, user, agent):
    return _resolve(
        surface, identity=identity, entity_id=entity_id, user_id=user.id, agent_id=agent.id,
    ).envelope


async def _visible_skill_ids(db, envelope) -> set[str]:
    from packages.core.ai.runtime.skills import resolve_skill_descriptors_for_envelope

    descriptors = await resolve_skill_descriptors_for_envelope(
        db, envelope, limit=10_000, raise_on_error=True,
    )
    return {descriptor.id for descriptor in descriptors}


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["owner", "bound_agent"])
async def test_editor_skill_visibility_matches_ordinary_chat(db_session, identity) -> None:
    from packages.core.services.builtin_plugin_service import set_user_builtin_plugin_enabled

    entity_id, user, agent, skills = await _seed_scope(db_session)
    editor = _envelope_for(EDITOR, identity, entity_id, user, agent)
    chat = _envelope_for(_CHAT_FOR_IDENTITY[identity], identity, entity_id, user, agent)

    editor_ids = await _visible_skill_ids(db_session, editor)
    chat_ids = await _visible_skill_ids(db_session, chat)
    # Never wider than chat: the editor only narrows by its own tool allowlist.
    assert editor_ids <= chat_ids
    ours = {key: skill.id for key, skill in skills.items()}
    for key, skill_id in ours.items():
        assert (skill_id in editor_ids) == (skill_id in chat_ids), key
    assert ours["public"] in editor_ids
    assert ours["private"] not in editor_ids
    assert ours["plugin"] not in editor_ids
    # Bindings scope an Agent; the user's own read access scopes owner chat.
    assert ours["bound"] in editor_ids

    await set_user_builtin_plugin_enabled(
        db_session, user_id=user.id, plugin_key="remotion", enabled=True,
    )
    assert ours["plugin"] in await _visible_skill_ids(db_session, editor)
    assert ours["plugin"] in await _visible_skill_ids(db_session, chat)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["owner", "bound_agent"])
async def test_editor_invokes_exactly_the_skills_chat_can_invoke(
    db_session, monkeypatch, identity,
) -> None:
    from packages.core.services import skill_service

    entity_id, user, agent, skills = await _seed_scope(db_session)
    admitted: list[str] = []

    async def fake_sandbox_admission(skill, *_args, **_kwargs):
        admitted.append(skill.id)
        return {"content": "ready", "stop_reason": "sandbox_ready", "sandbox_id": "sbx-1"}

    monkeypatch.setattr(skill_service, "_invoke_sandbox_skill", fake_sandbox_admission)

    async def invoke(surface, skill):
        envelope = _envelope_for(surface, identity, entity_id, user, agent)
        return await skill_service.invoke_skill(
            db_session, skill.slug, entity_id, "make the file",
            agent_id=envelope.agent_id, user_id=user.id, conversation_id="conversation-1",
            allowed_tool_names=set(envelope.allowed_tool_names), runtime_envelope=envelope,
        )

    for key, skill in skills.items():
        editor_result = await invoke(EDITOR, skill)
        chat_result = await invoke(_CHAT_FOR_IDENTITY[identity], skill)
        assert editor_result.get("code") == chat_result.get("code"), (key, editor_result)
        assert editor_result.get("stop_reason") == chat_result.get("stop_reason"), key
    usable = {"public", "bound"}
    # Each usable Skill reached sandbox admission once from each surface.
    assert sorted(admitted) == sorted(
        skills[key].id for key in usable for _surface in (EDITOR, "chat")
    )


@pytest.mark.asyncio
async def test_editor_skill_then_sandbox_pass_the_execution_gate_for_an_internal_user(
    db_session, monkeypatch,
) -> None:
    from contextlib import asynccontextmanager

    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.skills import runtime_activate_sandbox_skill_handoff

    entity_id, user, agent, _skills = await _seed_scope(db_session)

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(database, "async_session", session)

    async def guard(envelope, name, arguments):
        return await guard_runtime_tool_action(
            name=name, arguments=dict(arguments), entity_id=entity_id, user_id=user.id,
            workspace_id=None, conversation_id="conversation-1", runtime_envelope=envelope,
            classification=classify_runtime_tool(name, arguments),
        )

    editor = _envelope_for(EDITOR, "owner", entity_id, user, agent)
    invoke = {"skill": "pdf", "input": "export this file"}
    assert check_runtime_tool_policy(envelope=editor, tool_name="invoke_skill", arguments=invoke).allowed
    # invoke_skill is an orchestrator: the permission boundary admits it and
    # each child effect is gated on its own.
    assert await guard(editor, "invoke_skill", invoke) is None

    class _Context:
        runtime_envelope = editor

    assert runtime_activate_sandbox_skill_handoff(
        _Context(), {"stop_reason": "sandbox_ready", "sandbox_id": "sbx-1"},
    )
    exec_call = {"action": "exec", "params": {"sandbox_id": "sbx-1", "command": "python run.py"}}
    assert check_runtime_tool_policy(envelope=editor, tool_name="sandbox", arguments=exec_call).allowed
    assert await guard(editor, "sandbox", {"action": "create"}) is None
    # The command passes the permission boundary and stops only for the
    # missing persisted call identity (the approval-policy stage).
    assert json.loads(await guard(editor, "sandbox", exec_call))["error"] == (
        "approval_operation_identity_missing"
    )
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _EXTERNAL_SURFACES, ids=lambda s: s.value)
async def test_external_surfaces_cannot_invoke_a_sandbox_skill(
    db_session, monkeypatch, surface,
) -> None:
    from packages.core.models.skill import AgentSkillBinding
    from packages.core.services import skill_service

    entity_id, user, agent, skills = await _seed_scope(db_session)
    public = skills["public"]
    # Even a bound Skill that opts into the external surface and declares no
    # sandbox tool never allocates a sandbox for an external principal.
    public.config = {**public.config, "allowed_surfaces": [surface.value]}
    public.tools = []
    db_session.add(AgentSkillBinding(agent_id=agent.id, skill_id=public.id, status="active"))
    await db_session.flush()

    async def must_not_admit(*_args, **_kwargs):
        raise AssertionError("an external surface reached sandbox admission")

    monkeypatch.setattr(skill_service, "_invoke_sandbox_skill", must_not_admit)
    envelope = _envelope_for(surface, "bound_agent", entity_id, user, agent)
    result = await skill_service.invoke_skill(
        db_session, public.slug, entity_id, "make the file",
        agent_id=agent.id, user_id=None, conversation_id="conversation-1",
        allowed_tool_names=set(envelope.allowed_tool_names), runtime_envelope=envelope,
    )
    assert result.get("code") == "skill_surface_not_allowed", result
