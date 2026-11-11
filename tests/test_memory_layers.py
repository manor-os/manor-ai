"""Layered memory: what each kind of turn may see, through the real prompt builder."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
from packages.core.ai.runtime.profiles import runtime_profile_name_for_surface
from packages.core.ai.runtime.prompt_adapter import ChatContext, PromptBuilder
from packages.core.ai.runtime.prompt_sections import runtime_prompt_section_renderers, runtime_prompt_section_specs
from packages.core.config import get_settings
from packages.core.memory.canonical import write_workspace_memory_file
from packages.core.memory.layers import SOFT_LIMITS, org_memory_path, personal_memory_path
from packages.core.memory.layer_prompt import speaker_preferences_reminder
from packages.core.services.agent_files import write_agent_file
from packages.core.services.entity_fs import provision_entity_filesystem

E, OWNER, MEMBER, COLLABORATOR, WS = "ent_layers", "user_owner", "user_member", "user_collab", "ws_layers"
_SECTIONS = {"agent_identity", "workspace_charter", "memory_layers", "speaker_memory"}
_ALL = {"ORG", "PROFILE", "PREFERENCES", "PRIVATE", "WORKSPACE", "OLD-MASTER", "OUTER-ROLE"}


def _put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


@pytest.fixture
def layered_fs(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    # Matrix cases have no session; the speaker is an employee unless a case says otherwise.
    monkeypatch.setattr(
        "packages.core.memory.layer_prompt.speaker_is_collaborator",
        AsyncMock(side_effect=lambda db, *, entity_id, user_id: user_id == COLLABORATOR),
    )
    provision_entity_filesystem(E, "Layers Inc")
    _put(org_memory_path(E), "# Organization\n- CANARY-ORG")
    for layer in ("profile", "preferences", "private"):
        _put(personal_memory_path(E, OWNER, layer), f"# {layer}\n- CANARY-{layer.upper()}")
    write_workspace_memory_file(E, WS, "MEMORY.md", "# Workspace Memory\n- CANARY-WORKSPACE")
    write_agent_file(E, None, "AGENT.md", "# Role\n- CANARY-OUTER-ROLE")
    write_agent_file(E, None, "MEMORY.md", "# Memory\n- CANARY-OLD-MASTER", user_id=OWNER)
    return tmp_path


async def _build(surface, *, speaker, sections=_SECTIONS, conversation_id=None, envelope=True, **request):
    if envelope:
        envelope = RuntimeResolver().resolve_trace_envelope(
            AIRuntimeRequest(surface=surface, entity_id=E, user_id=speaker, **request)
        )
    # As runtime_build_base_prompt_for_turn sets them.
    profile = runtime_profile_name_for_surface(surface, None)
    ctx = ChatContext(
        entity_id=E, user_id=speaker, workspace_id=request.get("workspace_id"),
        agent_id=request.get("agent_id"), conversation_id=conversation_id,
        runtime_envelope=envelope or None, runtime_surface=surface.value,
        runtime_profile=profile, runtime_profile_name=profile,
        workspace=SimpleNamespace(name="Layers Ops", kind="marketing",
                                  primary_work="Run layered campaigns.", description=None),
    )
    builder, renderers = PromptBuilder(), runtime_prompt_section_renderers()
    for spec in runtime_prompt_section_specs("full"):
        if spec.name in sections:
            builder.add(renderers[spec.name], name=spec.name, modes=list(spec.modes), turn_scoped=spec.turn_scoped)
    return await builder.build(ctx, split_turn_context=True), ctx


def _turn(surface, *, speaker, **kwargs):
    stable, ctx = asyncio.run(_build(surface, speaker=speaker, **kwargs))
    return stable, ctx.runtime_turn_context, {s["key"] for s in ctx.runtime_context_sections}


CASES = {
    # surface, speaker, request, seen in the cached prompt, seen in this turn only, charter
    "own_chat": (ChatSurface.GLOBAL_OWNER_CHAT, OWNER, {},
                 {"ORG", "PROFILE", "PREFERENCES", "PRIVATE", "OLD-MASTER", "OUTER-ROLE"}, set(), False),
    "workspace_owner_speaks": (ChatSurface.WORKSPACE_CHAT, OWNER, {"workspace_id": WS},
                               {"ORG", "WORKSPACE"}, {"PROFILE", "PREFERENCES"}, True),
    "workspace_member_speaks": (ChatSurface.WORKSPACE_CHAT, MEMBER, {"workspace_id": WS},
                                {"ORG", "WORKSPACE"}, set(), True),
    "workspace_collaborator_speaks": (ChatSurface.WORKSPACE_CHAT, COLLABORATOR, {"workspace_id": WS},
                                      {"WORKSPACE"}, set(), True),
    "workspace_scheduled_run": (ChatSurface.SCHEDULED_AGENT_RUN, OWNER, {"workspace_id": WS},
                                {"ORG", "WORKSPACE"}, set(), True),
    "external_channel": (ChatSurface.EXTERNAL_CHANNEL_CHAT, OWNER, {"workspace_id": WS},
                         None, set(), False),
    # Channel replies build their base prompt with no envelope.
    "external_channel_base_prompt": (ChatSurface.EXTERNAL_CHANNEL_CHAT, OWNER,
                                     {"workspace_id": WS, "envelope": False}, None, set(), False),
}


@pytest.mark.parametrize("case", CASES)
def test_memory_layers_follow_surface_and_speaker(layered_fs, case):
    surface, speaker, request, in_stable, in_turn, charter = CASES[case]
    stable, ctx = asyncio.run(_build(surface, speaker=speaker, **request))
    turn, keys = ctx.runtime_turn_context, {s["key"] for s in ctx.runtime_context_sections}
    for name in _ALL:
        assert (f"CANARY-{name}" in stable) == (name in (in_stable or ())), (case, name, "cached prompt")
        assert (f"CANARY-{name}" in turn) == (name in in_turn), (case, name, "turn context")
    assert ("workspace_charter" in keys) == charter
    assert ("memory_layers" in keys) == (in_stable is not None)
    # Repeated after tool results: only preferences this turn already sees.
    reminder = speaker_preferences_reminder(ctx) or ""
    assert ("CANARY-PREFERENCES" in reminder) == ("PREFERENCES" in (in_stable or set()) | in_turn)
    assert "CANARY-PROFILE" not in reminder


@pytest.mark.parametrize("agent_id, charter", [("agent_sales", False), (None, True)])
def test_edited_workspace_doc_reaches_every_workspace_agent_once(layered_fs, agent_id, charter):
    write_workspace_memory_file(E, WS, "WORKSPACE.md", "# Workspace\n- CANARY-CHARTER-NOTES")
    stable, _, keys = _turn(
        ChatSurface.WORKSPACE_CHAT, speaker=OWNER, workspace_id=WS,
        sections=_SECTIONS | {"workspace_operating_memory"}, **({"agent_id": agent_id} if agent_id else {}),
    )
    assert stable.count("CANARY-CHARTER-NOTES") == 1
    assert ("workspace_charter" in keys) == charter


async def test_refresh_keeps_a_new_speaker_profile_out_of_the_shared_prompt(client, layered_fs):
    import packages.core.database as database
    from packages.core.ai.runtime.context_memory import RuntimeContextMemory
    from packages.core.models.task import Conversation
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace

    os.remove(personal_memory_path(E, OWNER, "profile"))
    async with database.async_session() as db:
        db.add_all([
            User(id=OWNER, entity_id=E, email="layers-owner@test.invalid", password_hash="test", role="owner"),
            Workspace(id=WS, entity_id=E, name="Layers Ops", status="active"),
            Conversation(id="c_layers", entity_id=E, user_id=OWNER, workspace_id=WS),
        ])
        await db.commit()
    stable, ctx = await _build(
        ChatSurface.WORKSPACE_CHAT, speaker=OWNER, workspace_id=WS, conversation_id="c_layers",
    )
    assert "CANARY-ORG" in stable and "CANARY-PROFILE" not in stable
    bridge = RuntimeContextMemory(ctx)
    # Written mid-conversation, e.g. by remember.
    _put(personal_memory_path(E, OWNER, "profile"), "# profile\n- CANARY-PROFILE")
    refreshed = await bridge.refresh([{"role": "system", "content": bridge.base},
                                      {"role": "user", "content": "Next request"}])
    assert "CANARY-ORG" in refreshed[0]["content"]
    assert "CANARY-PROFILE" not in refreshed[0]["content"]


def test_oversized_layer_is_cut_with_a_read_pointer(layered_fs):
    _put(org_memory_path(E), "# Organization\n" + "x" * 5000 + "\nCANARY-TAIL")
    stable, _, _ = _turn(ChatSurface.GLOBAL_OWNER_CHAT, speaker=OWNER)
    assert "CANARY-TAIL" not in stable
    assert f"Shortened at {SOFT_LIMITS['org']} characters" in stable and "layer=org" in stable


async def test_indexes_and_reads_follow_workspace_membership(client, layered_fs):
    import packages.core.database as database
    from packages.core.ai.runtime import runtime_manor_read_memory
    from packages.core.memory.layer_prompt import member_profile_index_section, workspace_index_section
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    async with database.async_session() as db:
        db.add_all([
            User(id=OWNER, entity_id=E, email="layers-owner@test.invalid", password_hash="test", role="owner",
                 display_name="Olive Owner"),
            User(id=MEMBER, entity_id=E, email="layers-member@test.invalid", password_hash="test", role="member"),
            Workspace(id=WS, entity_id=E, name="Layers Ops", status="active", description="Campaigns"),
            Workspace(id="ws_hidden", entity_id=E, name="Hidden Ops", status="active"),
            WorkspaceStaff(workspace_id=WS, user_id=MEMBER, role="editor", status="active"),
        ])
        await db.commit()

    async with database.async_session() as db:
        member = await db.get(User, MEMBER)
        index = (await workspace_index_section(db, entity_id=E, user=member))["content"]
        assert f"workspace_id={WS}" in index and "ws_hidden" not in index
        people = (await member_profile_index_section(db, entity_id=E, workspace_id=WS))["content"]
        assert "Olive Owner" in people and f"user_id={OWNER}" in people

        async def read(surface, speaker, layer, **params):
            envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
                surface=surface, entity_id=E, user_id=speaker,
                **({"workspace_id": WS} if surface == ChatSurface.WORKSPACE_CHAT else {}),
            ))
            return json.loads(await runtime_manor_read_memory(
                db, entity_id=E, user_id=speaker, params={"layer": layer, **params}, runtime_envelope=envelope,
            ))

        in_ws = ChatSurface.WORKSPACE_CHAT
        assert "CANARY-PROFILE" in (await read(in_ws, MEMBER, "profile", user_id=OWNER))["content"]
        assert "error" in await read(in_ws, MEMBER, "preferences", user_id=OWNER)
        assert "error" in await read(in_ws, MEMBER, "private", user_id=OWNER)
        assert "error" in await read(in_ws, OWNER, "private")
        assert "error" in await read(in_ws, COLLABORATOR, "org")
        assert "error" in await read(in_ws, MEMBER, "profile", user_id="..")

        own = ChatSurface.GLOBAL_OWNER_CHAT
        assert "CANARY-WORKSPACE" in (await read(own, MEMBER, "workspace", workspace_id=WS))["content"]
        assert "error" in await read(own, MEMBER, "workspace", workspace_id="ws_hidden")
        assert "error" in await read(own, MEMBER, "profile", user_id=OWNER)
        assert "CANARY-PRIVATE" in (await read(own, OWNER, "private"))["content"]
